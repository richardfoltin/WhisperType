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
from Foundation import NSMakeRect, NSObject, NSOperationQueue, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration

from ..icon import png_bytes
from ..jobs import JobStatus
from ..log import log

OV_W = 380
HTML_PATH = Path(__file__).with_name("overlay.html")


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

    def exitSelected_(self, sender):
        self._ui.app.quit()


# ── UI ───────────────────────────────────────────────────────────────────────

class AppKitUI:
    def __init__(self, app):
        self.app = app
        self.visible = False
        self.history_mode = False

        self._pos = None            # user-dragged position, screen coords
        self._height = 120
        self._ready = False
        self._pending = []          # JS calls queued until the page loads
        self._last_level = 0.0
        self._tray_state = None
        self._gpu_timer = None

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
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        # Panels hide when their app deactivates; ours is never active.
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
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

        panel.setContentView_(web)
        content = panel.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(8.0)
        content.layer().setMasksToBounds_(True)

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
        elif action == "height":
            self._set_height(int(msg["height"]))
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

    # ── Geometry ──

    def _default_origin(self):
        vf = NSScreen.mainScreen().visibleFrame()
        x = vf.origin.x + (vf.size.width - OV_W) / 2.0
        y = vf.origin.y + vf.size.height - self._height - 20
        return x, y

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
        return "idle"

    def _hint(self, mode):
        k = self.app.ptt_label
        if mode == "recording":
            return f"Transcribe: {k} / Enter↵ / {self.app.cfg.silence_duration:.0f}s silence  |  History: Space  |  Hide: Esc"
        if mode == "history":
            return "Record: Space  |  Hide: Esc"
        return f"Record: Double {k}  |  History: Space  |  Hide: Esc"

    def _push_state(self, rec_start=None, target=None):
        mode = self._mode()
        state = {
            "mode": mode,
            "model": self.app.model_name,
            "target": target if target is not None else "",
            "hint": self._hint(mode),
            "gpuAvailable": bool(self.app.backend.gpu_available),
            "gpuLabel": self.app.backend.gpu_label,
        }
        if rec_start is not None:
            state["recStart"] = int(rec_start * 1000)
        self._js(f"wt.setState({_json(state)}); wt.setMode({_json(mode)});")

    def _push_queue(self):
        items = [{
            "id": j.job_id,
            "ts": time.strftime("%H:%M:%S", time.localtime(j.created_at)),
            "dur": f"{j.audio_duration:.1f}s",
            "app": (j.app_name[:8] if j.app_name else "?"),
            "window": j.window_name,
            "status": "transcribing" if j.status == JobStatus.TRANSCRIBING else "waiting",
        } for j in self.app.jobs.active()]
        self._js(f"wt.setQueue({_json(items)});")

    def _push_history(self):
        entries = self.app.jobs.history()
        items = [dict(e, index=i) for i, e in enumerate(entries)]
        self._js(f"wt.setHistory({_json(items)});")

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
        if self.app.recording or self.app.jobs.busy() or self.history_mode:
            self._push_state()
            self._push_queue()
            if self.history_mode:
                self._push_history()
            self._show()
        else:
            self.hide()

    def check_hide(self):
        if not self.app.jobs.busy() and not self.app.recording and not self.history_mode:
            self.hide()

    def set_history_mode(self, on):
        self.history_mode = on
        if on:
            self._push_history()
        self._push_state()
        self._push_queue()
        self._show()

    def _show(self):
        if not self._pos:
            x, y = self._default_origin()
            self._panel.setFrame_display_(NSMakeRect(x, y, OV_W, self._height), True)
        # orderFrontRegardless shows the panel without activating the app.
        self._panel.orderFrontRegardless()
        self._panel.setAlphaValue_(0.93)
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

    def _rebuild_menu(self):
        menu = NSMenu.alloc().init()
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("WhisperType", None, "")
        header.setEnabled_(False)
        menu.addItem_(header)
        menu.addItem_(NSMenuItem.separatorItem())

        models_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Model", None, "")
        submenu = NSMenu.alloc().init()
        for info in self.app.model_catalog():
            label = f"{info.name}   {info.size_label}"
            if not info.downloaded:
                label = "↓ " + label
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "modelSelected:", "")
            item.setTarget_(self._bridge)
            item.setRepresentedObject_(info.name)
            item.setState_(1 if info.name == self.app.model_name else 0)
            submenu.addItem_(item)
        models_item.setSubmenu_(submenu)
        menu.addItem_(models_item)

        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Exit", "exitSelected:", "")
        quit_item.setTarget_(self._bridge)
        menu.addItem_(quit_item)

        self._status.setMenu_(menu)
        self._apply_tray_state(self._tray_state or "idle")

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
