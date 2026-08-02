"""Application orchestration — shared by the Windows and macOS builds.

Owns the state machine (idle / recording / transcribing / history), the hotkey
listener, the recording thread and the single transcription worker. Everything
platform specific goes through `self.backend` (input synthesis, window
targeting, GPU counter) and `self.ui` (overlay + tray).
"""
import threading
import time

import pynput.keyboard

from . import audio, config
from .jobs import JobQueue, JobStatus, ModelSwitch, TranscriptionJob
from .log import log
from .transcribe import create_engine

DOUBLE_TAP_MS = 400
GPU_WINDOW = 60          # samples kept for the sparkline (1 Hz => 60 s)

KEY_MAP = {
    "ctrl_r":  pynput.keyboard.Key.ctrl_r,
    "ctrl_l":  pynput.keyboard.Key.ctrl_l,
    "shift_r": pynput.keyboard.Key.shift_r,
    "shift_l": pynput.keyboard.Key.shift_l,
    "alt_r":   pynput.keyboard.Key.alt_r,
    "alt_l":   pynput.keyboard.Key.alt_l,
    "cmd_r":   pynput.keyboard.Key.cmd_r,
    "cmd_l":   pynput.keyboard.Key.cmd_l,
}

#: How the push-to-talk key is spelled in the overlay hints.
KEY_LABEL = {
    "ctrl_r": "R-Ctrl", "ctrl_l": "L-Ctrl",
    "shift_r": "R-Shift", "shift_l": "L-Shift",
    "alt_r": "R-Alt", "alt_l": "L-Alt",
    "cmd_r": "R-⌘", "cmd_l": "L-⌘",
}


