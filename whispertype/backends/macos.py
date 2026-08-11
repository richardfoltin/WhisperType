"""macOS backend — Quartz event synthesis + NSWorkspace/AX window targeting.

Notes that cost real debugging time if ignored:

  * `CGEventPost` only survives ~20 UTF-16 units per event. The event itself
    stores more (500 round-trips fine), so the truncation is invisible — you
    must chunk. Never split a surrogate pair.
  * `CGEventSetFlags(ev, 0)` on EVERY event. Right Cmd/Ctrl is the hotkey, and
    a leaked modifier flag turns a dictated "a" into ⌘A.
  * `\n`/`\r`/`\t` in the unicode payload are dropped on post; send them as
    real virtual keycodes instead.
  * Without the Accessibility (post-event) grant, `CGEventPost` is a silent
    no-op — no error, no return code. Always preflight.
  * `activateWithOptions_` is asynchronous with no completion callback; typing
    straight after it lands the first characters in the previous app.
"""
import time

import Quartz
from AppKit import (NSApplication, NSApplicationActivateAllWindows,
                    NSRunningApplication, NSWorkspace)

from ..log import log
from .base import Backend
from .mac_gpu import AppleGPU

try:
    from ApplicationServices import (
        AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
        AXUIElementPerformAction, AXUIElementSetAttributeValue,
        AXIsProcessTrusted, AXUIElementSetMessagingTimeout,
        kAXFocusedWindowAttribute, kAXTitleAttribute, kAXWindowsAttribute,
        kAXRaiseAction, kAXMainAttribute, kAXMinimizedAttribute,
    )
    _AX = True
except Exception as e:  # pragma: no cover - depends on pyobjc extras
    _AX = False
    log(f"Accessibility API unavailable: {e}")

kVK_Return = 0x24
kVK_Tab = 0x30

#: Written into kCGEventSourceUserData so a future raw event tap can recognise
#: our own synthetic keystrokes.
EVENT_MARKER = 0x57485459  # 'WHTY'

_TAP = Quartz.kCGHIDEventTap
_MODMASK = (Quartz.kCGEventFlagMaskCommand
            | Quartz.kCGEventFlagMaskControl
            | Quartz.kCGEventFlagMaskAlternate
            | Quartz.kCGEventFlagMaskShift
            | Quartz.kCGEventFlagMaskSecondaryFn)


class MacTarget:
    """The window/app the transcript should go back to."""

    __slots__ = ("pid", "bundle_id", "app_name", "title", "ax_app", "ax_window")

    def __init__(self, pid, bundle_id, app_name, title, ax_app=None, ax_window=None):
        self.pid = pid
        self.bundle_id = bundle_id
        self.app_name = app_name
        self.title = title
        self.ax_app = ax_app
        self.ax_window = ax_window

    def __repr__(self):
        return f"<MacTarget {self.app_name}#{self.pid} {self.title!r}>"


