"""macOS UI — menu-bar item + floating overlay.

The overlay is a borderless, NON-ACTIVATING NSPanel hosting a WKWebView. Two
constraints drive that choice:

  * The app re-focuses the user's target window to type into it, so the overlay
    must never take focus. `NSWindowStyleMaskNonactivatingPanel` plus a
    `canBecomeKeyWindow` override plus `orderFrontRegardless()` guarantees it.
    (This is also why tkinter is unusable here: Tk's XMapWindow calls
    `[NSApp activateIgnoringOtherApps:]` on every window map.)
  * AppKit owns the main thread, so the status item and the overlay share one
    NSApplication run loop instead of the Windows build's tray-on-a-thread.

Every method below may be called from any thread; the ones the worker/recorder
threads reach for marshal themselves onto the main queue.
"""
import base64
import os
import subprocess
import time
from pathlib import Path

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSColor, NSData, NSImage, NSMenu, NSMenuItem, NSPanel, NSPasteboard,
    NSPasteboardTypeString, NSScreen, NSStatusBar, NSVariableStatusItemLength,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel, NSStatusWindowLevel,
)
from AppKit import (NSEvent, NSPopUpMenuWindowLevel,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSAlert, NSAlertFirstButtonReturn, NSAppearance,
                    NSBitmapImageRep, NSCompositingOperationSourceOver,
                    NSPNGFileType, NSSecureTextField, NSViewHeightSizable,
                    NSViewWidthSizable, NSVisualEffectBlendingModeBehindWindow,
                    NSVisualEffectMaterialHUDWindow, NSVisualEffectStateActive,
                    NSVisualEffectView, NSWorkspace, NSZeroRect)
from Foundation import NSMakeRect, NSObject, NSOperationQueue, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration

from .. import __version__
from ..config import API_KEY_PATH
from ..icon import png_bytes
from ..jobs import JobStatus
from ..log import LOG_PATH, log

OV_W = 380

#: Fixed panel heights. Sizing to content meant expanding a history row or
#: showing a two-line error resized the window under the pointer; two stable
#: sizes are calmer and still proportionate — a recording HUD should not be as
#: tall as a 50-entry archive.
OV_H_COMPACT = 212      # idle / recording / transcribing / notice / error
OV_H_QUEUE = 300        # transcribing with jobs actually queued behind it
OV_H_HISTORY = 470

HTML_PATH = Path(__file__).with_name("overlay.html")

from .common import ENGINES, IDLE_CHOICES, LANGUAGES


_ICON_CACHE = {}


def _app_icon_uri(bundle_id):
    """The target app's icon as a data URI, for the history rows.

    Cached per bundle id — a cold cache with a dozen distinct apps would
    otherwise do a dozen disk lookups on the main thread in one go.
    """
    if not bundle_id:
        return None
    if bundle_id in _ICON_CACHE:
        return _ICON_CACHE[bundle_id]
    uri = None
    try:
        ws = NSWorkspace.sharedWorkspace()
        url = ws.URLForApplicationWithBundleIdentifier_(bundle_id)
        if url is not None:
            src = ws.iconForFile_(url.path())
            out = NSImage.alloc().initWithSize_((32, 32))       # 16pt @2x
            out.lockFocus()
            src.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(0, 0, 32, 32), NSZeroRect,
                NSCompositingOperationSourceOver, 1.0)
            out.unlockFocus()
            rep = NSBitmapImageRep.alloc().initWithData_(out.TIFFRepresentation())
            png = bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))
            uri = "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception as e:
        log(f"Could not load the icon for {bundle_id}: {e}")
    _ICON_CACHE[bundle_id] = uri
    return uri


def _input_device_names():
    try:
        import sounddevice as sd
        seen, names = set(), []
        for dev in sd.query_devices():
            name = dev["name"]
            if dev["max_input_channels"] > 0 and name not in seen:
                seen.add(name)
                names.append(name)
        return names
    except Exception as e:
        log(f"Could not enumerate input devices: {e}")
        return []


# ── AppKit subclasses ────────────────────────────────────────────────────────

class OverlayPanel(NSPanel):
    """A panel that can never become key, so showing it never moves focus."""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class OverlayWebView(WKWebView):
    def acceptsFirstMouse_(self, event):
        # Without this the first click on a button is swallowed whenever
        # another application is frontmost — which is always, here.
        return True