class App:
    def __init__(self, backend, ui_factory):
        self.cfg = config.load()
        self.backend = backend
        self.jobs = JobQueue()
        self.engine = create_engine()

        self.ptt_key = KEY_MAP.get(self.cfg.ptt_key_name,
                                   pynput.keyboard.Key.ctrl_r)
        self.ptt_label = KEY_LABEL.get(self.cfg.ptt_key_name,
                                       self.cfg.ptt_key_name)

        self.model_name = self.cfg.get("last_model", "large-v3-turbo")
        self.model_ready = False
        self.model_switching = False

        self.recording = False
        self.discard_recording = False
        self.enter_stop = False
        self.stop_event = threading.Event()
        self._last_tap = 0.0
        self.shutting_down = False

        self.target = None
        self.target_title = ""

        # Our own synthetic keystrokes are visible to the global listener. A
        # transcript containing a space would otherwise toggle history mode,
        # and the Enter we send would stop the next recording.
        self._suppress_keys = False

        self.gpu_history = []
        self.gpu_lock = threading.Lock()

        self.ui = ui_factory(self)
        self.backend.set_own_window_provider(self.ui.own_window_ids)
        self._listener = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        if self.backend.gpu_available:
            threading.Thread(target=self._gpu_collector, daemon=True).start()
            log(f"GPU collector started (1s interval, {GPU_WINDOW}s window)")

        threading.Thread(target=self._worker, daemon=True).start()

        self._listener = pynput.keyboard.Listener(on_press=self._on_press)
        self._listener.start()

        log(f"PTT={self.cfg.ptt_key_name} (double-tap) | Model={self.model_name}")

    def run(self):
        self.ui.run()          # blocks on the platform main loop
        if self._listener:
            self._listener.stop()
        log("Daemon stopped.")

    def quit(self):
        """Tray/overlay Exit. Lets in-flight transcriptions finish (they land
        in history) but skips typing them, then tears the UI down."""
        self.shutting_down = True
        if self.recording:
            self.recording = False
            self.stop_event.set()
        self.ui.stop_tray()

        if self.jobs.busy():
            self.ui.call_soon(self.ui.hide)

            def _wait_and_destroy():
                self.jobs.join()
                self.ui.call_soon(self.ui.destroy)

            threading.Thread(target=_wait_and_destroy, daemon=True).start()
        else:
            self.ui.call_soon(self.ui.destroy)

    # ── GPU sampling ─────────────────────────────────────────────────────

    def _gpu_collector(self):
        while not self.shutting_down:
            try:
                pct = self.backend.gpu_percent()
                if pct is not None:
                    with self.gpu_lock:
                        self.gpu_history.append(pct / 100.0)
                        if len(self.gpu_history) > GPU_WINDOW:
                            self.gpu_history.pop(0)
            except Exception:
                pass
            time.sleep(1.0)

    def gpu_series(self):
        with self.gpu_lock:
            return list(self.gpu_history)

    # ── Model handling ───────────────────────────────────────────────────

    def model_catalog(self):
        return self.engine.catalog()

    def request_model(self, name):
        """Called from the tray menu. The switch is queued so it runs on the
        worker thread, after everything already queued has been transcribed."""
        if self.recording or self.model_switching or name == self.model_name:
            return
        self.model_switching = True
        self.model_name = name
        self.cfg.set("last_model", name)
        self.cfg.save()
        if not self.engine.is_downloaded(name):
            self.ui.set_tray_state("downloading")
        self.jobs.submit_control(ModelSwitch(name))

    # ── Transcription worker (the only thread that touches the model) ─────

    def _worker(self):
        try:
            self.engine.load(self.model_name)
            self.model_ready = True
            self.ui.call_soon(self.ui.refresh_tray)
            log("Ready.")
        except Exception as e:
            log(f"Failed to load {self.model_name}: {e}")

        while True:
            item = self.jobs.take()
            try:
                if isinstance(item, ModelSwitch):
                    self._handle_model_switch(item)
                    continue
                self._handle_job(item)
            except Exception as e:
                log(f"Worker error: {e}")
            finally:
                self.jobs.done()

    def _handle_model_switch(self, msg):
        try:
            log(f"Switching model to {msg.name}...")
            self.engine.load(msg.name)
            self.model_ready = True
        except Exception as e:
            log(f"Failed to load {msg.name}: {e}")
        finally:
            self.model_switching = False
            self.ui.call_soon(self.ui.refresh_tray)

    def _handle_job(self, job):
        if job.status == JobStatus.CANCELLED:
            log(f"Skipped cancelled job {job.job_id}")
            return
        try:
            self.jobs.set_status(job, JobStatus.TRANSCRIBING)
            self.ui.call_soon(self.ui.refresh)
            self.ui.set_tray_state("transcribing")

            text = self.engine.transcribe(job.audio_bytes, self.cfg.language)
            log(f"Transcribed (job {job.job_id}, target={job.window_name}): {text}")

            if text:
                self.jobs.add_history({
                    "ts": time.strftime("%H:%M:%S", time.localtime(job.created_at)),
                    "dur": f"{job.audio_duration:.1f}s",
                    "app": job.app_name[:8] if job.app_name else "?",
                    "window": job.window_name,
                    "text": text,
                })
                if self.shutting_down:
                    log(f"Shutdown: kept job {job.job_id} in history, not typing")
                elif job.status == JobStatus.CANCELLED:
                    log(f"Job {job.job_id} cancelled during transcription — history only")
                else:
                    self._deliver(text, job)
        except Exception as e:
            log(f"Transcription error (job {job.job_id}): {e}")
        finally:
            self.jobs.finish(job)
            self.ui.call_soon(self.ui.refresh)
            self.ui.call_later(100, self.ui.check_hide)
            if not self.jobs.busy() and not self.recording:
                self.ui.set_tray_state("idle")

    def _deliver(self, text, job):
        if job.target is not None:
            try:
                activated = self.backend.activate(job.target)
            except Exception as e:
                log(f"activate failed: {e}")
                activated = False
            if not activated:
                log("Could not activate target window — text kept in history only")
                return
        else:
            time.sleep(0.2)
        self._suppress_keys = True
        try:
            self.backend.type_text(text)
            if job.send_enter:
                time.sleep(0.05)
                self.backend.send_enter()
                log(f"Sent Enter after transcription (job {job.job_id})")
        except PermissionError as e:
            log(f"Typing blocked ({e}) — text kept in history only")
        finally:
            # Let the last synthetic events drain past the listener.
            time.sleep(0.15)
            self._suppress_keys = False

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, capture_target=True):
        if self.recording:
            return
        self.recording = True
        self.discard_recording = False
        self.enter_stop = False
        self.stop_event = threading.Event()
        if capture_target:
            try:
                self.target = self.backend.capture_target()
                self.target_title = self.backend.target_title(self.target)
            except Exception as e:
                log(f"Could not capture target window: {e}")
                self.target, self.target_title = None, ""
        name = self.target_title
        self.ui.call_soon(lambda: self.ui.show_recording(name))
        threading.Thread(target=self._record_and_enqueue, daemon=True).start()
        log(f"Recording started, target: {name or '(none)'}")

    def stop_recording(self, discard=False, send_enter=False):
        if not self.recording:
            return
        self.recording = False
        self.discard_recording = discard
        self.enter_stop = send_enter
        self.stop_event.set()

    def _record_and_enqueue(self):
        try:
            data = audio.record_until_stop(self.cfg, self.stop_event,
                                           level_callback=self.ui.push_level)
            target = self.target
            window_name = self.target_title
            self.recording = False

            if not data or self.discard_recording:
                self.discard_recording = False
                self.enter_stop = False
                if not self.ui.history_mode:
                    self.ui.call_soon(self.ui.on_recording_stopped)
                log("Recording discarded" if data else "No audio captured")
                return

            duration = len(data) / (self.cfg.rate * 2)   # 16-bit mono
            try:
                app_name = self.backend.target_app(target) if target is not None else "?"
            except Exception:
                app_name = "?"

            send_enter = self.enter_stop
            self.enter_stop = False
            job = TranscriptionJob(
                job_id=self.jobs.next_id(),
                audio_bytes=data,
                target=target,
                window_name=window_name,
                app_name=app_name,
                audio_duration=duration,
                send_enter=send_enter,
            )

            # Schedule the overlay update before queuing, so a very fast worker
            # cannot clear the job before the UI has processed the transition.
            self.ui.call_soon(self.ui.on_recording_stopped)
            self.jobs.submit(job)
            log(f"Enqueued job {job.job_id} for '{window_name}' ({duration:.1f}s)")
            self.ui.set_tray_state("transcribing")
        except Exception as e:
            log(f"Recording error: {e}")
            self.recording = False
            self.ui.call_soon(self.ui.on_recording_stopped)

    # ── History mode ─────────────────────────────────────────────────────

    def toggle_history(self):
        if self.ui.history_mode:
            self.ui.set_history_mode(False)
            # Leaving history resumes dictation. capture_target() skips our own
            # overlay, so the user's real window is picked up here.
            self.start_recording()
        else:
            if self.recording:
                self.stop_recording(discard=True)
            self.ui.set_history_mode(True)

    def cancel_job(self, job):
        self.jobs.cancel(job)
        log(f"Cancelled job {job.job_id}")
        self.ui.refresh()

    # ── Hotkeys ──────────────────────────────────────────────────────────

    def _on_press(self, key):
        try:
            self._handle_key(key)
        except Exception as e:
            log(f"Hotkey error: {e}")

    def _handle_key(self, key):
        if self._suppress_keys:
            return
        K = pynput.keyboard.Key

        if key == K.space and self.ui.visible:
            self.ui.call_soon(self.toggle_history)
            return

        if key == K.esc and self.ui.visible:
            if self.recording:
                self.stop_recording(discard=True)
            self.ui.call_soon(self.ui.hide)
            return

        if key == K.enter and self.recording:
            self.stop_recording(send_enter=True)
            log("Recording stopped by Enter (will send Enter after transcription)")
            return

        if key != self.ptt_key or not self.model_ready or self.model_switching:
            return

        now = time.time()
        if self.recording:
            self.stop_recording()
            log("Recording stopped by keypress")
        else:
            if (now - self._last_tap) * 1000 < DOUBLE_TAP_MS:
                self.start_recording()
            self._last_tap = now
