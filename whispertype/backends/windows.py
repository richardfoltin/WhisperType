"""Windows backend — Win32 SendInput + foreground window handling.

Lifted from the original single-file whispertype.pyw with no behavioural
changes; only the overlay-skipping hook is now injected instead of reaching
for a global `overlay` object.
"""
import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

from ..log import log
from .base import Backend

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
VK_RETURN = 0x0D

#: Modifiers that must not be physically held while the transcript is typed.
#: The push-to-talk key is itself a modifier, so without this a dictation
#: finished the instant the model was warm turns "a" into Ctrl+A.
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)   # Shift, Ctrl, Alt, L/R Win

#: Stamped into every synthetic event's dwExtraInfo, so a keyboard hook (ours
#: or anybody's) can tell our typing apart from the user's. The macOS backend
#: writes the same value into kCGEventSourceUserData.
EVENT_MARKER = 0x57485459  # 'WHTY'


class MOUSEINPUT(ctypes.Structure):
    # dwExtraInfo is a ULONG_PTR, which is what WPARAM is; declaring it as a
    # pointer type made it impossible to store the marker value in it.
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", wt.WPARAM)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", wt.WPARAM)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", wt.DWORD), ("_u", _INPUT_UNION)]


#: One SendInput call per this many characters. A 300 s dictation is tens of
#: thousands of INPUT structs, and a number of applications drop the tail of a
#: single huge batch rather than queueing it.
TYPE_CHUNK_CHARS = 200

#: Separate handle so ctypes preserves GetLastError for the SendInput checks
#: (ctypes.windll does not).
_user32_le = ctypes.WinDLL("user32", use_last_error=True)


def _send_inputs(events):
    """Send one batch, reporting short writes instead of losing them silently."""
    n = len(events)
    if n == 0:
        return 0
    sent = _user32_le.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))
    if sent != n:
        log(f"SendInput: {sent}/{n} events accepted (GetLastError={ctypes.get_last_error()})")
    return sent


# ── Integrity levels ─────────────────────────────────────────────────────────
#
# User Interface Privilege Isolation drops synthetic input sent from a lower
# integrity level to a higher one — SendInput still reports every event as
# accepted, and the keystrokes simply never arrive. That is the Windows
# equivalent of macOS's secure-input state: the one case where a transcript can
# vanish with nothing to show for it. Detect it and keep the text in history.

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)
_advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]
_advapi32.OpenProcessToken.restype = wt.BOOL
_advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                       ctypes.POINTER(wt.HANDLE)]
_advapi32.GetTokenInformation.restype = wt.BOOL
_advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD)]
# Without explicit types a HANDLE round-trips through a 32-bit int, which
# truncates the pseudo handle GetCurrentProcess returns and every real handle
# above 4 GB — the lookup then fails silently and reports "unknown".
_kernel32.GetCurrentProcess.restype = wt.HANDLE
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


#: Distinguishes "not looked up yet" from a lookup that returned None.
_UNKNOWN = object()


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def _token_integrity(token):
    """The RID of a token's integrity level, or None if it cannot be read."""
    size = wt.DWORD(0)
    _advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                                  ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf,
                                         size.value, ctypes.byref(size)):
        return None
    label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
    count = _advapi32.GetSidSubAuthorityCount(label.Label.Sid)
    if not count:
        return None
    # The integrity RID is always the last sub-authority of S-1-16-x.
    return int(_advapi32.GetSidSubAuthority(label.Label.Sid,
                                            count.contents.value - 1).contents.value)


def _process_integrity(pid=None):
    """Integrity RID of `pid` (default: this process), or None when unknown.

    None means "could not tell", never "not elevated" — a guess in either
    direction is worse than staying quiet.
    """
    handle = token = None
    owned = False
    try:
        if pid is None:
            # Pseudo handle — never closed, which is why `owned` gates the
            # CloseHandle below rather than the handle being truthy.
            handle = _kernel32.GetCurrentProcess()
        else:
            handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                           False, int(pid))
            owned = True
            if not handle:
                return None
        token = wt.HANDLE()
        if not _advapi32.OpenProcessToken(handle, TOKEN_QUERY,
                                          ctypes.byref(token)):
            return None
        return _token_integrity(token)
    except Exception:
        return None
    finally:
        if token:
            _kernel32.CloseHandle(token)
        if handle and owned:
            _kernel32.CloseHandle(handle)