class Bridge(NSObject):
    """WKScriptMessageHandler + menu target. Holds the UI weakly enough that
    PyObjC does not fight Python's GC."""

    def initWithUI_(self, ui):
        self = objc.super(Bridge, self).init()
        if self is None:
            return None
        self._ui = ui
        return self

    # WKScriptMessageHandler
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            self._ui._on_js(dict(message.body()))
        except Exception as e:
            log(f"overlay bridge error: {e}")

    # Menu actions
    def modelSelected_(self, sender):
        self._ui.app.request_model(str(sender.representedObject()))

    def languageSelected_(self, sender):
        # Read fresh per job (app._handle_job), so this needs no restart.
        self._ui.app.cfg.set("language", str(sender.representedObject()))
        self._ui.app.cfg.save()
        self._ui.refresh_tray()

    def micSelected_(self, sender):
        # Resolved per recording (audio.record_until_stop), also no restart.
        value = sender.representedObject()
        self._ui.app.cfg.set("input_device", None if value is None else str(value))
        self._ui.app.cfg.save()
        self._ui.refresh_tray()

    def engineSelected_(self, sender):
        self._ui.app.request_engine(str(sender.representedObject()))

    def apiKeySelected_(self, sender):
        self._ui.ask_api_key()

    def idleSelected_(self, sender):
        self._ui.app.set_idle_unload(int(sender.representedObject()))
        self._ui.refresh_tray()

    def downloadAllSelected_(self, sender):
        self._ui.app.download_all_models()

    def benchmarkSelected_(self, sender):
        self._ui.app.toggle_benchmark_next()
        self._ui.refresh_tray()

    def openBenchmarkSelected_(self, sender):
        self._ui.app.open_last_benchmark()

    def historySelected_(self, sender):
        self._ui.app.show_history()

    def pauseSelected_(self, sender):
        self._ui.app.set_paused(not self._ui.app.paused)

    def openLogSelected_(self, sender):
        self._ui.open_log()

    def restartSelected_(self, sender):
        self._ui.restart()

    def exitSelected_(self, sender):
        self._ui.app.quit()


# ── UI ───────────────────────────────────────────────────────────────────────