def _utf16_chunks(text, limit=20):
    buf, n = [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > limit and buf:
            yield "".join(buf), n
            buf, n = [], 0
        buf.append(ch)
        n += w
    if buf:
        yield "".join(buf), n


class MacBackend(Backend):
    name = "macos"
    gpu_label = "GPU"

    def __init__(self, own_pid=None):
        import os
        self._own_pid = own_pid or os.getpid()
        self._gpu = AppleGPU()
        self.gpu_available = self._gpu.available
        if self.gpu_available:
            log(f"Apple GPU monitor: {self._gpu.name} ({self._gpu.core_count} cores)")
        else:
            log("Apple GPU monitor unavailable (graph disabled)")
        self._delay = 0.004

    def set_own_window_provider(self, fn):
        pass  # macOS targets by pid; our own process is skipped explicitly

    # ── Target window ────────────────────────────────────────────────────

    def capture_target(self):
        ws = NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        if front is None or front.processIdentifier() == self._own_pid:
            front = self._next_frontmost()
        if front is None:
            return None

        pid = int(front.processIdentifier())
        app_name = str(front.localizedName() or "?")
        bundle_id = str(front.bundleIdentifier() or "")
        ax_app = ax_window = None
        title = ""

        if _AX:
            try:
                ax_app = AXUIElementCreateApplication(pid)
                # Without a timeout an unresponsive app blocks this call — and
                # this runs on the hotkey path, right before recording starts.
                AXUIElementSetMessagingTimeout(ax_app, 0.25)
                err, win = AXUIElementCopyAttributeValue(
                    ax_app, kAXFocusedWindowAttribute, None)
                if err != 0 or win is None:
                    # Some apps expose only AXWindows, never AXFocusedWindow.
                    err, wins = AXUIElementCopyAttributeValue(
                        ax_app, kAXWindowsAttribute, None)
                    win = wins[0] if (err == 0 and wins) else None
                if win is not None:
                    ax_window = win
                    AXUIElementSetMessagingTimeout(win, 0.25)
                    err, value = AXUIElementCopyAttributeValue(
                        win, kAXTitleAttribute, None)
                    if err == 0 and value:
                        title = str(value)
            except Exception as e:
                log(f"AX window lookup failed: {e}")

        return MacTarget(pid, bundle_id, app_name, title or app_name,
                         ax_app, ax_window)

    def _next_frontmost(self):
        """The app behind us — the analogue of GetWindow(hwnd, GW_HWNDNEXT).

        Reached when our own process is frontmost (the user opened the menu-bar
        menu, or clicked a history button). `runningApplications()` is useless
        for this: it is ordered by launch time, not z-order, and once we are
        frontmost no other app reports `isActive()`. `CGWindowListCopyWindowInfo`
        with kCGWindowListOptionOnScreenOnly *is* a true front-to-back list, so
        the first layer-0 window that is not ours belongs to the app the user
        was last working in.
        """
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        for w in (Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []):
            if w.get("kCGWindowLayer") != 0:        # normal app windows only
                continue
            pid = w.get("kCGWindowOwnerPID")
            if pid is None or int(pid) == self._own_pid:
                continue
            bounds = w.get("kCGWindowBounds") or {}
            if bounds.get("Width", 0) < 80 or bounds.get("Height", 0) < 80:
                continue                            # tool/shadow windows
            if float(w.get("kCGWindowAlpha", 1.0)) < 0.05:
                continue
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            if app is not None and not app.isTerminated():
                return app
        return None

    def target_title(self, target):
        if target is None:
            return ""
        title = target.title or target.app_name or "(untitled)"
        return (title[:20] + "…") if len(title) > 20 else title

    def target_app(self, target):
        return target.app_name if target else "?"

    def target_bundle(self, target):
        return (target.bundle_id or "") if target else ""

    def activate(self, target):
        if target is None:
            return False
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(target.pid)
        if app is None or app.isTerminated():
            log(f"activate: pid {target.pid} ({target.app_name}) is gone")
            return False

        if app.isHidden():
            app.unhide()          # the ⌘H analogue of SW_RESTORE

        # Raise the exact window first, then activate the app, so multi-window
        # apps come back to the window the user was actually in.
        if _AX and target.ax_window is not None:
            try:
                AXUIElementSetAttributeValue(target.ax_window,
                                             kAXMinimizedAttribute, False)
                AXUIElementPerformAction(target.ax_window, kAXRaiseAction)
                AXUIElementSetAttributeValue(target.ax_window, kAXMainAttribute, True)
            except Exception as e:
                log(f"AX raise failed: {e}")

        ws = NSWorkspace.sharedWorkspace()

        # Cooperative activation (macOS 14+): if we currently hold activation —
        # the user clicked the menu-bar item or a history button — we have to
        # hand it over explicitly, otherwise the system may refuse the switch.
        front = ws.frontmostApplication()
        if front is not None and front.processIdentifier() == self._own_pid:
            try:
                NSApplication.sharedApplication().yieldActivationToApplication_(app)
            except Exception:
                pass

        for attempt in range(2):
            app.activateWithOptions_(NSApplicationActivateAllWindows)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                front = ws.frontmostApplication()
                if front is not None and front.processIdentifier() == target.pid:
                    # Activation reports done before the app is really keyed.
                    time.sleep(0.08)
                    return True
                time.sleep(0.02)
            if attempt == 0:
                log(f"activate: retrying {target.app_name} (pid {target.pid})")
        log(f"activate: FAILED to activate {target.app_name} (pid {target.pid})")
        return False

    # ── Input synthesis ──────────────────────────────────────────────────

    def _post_pair(self, keycode, unistr, unilen):
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, is_down)
            if ev is None:
                raise RuntimeError("CGEventCreateKeyboardEvent failed")
            Quartz.CGEventSetFlags(ev, 0)
            if unistr is not None:
                Quartz.CGEventKeyboardSetUnicodeString(ev, unilen, unistr)
            Quartz.CGEventSetIntegerValueField(
                ev, Quartz.kCGEventSourceUserData, EVENT_MARKER)
            Quartz.CGEventPost(_TAP, ev)
            if self._delay:
                time.sleep(self._delay)

    def _wait_modifiers_released(self, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            flags = Quartz.CGEventSourceFlagsState(
                Quartz.kCGEventSourceStateCombinedSessionState)
            if not (flags & _MODMASK):
                return True
            time.sleep(0.02)
        log("Modifiers still held after 1.5s — typing anyway")
        return False

    def type_text(self, text):
        if not text:
            return
        if not Quartz.CGPreflightPostEventAccess():
            log("Accessibility (post-event) permission missing — keystrokes "
                "would be silently dropped. Text kept in history.")
            raise PermissionError("post-event access not granted")
        if secure_input_enabled():
            log("Secure input is active (password field?) — refusing to type.")
            raise PermissionError("secure input enabled")

        # The hotkey is a modifier; typing while it is still physically held
        # would turn the transcript into keyboard shortcuts.
        self._wait_modifiers_released()

        for line_i, line in enumerate(text.replace("\r\n", "\n").split("\n")):
            if line_i:
                self._post_pair(kVK_Return, None, 0)
            for seg_i, seg in enumerate(line.split("\t")):
                if seg_i:
                    self._post_pair(kVK_Tab, None, 0)
                for chunk, n in _utf16_chunks(seg, 20):
                    self._post_pair(0, chunk, n)

    def send_enter(self):
        self._post_pair(kVK_Return, None, 0)

    # ── Metrics ──────────────────────────────────────────────────────────

    def gpu_percent(self):
        util, _mem = self._gpu.sample()
        return None if util is None else util * 100.0

    # ── Permissions ──────────────────────────────────────────────────────

    def check_permissions(self):
        missing = []
        if not Quartz.CGPreflightPostEventAccess():
            missing.append(
                "Accessibility — System Settings ▸ Privacy & Security ▸ "
                "Accessibility (needed to type the transcript)")
        if not Quartz.CGPreflightListenEventAccess():
            missing.append(
                "Input Monitoring — System Settings ▸ Privacy & Security ▸ "
                "Input Monitoring (needed for the push-to-talk hotkey)")
        if _AX and not AXIsProcessTrusted():
            missing.append(
                "Accessibility trust — needed to read the target window title")
        return missing

    def request_permissions(self):
        """Trigger the system prompts. Both return the CURRENT (False) state;
        the dialog appears asynchronously."""
        if not Quartz.CGPreflightPostEventAccess():
            Quartz.CGRequestPostEventAccess()
        if not Quartz.CGPreflightListenEventAccess():
            Quartz.CGRequestListenEventAccess()


def secure_input_enabled():
    """True while a password field has taken secure input — synthetic
    keystrokes cannot reach the focused app in that state."""
    try:
        import ctypes
        import ctypes.util
        carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))
        carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        return False