class WindowsBackend(Backend):
    name = "windows"
    gpu_label = "GPU"

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._own_windows = lambda: ()
        self._integrity = _UNKNOWN
        self._nvml = None
        self._gpu_handle = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml = pynvml
            self.gpu_available = True
            log(f"NVML initialized: {pynvml.nvmlDeviceGetName(self._gpu_handle)}")
        except Exception as e:
            log(f"NVML not available (GPU graph disabled): {e}")

    def set_own_window_provider(self, fn):
        self._own_windows = fn

    # ── Target window ──

    def capture_target(self):
        """Foreground window, skipping our own overlay (which may hold focus
        in history mode) by walking down the Z-order.

        Returns None when the overlay is in front and nothing behind it
        qualifies — the caller must treat that as "no target" rather than
        typing into whatever happens to be there.
        """
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        own = tuple(self._own_windows())
        if hwnd in own:
            GW_HWNDNEXT = 2
            candidate = user32.GetWindow(hwnd, GW_HWNDNEXT)
            while candidate:
                if (candidate not in own
                        and user32.GetParent(candidate) == 0
                        and user32.IsWindowVisible(candidate)
                        and user32.GetWindowTextLengthW(candidate) > 0):
                    return candidate
                candidate = user32.GetWindow(candidate, GW_HWNDNEXT)
            log("capture_target: overlay is in front and no window behind it")
            return None
        return hwnd

    def target_title(self, target):
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetWindowTextW(target, buf, 256)
        title = buf.value.strip()
        return (title[:20] + "…") if len(title) > 20 else (title or "(untitled)")

    def target_pid(self, target):
        pid = wt.DWORD()
        self._user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
        return int(pid.value)

    def _image_path(self, target):
        """Full path of the executable owning `target`, or "" if unavailable."""
        try:
            h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                      self.target_pid(target))
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(4096)
                size = wt.DWORD(4096)
                if not _kernel32.QueryFullProcessImageNameW(
                        h, 0, buf, ctypes.byref(size)):
                    return ""
                return buf.value.strip()
            finally:
                _kernel32.CloseHandle(h)
        except Exception:
            return ""

    def target_app(self, target):
        path = self._image_path(target)
        return Path(path).stem if path else "?"

    def target_bundle(self, target):
        # The executable's path is the Windows analogue of a bundle id: stable
        # per application, and the thing the icon is read out of.
        return self._image_path(target) if target is not None else ""

    def activate(self, target):
        user32 = self._user32
        if not user32.IsWindow(target):
            log(f"activate: HWND {target} is no longer valid")
            return False
        SW_RESTORE = 9
        if user32.IsIconic(target):
            user32.ShowWindow(target, SW_RESTORE)
        elif user32.GetForegroundWindow() == target:
            # Already there. Skipping the synthetic Alt below matters: a bare
            # Alt press activates the target application's menu bar.
            return True
        # Press+release Alt so SetForegroundWindow is allowed from background
        alt_down = INPUT()
        alt_down.type = INPUT_KEYBOARD
        alt_down.ki.wVk = VK_MENU
        alt_down.ki.dwFlags = 0
        alt_up = INPUT()
        alt_up.type = INPUT_KEYBOARD
        alt_up.ki.wVk = VK_MENU
        alt_up.ki.dwFlags = KEYEVENTF_KEYUP
        arr = (INPUT * 2)(alt_down, alt_up)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        for attempt in range(2):
            user32.SetForegroundWindow(target)
            for _ in range(10):  # poll up to ~500 ms
                time.sleep(0.05)
                if user32.GetForegroundWindow() == target:
                    return True
            if attempt == 0:
                user32.BringWindowToTop(target)
                log(f"activate: retry with BringWindowToTop for HWND {target}")
        log(f"activate: FAILED to activate HWND {target} after retries")
        return False

    # ── Input synthesis ──

    def preflight_typing(self, target):
        """Refuse to type into a window Windows will not deliver keystrokes to.

        UIPI silently drops synthetic input aimed at a process running at a
        higher integrity level — SendInput reports success and nothing arrives.
        Without this the transcript disappeared with no error at all, which is
        the one failure mode the error surface exists to prevent.
        """
        if target is None:
            return
        try:
            theirs = _process_integrity(self.target_pid(target))
        except Exception:
            return
        if theirs is None:
            return
        ours = self._own_integrity()
        # Unknown on either side: say nothing. A false alarm here would send
        # every transcript to history for no reason.
        if ours is None or theirs <= ours:
            return
        raise PermissionError(
            f"{self.target_app(target)} runs elevated and Windows blocks "
            f"keystrokes from a normal app into it")

    def _own_integrity(self):
        if self._integrity is _UNKNOWN:
            self._integrity = _process_integrity()
            log(f"Own integrity level: {self._integrity}")
        return self._integrity

    def _wait_modifiers_released(self, timeout=1.5):
        """The push-to-talk key is a modifier. Typing while it is still
        physically held turns the transcript into keyboard shortcuts."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(self._user32.GetAsyncKeyState(vk) & 0x8000
                       for vk in _MODIFIER_VKS):
                return True
            time.sleep(0.02)
        log("Modifiers still held after 1.5s — typing anyway")
        return False

    def type_text(self, text):
        self._wait_modifiers_released()
        sent = expected = 0
        for start in range(0, len(text), TYPE_CHUNK_CHARS):
            # Iterate UTF-16 code units, not characters: wScan is a WORD, so a
            # non-BMP character has to go out as its two surrogate halves
            # rather than being truncated by ord().
            units = text[start:start + TYPE_CHUNK_CHARS].encode("utf-16-le")
            events = []
            for i in range(0, len(units), 2):
                code = units[i] | (units[i + 1] << 8)
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                    inp = INPUT()
                    inp.type = INPUT_KEYBOARD
                    inp.ki.wVk = 0
                    inp.ki.wScan = code
                    inp.ki.dwFlags = flags
                    inp.ki.time = 0
                    inp.ki.dwExtraInfo = EVENT_MARKER
                    events.append(inp)
            expected += len(events)
            sent += _send_inputs(events)
            if start + TYPE_CHUNK_CHARS < len(text):
                time.sleep(0.005)   # let the target's message pump drain
        if sent != expected:
            raise RuntimeError(
                f"only {sent}/{expected} keystroke events were delivered")

    def send_enter(self):
        events = []
        for flags in (0, KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki.wVk = VK_RETURN
            inp.ki.wScan = 0
            inp.ki.dwFlags = flags
            inp.ki.time = 0
            inp.ki.dwExtraInfo = EVENT_MARKER
            events.append(inp)
        _send_inputs(events)

    # ── Metrics ──

    def gpu_percent(self):
        if not self._nvml:
            return None
        try:
            return float(self._nvml.nvmlDeviceGetUtilizationRates(self._gpu_handle).gpu)
        except Exception:
            return None