class AppKitUI:
    #: The benchmark results table is Tk-only — this overlay is an HTML page
    #: with no per-model rows, so the tray items are not offered here either.
    #: App.benchmark_supported() reads this; the two hooks below stay no-ops so
    #: the shared worker can call them without platform checks.
    supports_benchmark = False

    def show_benchmark(self, job):
        pass

    def refresh_benchmark(self):
        pass

    def __init__(self, app):
        self.app = app
        self.visible = False
        self.history_mode = False

        self._pos = None            # user-dragged position, screen coords
        self._height = OV_H_COMPACT
        self._ready = False
        self._pending = []          # JS calls queued until the page loads
        self._last_level = 0.0
        self._tray_state = None
        self._gpu_timer = None
        self._notice = None         # transient message, e.g. "Nothing heard"
        self._loading = None        # model name while it is being loaded
        self._reduce_motion = False

        self._ns = NSApplication.sharedApplication()
        # Accessory: menu-bar only, no Dock icon, never becomes frontmost.
        self._ns.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        self._bridge = Bridge.alloc().initWithUI_(self)
        self._build_panel()
        self._build_status_item()

    # ── Construction ──

    def _build_panel(self):
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        rect = NSMakeRect(0, 0, OV_W, self._height)
        panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        # Above a full-screen app's content. NSStatusWindowLevel (25) is not:
        # a full-screen Space puts its window above it, so the overlay was
        # invisible whenever you dictated from anything running full screen.
        panel.setLevel_(NSPopUpMenuWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        # Panels hide when their app deactivates; ours is never active.
        panel.setHidesOnDeactivate_(False)
        # CanJoinAllSpaces puts it on every Space including full-screen ones;
        # FullScreenAuxiliary is what allows it over another app's full-screen
        # window. Stationary is deliberately gone — it is the "pinned to the
        # desktop, unaffected by Exposé" behaviour, which fought Space joining.
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setAlphaValue_(0.0)
        panel.setReleasedWhenClosed_(False)

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.userContentController().addScriptMessageHandler_name_(self._bridge, "wt")
        web = OverlayWebView.alloc().initWithFrame_configuration_(rect, cfg)
        web.setValue_forKey_(False, "drawsBackground")
        try:
            web.setInspectable_(True)     # macOS 13.3+; harmless if it fails
        except Exception:
            pass

        # A real material instead of window alpha. Fading the whole window to
        # 0.93 made the desktop show through the *letterforms*; macOS HUDs put
        # an NSVisualEffectView behind opaque text instead.
        vfx = NSVisualEffectView.alloc().initWithFrame_(rect)
        vfx.setMaterial_(NSVisualEffectMaterialHUDWindow)
        vfx.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        # Load-bearing: we are an accessory app and never frontmost, so the
        # default FollowsWindowActiveState would render the material flat.
        vfx.setState_(NSVisualEffectStateActive)
        vfx.setWantsLayer_(True)
        vfx.layer().setCornerRadius_(16.0)      # 8pt is Big Sur-era; a real
        try:                                    # popover measures 12-13pt
            vfx.layer().setCornerCurve_("continuous")   # squircle, not a circle
        except Exception:
            pass
        vfx.layer().setMasksToBounds_(True)

        web.setFrame_(rect)
        web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        vfx.addSubview_(web)
        panel.setContentView_(vfx)

        web.loadHTMLString_baseURL_(HTML_PATH.read_text(encoding="utf-8"), None)

        self._panel = panel
        self._web = web

    def _build_status_item(self):
        bar = NSStatusBar.systemStatusBar()
        self._status = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.set_tray_state("idle")
        self.refresh_tray()

    # ── Thread marshalling ──

    def call_soon(self, fn):
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    def call_later(self, ms, fn):
        def schedule():
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                ms / 1000.0, False, lambda timer: fn())
        self.call_soon(schedule)

    # ── JS bridge ──

    def _js(self, script):
        if not self._ready:
            self._pending.append(script)
            return
        self._web.evaluateJavaScript_completionHandler_(script, None)

    def _on_js(self, msg):
        action = msg.get("action")
        if action == "ready":
            self._ready = True
            for script in self._pending:
                self._web.evaluateJavaScript_completionHandler_(script, None)
            self._pending = []
            self._apply_a11y()
            self._apply_appearance()
        elif action == "height":
            pass        # the panel is a fixed size per mode; see _push_state
        elif action == "drag":
            self._drag(float(msg["dx"]), float(msg["dy"]))
        elif action == "hide":
            self.hide()
        elif action == "cancelJob":
            job_id = int(msg["id"])
            for job in self.app.jobs.active():
                if job.job_id == job_id:
                    self.app.cancel_job(job)
                    break
        elif action == "copy":
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(msg.get("text", ""), NSPasteboardTypeString)
        elif action == "deleteHistory":
            self.app.jobs.delete_history(int(msg["index"]))
            self._push_history()
            self.refresh()
        elif action == "clearHistory":
            self.app.jobs.clear_history()
            self._push_history()
            self.refresh()

    # ── Geometry ──

    def _active_screen(self):
        """The screen the user is actually working on.

        NSScreen.mainScreen() is the screen holding the key window — and this
        app never has one, so it can name a display the user is not looking at.
        The pointer is the honest signal.
        """
        try:
            point = NSEvent.mouseLocation()
            for screen in NSScreen.screens():
                frame = screen.frame()
                if (frame.origin.x <= point.x <= frame.origin.x + frame.size.width
                        and frame.origin.y <= point.y <= frame.origin.y + frame.size.height):
                    return screen
        except Exception:
            pass
        return NSScreen.mainScreen()

    def _default_origin(self, screen=None):
        vf = (screen or self._active_screen()).visibleFrame()
        x = vf.origin.x + (vf.size.width - OV_W) / 2.0
        y = vf.origin.y + vf.size.height - self._height - 20
        return x, y

    def _place(self):
        """Position the panel for this appearance.

        A dragged position is kept only while it is still on the screen the
        user is on; otherwise the panel would sit on a display — or beyond an
        edge — where it cannot be seen, which looks exactly like the app not
        having started.
        """
        screen = self._active_screen()
        vf = screen.visibleFrame()
        if self._pos:
            x, top = self._pos
            y = top - self._height
            if (vf.origin.x - 40 <= x <= vf.origin.x + vf.size.width - 40
                    and vf.origin.y <= y <= vf.origin.y + vf.size.height):
                self._panel.setFrame_display_(NSMakeRect(x, y, OV_W, self._height), True)
                return
        x, y = self._default_origin(screen)
        self._panel.setFrame_display_(NSMakeRect(x, y, OV_W, self._height), True)

    def _set_height(self, h):
        if h == self._height:
            return
        frame = self._panel.frame()
        top = frame.origin.y + frame.size.height
        self._height = h
        if self._pos:
            x, top = self._pos[0], self._pos[1]
        else:
            x = frame.origin.x
            if not self.visible:
                x, y0 = self._default_origin()
                top = y0 + h
        self._panel.setFrame_display_(NSMakeRect(x, top - h, OV_W, h), True)
        # Without this the drop shadow keeps the silhouette of the previous
        # frame, which is visible at every height change.
        self._panel.invalidateShadow()

    def _drag(self, dx, dy):
        frame = self._panel.frame()
        x = frame.origin.x + dx
        y = frame.origin.y - dy          # web y grows downward, AppKit upward
        self._pos = (x, y + frame.size.height)
        self._panel.setFrameOrigin_((x, y))

    # ── State pushed to the page ──

    def _mode(self):
        if self.app.recording:
            return "recording"
        if self.history_mode:
            return "history"
        if self.app.jobs.busy():
            return "transcribing"
        if self.app.last_error:
            return "error"
        if self._notice:
            return "notice"
        if self._loading:
            return "loading"
        return "idle"

    def _hint(self, mode):
        """Structured key/label pairs — the page renders them as keycaps
        instead of a pipe-separated command line."""
        k = self.app.ptt_label
        if mode == "recording":
            return [{"key": k, "label": "Transcribe"},
                    {"key": "↩", "label": "Transcribe + Enter"},
                    {"key": "⎋", "label": "Discard"}]
        if mode == "history":
            return [{"key": "⎋", "label": "Hide"}]
        if mode == "error":
            return [{"key": "⎋", "label": "Dismiss"}]
        return [{"key": k, "label": "Double-tap to record"},
                {"key": "⎋", "label": "Hide"}]

    def _push_state(self, rec_start=None, target=None):
        mode = self._mode()
        message = ""
        if mode == "error":
            message = str(self.app.last_error)
        elif mode == "notice":
            message = self._notice
        elif mode == "loading":
            message = f"Loading {self._loading}…"
        state = {
            "mode": mode,
            "model": self.app.model_name,
            "target": target if target is not None else "",
            "hint": self._hint(mode),
            "message": message,
            "theme": self.app.cfg.theme,
            "silenceHold": self.app.cfg.silence_duration,
            "gpuGraph": bool(self.app.cfg.get("show_gpu_graph", False)),
            "gpuAvailable": bool(self.app.backend.gpu_available),
            "gpuLabel": self.app.backend.gpu_label,
        }
        if rec_start is not None:
            state["recStart"] = int(rec_start * 1000)
        self._js(f"wt.setState({_json(state)}); wt.setMode({_json(mode)});")
        # Transcribing is the same size as recording — the header and the stage
        # line up exactly — and only grows when a queue actually exists behind
        # the job in flight.
        if mode == "history":
            height = OV_H_HISTORY
        elif mode == "transcribing" and self.app.jobs.active_count() > 1:
            height = OV_H_QUEUE
        else:
            height = OV_H_COMPACT
        self._set_height(height)

    def set_ticker(self, text):
        """Show the finished transcript running in from the right."""
        self.call_soon(lambda: self._js(f"wt.setTicker({_json(text or '')});"))

    # ── Transient states ──

    def on_capture_started(self):
        """Called from the recorder thread the moment audio actually flows.

        Opening the CoreAudio stream takes long enough that starting the timer
        at the keypress made the overlay claim to be recording before it was.
        """
        self.call_soon(lambda: self._js(
            f"wt.setState({{\"recStart\": {int(time.time() * 1000)}}});"))

    def show_notice(self, text, seconds=1.6):
        """Briefly show a message (e.g. 'Nothing heard') and then fall back to
        whatever state the app is really in."""
        def run():
            self._notice = text
            self._push_state()
            self._show()

            def clear():
                self._notice = None
                self._refresh_main()
            self.call_later(int(seconds * 1000), clear)
        self.call_soon(run)

    def show_loading(self, model_name):
        def run():
            self._loading = model_name
            self._push_state()
            self._show()
        self.call_soon(run)

    def clear_loading(self):
        self._loading = None

    def _push_queue(self):
        # Raw values: the page formats. The old 8-character truncation chopped
        # app names mid-word regardless of the space available; text-overflow
        # does it properly.
        items = [{
            "id": j.job_id,
            "at": int(j.created_at),
            "seconds": float(j.audio_duration),
            "app": j.app_name or "?",
            "window": j.window_name,
            "status": "transcribing" if j.status == JobStatus.TRANSCRIBING else "waiting",
        } for j in self.app.jobs.active()]
        self._js(f"wt.setQueue({_json(items)});")

    def _push_history(self):
        entries = self.app.jobs.history()
        items = [dict(e, index=i) for i, e in enumerate(entries)]
        # Real app icons are what make the list read as a native table rather
        # than a web list. Cached per bundle id; only resolved on demand.
        icons = {}
        for entry in entries:
            bundle = entry.get("bundle")
            if bundle and bundle not in icons:
                uri = _app_icon_uri(bundle)
                if uri:
                    icons[bundle] = uri
        self._js(f"wt.setAppIcons({_json(icons)}); wt.setHistory({_json(items)});")

    def _apply_appearance(self):
        """Keep the material and the page's colour scheme in the same
        appearance — otherwise a pinned theme only affects the HTML."""
        name = {"light": "NSAppearanceNameAqua",
                "dark": "NSAppearanceNameDarkAqua"}.get(self.app.cfg.theme)
        self._panel.setAppearance_(
            NSAppearance.appearanceNamed_(name) if name else None)

    def _apply_a11y(self):
        """prefers-reduced-transparency parses but never evaluates in this
        WKWebView, so the accessibility flags have to come from NSWorkspace.
        NSVisualEffectView honours Reduce Transparency by itself; this is only
        so the page's own plates go opaque to match."""
        try:
            ws = NSWorkspace.sharedWorkspace()
            self._reduce_motion = bool(ws.accessibilityDisplayShouldReduceMotion())
            self._js(
                "var d=document.documentElement.dataset;"
                f"d.reduceTransparency='{int(ws.accessibilityDisplayShouldReduceTransparency())}';"
                f"d.increaseContrast='{int(ws.accessibilityDisplayShouldIncreaseContrast())}';"
                f"d.reduceMotion='{int(self._reduce_motion)}';")
        except Exception as e:
            log(f"Could not read accessibility settings: {e}")

    def _push_gpu(self):
        series = self.app.gpu_series()
        pct = int(round(series[-1] * 100)) if series else None
        self._js(f"wt.setGpu({_json(series)}, {_json(pct)});")

    # ── Public interface used by App ──

    def own_window_ids(self):
        # macOS targets applications by pid; the backend skips our own process,
        # so there is nothing to report here.
        return ()

    def push_level(self, rms):
        now = time.monotonic()
        if now - self._last_level < 0.05:      # cap the bridge at ~20 Hz
            return
        self._last_level = now
        thr = self.app.cfg.silence_threshold
        self.call_soon(lambda: self._js(f"wt.setLevel({rms:.1f}, {thr}, 4000);"))

    def show_recording(self, target_name=""):
        self.history_mode = False
        self._push_state(rec_start=time.time(), target=target_name)
        self._push_queue()
        self._show()

    def on_recording_stopped(self):
        if self.app.jobs.busy():
            self._push_state()
            self._push_queue()
            self._show()
        else:
            self.hide()

    def refresh(self):
        self.call_soon(self._refresh_main)

    def _refresh_main(self):
        if (self.app.recording or self.app.jobs.busy() or self.history_mode
                or self.app.last_error or self._notice or self._loading):
            self._push_state()
            self._push_queue()
            if self.history_mode:
                self._push_history()
            self._show()
        else:
            self.hide()

    def check_hide(self):
        # An unacknowledged error keeps the overlay up: it is the only place
        # the reason is written down.
        if (not self.app.jobs.busy() and not self.app.recording
                and not self.history_mode and not self.app.last_error
                and not self._notice and not self._loading):
            self.hide()

    def set_history_mode(self, on):
        self.history_mode = on
        if on:
            self._push_history()
        self._push_state()
        self._push_queue()
        self._show()

    def _show(self):
        # Re-placed on every appearance, not only the first: the user may be on
        # a different Space or display than the last time it was shown.
        self._place()
        # orderFrontRegardless shows the panel without activating the app.
        self._panel.orderFrontRegardless()
        # Fully opaque: the NSVisualEffectView provides the translucency now.
        # Window alpha and a material are mutually exclusive ideas — the old
        # 0.93 faded the text along with the background.
        self._panel.setAlphaValue_(1.0)
        self._panel.invalidateShadow()
        self.visible = True
        self._start_gpu_refresh()

    def hide(self):
        self._stop_gpu_refresh()
        self.history_mode = False
        self._panel.setAlphaValue_(0.0)
        self._panel.orderOut_(None)
        self.visible = False

    # ── GPU sparkline refresh ──

    def _start_gpu_refresh(self):
        if not self.app.backend.gpu_available or self._gpu_timer is not None:
            return
        self._push_gpu()
        self._gpu_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            1.0, True, lambda timer: self._push_gpu())

    def _stop_gpu_refresh(self):
        if self._gpu_timer is not None:
            self._gpu_timer.invalidate()
            self._gpu_timer = None

    # ── Menu bar ──

    def set_tray_state(self, state):
        if state == self._tray_state:
            return
        self._tray_state = state
        self.call_soon(lambda: self._apply_tray_state(state))

    def _apply_tray_state(self, state):
        data = NSData.dataWithBytes_length_(png_bytes(state), len(png_bytes(state)))
        img = NSImage.alloc().initWithData_(data)
        img.setSize_((18, 18))
        self._status.button().setImage_(img)

    def refresh_tray(self):
        self.call_soon(self._rebuild_menu)

    def _item(self, menu, title, action=None, obj=None, state=0, enabled=True):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, "")
        if action:
            item.setTarget_(self._bridge)
        if obj is not None:
            item.setRepresentedObject_(obj)
        item.setState_(state)
        item.setEnabled_(enabled)
        menu.addItem_(item)
        return item

    def _submenu(self, menu, title):
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        sub = NSMenu.alloc().init()
        parent.setSubmenu_(sub)
        menu.addItem_(parent)
        return sub

    def _rebuild_menu(self):
        if self.app.model_ready:
            self._loading = None
        app = self.app
        menu = NSMenu.alloc().init()

        # The hotkey used to be written only on the overlay — which you can
        # only open by pressing the hotkey you are trying to look up.
        self._item(menu, f"WhisperType {__version__}", enabled=False)
        self._item(menu, f"Record: double-tap {app.ptt_label}", enabled=False)
        if app.last_error:
            self._item(menu, f"⚠ {str(app.last_error)[:60]}", enabled=False)
        elif not app.model_ready:
            self._item(menu, f"Loading {app.model_name}…", enabled=False)

        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "History…", "historySelected:")
        self._item(menu, "Pause dictation", "pauseSelected:",
                   state=1 if app.paused else 0)

        menu.addItem_(NSMenuItem.separatorItem())

        local = app.engine_kind == "local"

        engines = self._submenu(menu, "Engine")
        for kind, label in ENGINES:
            self._item(engines, label, "engineSelected:", obj=kind,
                       state=1 if kind == app.engine_kind else 0)
        engines.addItem_(NSMenuItem.separatorItem())
        source = app.cfg.api_key_source
        self._item(engines,
                   "Set API key…" if not source else f"Replace API key ({source})…",
                   "apiKeySelected:")

        models = self._submenu(menu, "Model")
        for info in app.model_catalog():
            label = f"{info.name}   {info.size_label}"
            if not info.downloaded:
                label = "↓ " + label
            self._item(models, label, "modelSelected:", obj=info.name,
                       state=1 if info.name == app.model_name else 0)

        langs = self._submenu(menu, "Language")
        current_lang = app.cfg.language
        for code, name in LANGUAGES:
            self._item(langs, name, "languageSelected:", obj=code,
                       state=1 if code == current_lang else 0)

        mics = self._submenu(menu, "Microphone")
        current_mic = app.cfg.input_device
        self._item(mics, "System default", "micSelected:", obj=None,
                   state=1 if not current_mic else 0)
        for name in _input_device_names():
            self._item(mics, name, "micSelected:", obj=name,
                       state=1 if current_mic and str(current_mic) in name else 0)

        # Only meaningful for the local engine — the hosted models live on
        # OpenAI's servers and there is nothing to unload or download.
        if local:
            idle = self._submenu(menu, "Release model when idle")
            for minutes, label in IDLE_CHOICES:
                self._item(idle, label, "idleSelected:", obj=minutes,
                           state=1 if abs(app.cfg.idle_unload_seconds
                                          - minutes * 60) < 1 else 0)

            pending = any(not i.downloaded for i in app.model_catalog())
            self._item(menu, "Download all models", "downloadAllSelected:",
                       enabled=(pending and not app.downloading_all))

        menu.addItem_(NSMenuItem.separatorItem())
        if app.benchmark_supported():
            self._item(menu, "Benchmark next recording", "benchmarkSelected:",
                       state=1 if app.benchmark_next else 0)
            self._item(menu, "Open last benchmark", "openBenchmarkSelected:",
                       enabled=app.last_benchmark is not None)
            menu.addItem_(NSMenuItem.separatorItem())

        self._item(menu, "Open Log", "openLogSelected:")
        self._item(menu, "Restart WhisperType", "restartSelected:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "Exit", "exitSelected:")

        self._status.setMenu_(menu)
        self._apply_tray_state(self._tray_state or "idle")

    # ── Menu actions that touch the system ──

    def ask_api_key(self):
        self.call_soon(self._ask_api_key)

    def _ask_api_key(self):
        """Prompt for the OpenAI key in a standard sheet-less NSAlert.

        The app runs as an accessory with a non-activating panel and normally
        has no key window, so it has to activate itself for the duration of the
        modal — otherwise the secure field cannot receive keystrokes. We hide
        again afterwards so focus goes back where the user left it.
        """
        source = self.app.cfg.api_key_source
        alert = NSAlert.alloc().init()
        alert.setMessageText_("OpenAI API key")
        alert.setInformativeText_(
            (f"A key is already set (from the {source}). Entering a new one "
             f"replaces it.\n\n" if source else "No key is set yet.\n\n")
            + f"The key is stored in {API_KEY_PATH}, readable only by you — "
              f"never in config.json, and never inside the repository.")
        field = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, 320, 24))
        field.setPlaceholderString_("sk-…")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        self._ns.activateIgnoringOtherApps_(True)
        alert.window().makeFirstResponder_(field)
        clicked = alert.runModal()
        key = str(field.stringValue()).strip()
        self._ns.hide_(None)

        if clicked != NSAlertFirstButtonReturn or not key:
            return
        if not key.startswith("sk-"):
            # Cheap sanity check — a mistyped key otherwise only shows up as a
            # 401 on the next dictation.
            self.app.set_error("That does not look like an OpenAI key (sk-…)")
            return
        self.app.set_api_key(key)

    def open_log(self):
        subprocess.Popen(["/usr/bin/open", "-a", "Console", str(LOG_PATH)])

    def restart(self):
        """Ask launchd to restart us. Falls back to a plain exit when the app
        was started by hand, in which case there is nothing to restart into."""
        label = f"gui/{os.getuid()}/com.whispertype.agent"
        try:
            probe = subprocess.run(["/bin/launchctl", "print", label],
                                   capture_output=True)
            if probe.returncode != 0:
                log("Restart requested but no LaunchAgent is loaded — quitting")
                self.app.quit()
                return
            subprocess.Popen(["/bin/launchctl", "kickstart", "-k", label])
        except Exception as e:
            log(f"Restart failed: {e}")

    def stop_tray(self):
        def drop():
            NSStatusBar.systemStatusBar().removeStatusItem_(self._status)
        self.call_soon(drop)

    # ── Main loop ──

    def run(self):
        self._ns.run()

    def destroy(self):
        self._stop_gpu_refresh()
        self._panel.orderOut_(None)
        self._ns.terminate_(None)


def _json(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)
