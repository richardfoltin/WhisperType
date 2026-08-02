"""Application orchestration — shared by the Windows and macOS builds.

Owns the state machine (idle / recording / transcribing / history / error), the
hotkey listener, the recording thread and the single transcription worker.
Everything platform specific goes through `self.backend` (input synthesis,
window targeting, GPU counter) and `self.ui` (overlay + tray).
"""
import json
import threading
import time
from pathlib import Path

import pynput.keyboard

from . import __version__, audio, config
from .jobs import (BenchmarkJob, BenchmarkResult, EngineSwitch, JobQueue,
                   JobStatus, ModelSwitch, ModelUnload, TranscriptionJob)
from . import transcribe as transcribe_mod
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

#: How the push-to-talk key is spelled in the overlay hints and the menu.
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
        self.jobs.load_history()
        self.engine = create_engine(self.cfg)
        self._apply_engine_options()

        self.ptt_key = KEY_MAP.get(self.cfg.ptt_key_name,
                                   pynput.keyboard.Key.ctrl_r)
        self.ptt_label = KEY_LABEL.get(self.cfg.ptt_key_name,
                                       self.cfg.ptt_key_name)

        self.model_name = self._model_for_engine()
        self.model_ready = False
        self.model_switching = False

        self.recording = False
        self.paused = False
        self.enter_stop = False
        self.stop_event = threading.Event()
        self._last_tap = 0.0
        self.shutting_down = False

        # Every recording gets a generation number. Stopping one is not
        # instantaneous — the audio thread sits in stream.read() for up to a
        # chunk period plus the device teardown — so a quick stop-then-start
        # leaves the old thread running while the new one begins. Without
        # this, the dying thread cleared `recording` on the live recording and
        # enqueued its audio against the NEW target window.
        self._rec_gen = 0
        self._discard_gen = -1
        self._rec_thread = None

        #: Last thing that went wrong, shown until acknowledged. Without this
        #: a transcript that could not be delivered looked exactly like one
        #: that was: the overlay just disappeared.
        self.last_error = None

        self.target = None
        self.target_title = ""

        #: Benchmark mode. Armed from the tray; the next recording is run
        #: through every downloaded model instead of being typed anywhere.
        self.benchmark_next = False
        self.benchmark_job = None       # running BenchmarkJob, for the live panel
        self.last_benchmark = None      # last finished one, for "Open last"
        self.downloading_all = False

        # Our own synthetic keystrokes are visible to the global listener. A
        # transcript containing a space would otherwise toggle history mode,
        # and the Enter we send would stop the next recording.
        self._suppress_keys = False

        self.gpu_history = []
        self.gpu_lock = threading.Lock()

        self._last_activity = time.monotonic()
        self._idle_thread_running = False

        self.ui = ui_factory(self)
        self.backend.set_own_window_provider(self.ui.own_window_ids)
        self._listener = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        log(f"WhisperType {__version__} starting")

        if self.backend.gpu_available:
            threading.Thread(target=self._gpu_collector, daemon=True).start()
            log(f"GPU collector started (1s interval, {GPU_WINDOW}s window)")

        threading.Thread(target=self._warm_audio, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

        if self.cfg.idle_unload_seconds > 0:
            self._start_idle_watchdog()

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

    def _warm_audio(self):
        try:
            audio.warm_up(self.cfg)
        except Exception as e:
            # Not fatal — the real open will report it again with the overlay up.
            log(f"Audio warm-up failed: {e}")

    # ── Error surface ────────────────────────────────────────────────────

    def set_error(self, message):
        """Record a failure and make it visible. The overlay stays up until the
        user acknowledges it or the next dictation succeeds."""
        self.last_error = message
        log(f"ERROR SHOWN: {message}")
        self.ui.set_tray_state("error")
        self.ui.refresh()

    def clear_error(self):
        if self.last_error is None:
            return
        self.last_error = None
        self.ui.set_tray_state("idle")
        self.ui.refresh()

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

    # ── Idle memory release ──────────────────────────────────────────────

    def _touch(self):
        self._last_activity = time.monotonic()

    def _start_idle_watchdog(self):
        """Idempotent: the menu can switch idle-unload on after boot."""
        if self._idle_thread_running:
            return
        self._idle_thread_running = True
        threading.Thread(target=self._idle_watchdog, daemon=True).start()
        log(f"Idle unload after {self.cfg.idle_unload_seconds / 60:.0f} min")

    def _idle_watchdog(self):
        """Release the model after a configurable idle period.

        Reloading from the local cache measures about a second, so holding
        ~1.6 GB resident all day for a tool used a few times an hour is a bad
        trade. Set idle_unload_minutes to 0 to keep it resident.
        """
        while not self.shutting_down:
            time.sleep(30.0)
            limit = self.cfg.idle_unload_seconds
            if limit <= 0 or not self.model_ready or self.model_switching:
                continue
            if self.recording or self.jobs.busy():
                continue
            if not self.engine.loaded:
                continue
            if time.monotonic() - self._last_activity >= limit:
                self.jobs.submit_control(ModelUnload())

    # ── Model handling ───────────────────────────────────────────────────

    def _apply_engine_options(self):
        # Half precision is the right default on Turing and newer; on Pascal it
        # can be slower than fp32, so it stays overridable. Absent on the API
        # engine, which decides nothing locally.
        if hasattr(self.engine, "fp16"):
            self.engine.fp16 = bool(self.cfg.get("fp16", self.engine.fp16))
            log(f"fp16={self.engine.fp16}")

    def _model_for_engine(self):
        """Each engine remembers its own model — the names do not overlap."""
        if self.cfg.stt_engine == "openai":
            return self.cfg.openai_model
        return self.cfg.get("last_model", "large-v3-turbo")

    def _remember_model(self, name):
        if self.cfg.stt_engine == "openai":
            self.cfg.set("openai_model", name)
        else:
            self.cfg.set("last_model", name)
        self.cfg.save()

    # ── Engine (local GPU vs hosted API) ─────────────────────────────────

    @property
    def engine_kind(self):
        return self.cfg.stt_engine

    def request_engine(self, kind):
        """Switch between the local model and the hosted API."""
        if kind == self.cfg.stt_engine or self.recording or self.model_switching:
            return
        if kind == "openai" and not self.cfg.openai_api_key:
            self.set_error("No OpenAI API key — set one from the menu first")
            return
        self.model_switching = True
        self.ui.set_tray_state("loading")
        self.jobs.submit_control(EngineSwitch(kind))

    def _handle_engine_switch(self, msg):
        previous = self.cfg.stt_engine
        try:
            # Release the outgoing engine's memory before building the next.
            try:
                self.engine.unload()
            except Exception as e:
                log(f"Could not unload the previous engine: {e}")
            self.cfg.set("stt_engine", msg.engine)
            self.engine = create_engine(self.cfg)
            self._apply_engine_options()
            self.model_name = self._model_for_engine()
            self.engine.load(self.model_name)
            self.model_ready = True
            self.cfg.save()
            self.clear_error()
            self.ui.set_tray_state("idle")
            log(f"Engine switched to {msg.engine} ({self.model_name})")
        except Exception as e:
            # Fall back to what was working rather than leaving no engine.
            self.cfg.set("stt_engine", previous)
            self.model_ready = False
            self.set_error(f"Could not switch to {msg.engine}: {e}")
            try:
                self.engine = create_engine(self.cfg)
                self._apply_engine_options()
                self.model_name = self._model_for_engine()
                self.engine.load(self.model_name)
                self.model_ready = True
            except Exception as e2:
                log(f"Could not restore the {previous} engine either: {e2}")
        finally:
            self.model_switching = False
            self.ui.call_soon(self.ui.refresh_tray)

    def set_api_key(self, key):
        """Store the key and, if the API engine is selected, re-validate it."""
        if not key or not key.strip():
            return False
        if not self.cfg.write_api_key(key):
            self.set_error("Could not write the API key file")
            return False
        self.clear_error()
        if hasattr(self.engine, "refresh_models"):
            self.engine.refresh_models()
        self.ui.call_soon(self.ui.refresh_tray)
        return True

    def model_catalog(self):
        return self.engine.catalog()

    def request_model(self, name):
        """Called from the tray menu. The switch is queued so it runs on the
        worker thread, after everything already queued has been transcribed."""
        if self.recording or self.model_switching:
            return
        if name == self.model_name and self.model_ready:
            return                      # already active; re-pick a failed one
        self.model_switching = True
        if not self.engine.is_downloaded(name):
            self.ui.set_tray_state("downloading")
        else:
            self.ui.set_tray_state("loading")
        self.jobs.submit_control(ModelSwitch(name))

    # ── Transcription worker (the only thread that touches the model) ─────

    def _worker(self):
        self.ui.set_tray_state(
            "downloading" if not self.engine.is_downloaded(self.model_name)
            else "loading")
        try:
            self.engine.load(self.model_name)
            self.model_ready = True
            # Once a model exists we know the tokenizer works, so this is the
            # first point at which the prompt budget can be measured.
            transcribe_mod.report_prompt_budget(self.cfg.language)
            self.ui.set_tray_state("idle")
            self.ui.call_soon(self.ui.refresh_tray)
            log("Ready.")
        except Exception as e:
            self.set_error(f"Could not load {self.model_name}: {e}")
            self.ui.call_soon(self.ui.refresh_tray)

        while True:
            item = self.jobs.take()
            try:
                if isinstance(item, EngineSwitch):
                    self._handle_engine_switch(item)
                    continue
                if isinstance(item, ModelSwitch):
                    self._handle_model_switch(item)
                    continue
                if isinstance(item, ModelUnload):
                    self._handle_unload()
                    continue
                if isinstance(item, BenchmarkJob):
                    self._run_benchmark(item)
                    continue
                self._handle_job(item)
            except Exception as e:
                log(f"Worker error: {e}")
            finally:
                self.jobs.done()

    def _handle_unload(self):
        try:
            self.engine.unload()
            self.ui.call_soon(self.ui.refresh_tray)
        except Exception as e:
            log(f"Unload failed: {e}")

    def _handle_model_switch(self, msg):
        previous = self.model_name
        try:
            log(f"Switching model to {msg.name}...")
            self.engine.load(msg.name)
            self.model_name = msg.name
            self.model_ready = True
            # Only persist a model that actually loaded, or one bad switch
            # would break the next launch too.
            self._remember_model(msg.name)
            self.clear_error()
            self.ui.set_tray_state("idle")
        except Exception as e:
            self.model_name = previous
            self.set_error(f"Could not load {msg.name}: {e}")
        finally:
            self.model_switching = False
            self.ui.call_soon(self.ui.refresh_tray)

    def _handle_job(self, job):
        if job.status == JobStatus.CANCELLED:
            log(f"Skipped cancelled job {job.job_id}")
            return
        entry = {
            # `at` and `secs` are the raw values the UI formats and groups by;
            # `ts`/`dur` stay for tk_ui and for history.json written by older
            # builds. `app` is no longer truncated — the view ellipsises.
            "at": int(job.created_at),
            "secs": round(job.audio_duration, 2),
            "ts": time.strftime("%H:%M:%S", time.localtime(job.created_at)),
            "dur": f"{job.audio_duration:.1f}s",
            "app": job.app_name or "?",
            "bundle": getattr(job.target, "bundle_id", "") or "",
            "window": job.window_name,
            "text": "",
        }
        try:
            self.jobs.set_status(job, JobStatus.TRANSCRIBING)
            self.ui.call_soon(self.ui.refresh)
            self.ui.set_tray_state("transcribing")

            text = self.engine.transcribe(job.audio_bytes, self.cfg.language)
            self._touch()
            log(f"Transcribed (job {job.job_id}, target={job.window_name}): {text}")

            if not text:
                return
            # Run it across the overlay before it is typed, so there is a
            # moment where you can see what is about to land in your document.
            self.ui.set_ticker(text)
            entry["text"] = text
            self.jobs.add_history(entry)

            if self.shutting_down:
                log(f"Shutdown: kept job {job.job_id} in history, not typing")
            elif job.status == JobStatus.CANCELLED:
                log(f"Job {job.job_id} cancelled during transcription — history only")
            else:
                self._deliver(text, job)
        except Exception as e:
            # Record the failure in history too, so the user at least sees that
            # a clip was lost and when.
            entry["text"] = f"[transcription failed: {e}]"
            self.jobs.add_history(entry)
            self.set_error(f"Transcription failed: {e}")
        finally:
            self.jobs.finish(job)
            self.ui.call_soon(self.ui.refresh)
            # Long enough for the transcript to finish running across the
            # overlay (~700ms) before it disappears.
            self.ui.call_later(900, self.ui.check_hide)
            if not self.jobs.busy() and not self.recording:
                self.ui.set_tray_state("error" if self.last_error else "idle")

    # ── Benchmark ────────────────────────────────────────────────────────

    def benchmark_supported(self):
        return getattr(self.ui, "supports_benchmark", False)

    def toggle_benchmark_next(self):
        if not self.benchmark_supported():
            return
        self.benchmark_next = not self.benchmark_next
        log(f"Benchmark mode {'ARMED' if self.benchmark_next else 'disarmed'}")
        self.ui.call_soon(self.ui.refresh_tray)
        self.ui.call_soon(self.ui.refresh)

    def open_last_benchmark(self):
        job = self.last_benchmark
        if job is not None:
            self.ui.call_soon(lambda: self.ui.show_benchmark(job))

    def download_all_models(self):
        if self.downloading_all:
            return
        threading.Thread(target=self._download_all, daemon=True).start()

    def _download_all(self):
        missing = [m.name for m in self.engine.catalog() if not m.downloaded]
        if not missing:
            log("All models already downloaded.")
            return
        self.downloading_all = True
        self.ui.set_tray_state("downloading")
        try:
            log(f"Pre-downloading {len(missing)} model(s): {', '.join(missing)}")
            for name in missing:
                try:
                    log(f"Downloading {name}...")
                    self.engine.predownload(name)
                    log(f"Downloaded {name}.")
                except Exception as e:
                    log(f"Failed to download {name}: {e}")
                self.ui.call_soon(self.ui.refresh_tray)
            log("Model pre-download finished.")
        finally:
            self.downloading_all = False
            # predownload may have displaced the resident model (MLX keeps
            # exactly one), so make sure dictation still has one.
            if not self.engine.loaded and self.model_name:
                try:
                    self.engine.load(self.model_name)
                except Exception as e:
                    self.set_error(f"Could not reload {self.model_name}: {e}")
            self.ui.set_tray_state("error" if self.last_error else "idle")
            self.ui.call_soon(self.ui.refresh_tray)

    def _run_benchmark(self, job):
        log(f"Benchmark job {job.job_id} started "
            f"({job.audio_duration:.1f}s audio, {len(job.results)} models)")
        original = self.model_name
        # Blocks the push-to-talk key and the tray's model menu for the run.
        self.model_switching = True
        self.benchmark_job = job

        def refresh():
            self.ui.call_soon(self.ui.refresh_benchmark)

        # Drop the dictation model first: otherwise every benchmarked model
        # shares VRAM with it, and large-v3 next to large-v3-turbo is ~4.7 GB.
        try:
            self.engine.unload()
        except Exception as e:
            log(f"Could not unload before benchmark: {e}")

        self.ui.call_soon(lambda: self.ui.show_benchmark(job))
        refresh()

        for result in job.results:
            model = None
            try:
                result.status = "loading"
                refresh()
                model, result.load_secs = self.engine.load_for_benchmark(result.model)

                result.status = "running"
                refresh()
                t0 = time.perf_counter()
                result.text = self.engine.transcribe_with(
                    model, job.audio_bytes, self.cfg.language)
                result.transcribe_secs = time.perf_counter() - t0
                result.status = "done"
                log(f"Benchmark [{result.model}] "
                    f"{result.transcribe_secs:.2f}s: {result.text[:60]}")
            except Exception as e:
                result.status = "error"
                result.error = str(e)
                log(f"Benchmark [{result.model}] ERROR: {e}")
            finally:
                # Also runs when the load or the decode raised, so a partially
                # loaded model cannot stay pinned for the rest of the run.
                model = None
                self.engine.free_cache()
            refresh()

        self._save_benchmark(job)
        self.last_benchmark = job
        self.benchmark_job = None

        try:
            log(f"Restoring {original}...")
            self.engine.load(original)
            self.model_ready = True
        except Exception as e:
            self.model_ready = False
            self.set_error(f"Could not restore {original} after the benchmark: {e}")

        self.model_switching = False
        self.ui.call_soon(self.ui.refresh_tray)
        refresh()
        log(f"Benchmark job {job.job_id} complete")

    def _save_benchmark(self, job):
        directory = Path.home() / ".whispertype" / "benchmarks"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(job.created_at))
            data = {
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(job.created_at)),
                "audio_duration_secs": round(job.audio_duration, 2),
                "app": job.app_name,
                "window": job.window_name,
                "device": self.engine.device_label,
                "language": self.cfg.language,
                "results": [vars(r) for r in job.results],
            }
            path = directory / f"benchmark_{stamp}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            with open(directory / "benchmark_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n=== {data['created_at']}  "
                        f"audio={data['audio_duration_secs']}s  "
                        f"device={data['device']} ===\n")
                for r in job.results:
                    secs = f"{r.transcribe_secs:.2f}s" if r.transcribe_secs else "—"
                    f.write(f"  [{r.model:<16}] {secs:>7}  "
                            f"{r.text if r.text is not None else (r.error or '')}\n")
            log(f"Benchmark saved: {path.name}")
        except Exception as e:
            log(f"Could not save the benchmark: {e}")

    def _deliver(self, text, job):
        if job.target is None:
            # No captured target means we do not know where this belongs.
            # Typing into whatever happens to be in front would drop a
            # transcript into an unrelated document.
            self.set_error("No target window was captured — "
                           "text saved to history (Space)")
            return
        try:
            activated = self.backend.activate(job.target)
        except Exception as e:
            log(f"activate failed: {e}")
            activated = False
        if not activated:
            self.set_error(
                f"{job.app_name or 'The target window'} is gone — "
                f"text saved to history (Space)")
            return
        self._suppress_keys = True
        try:
            self.backend.type_text(text)
            if job.send_enter:
                time.sleep(0.05)
                self.backend.send_enter()
                log(f"Sent Enter after transcription (job {job.job_id})")
            self.clear_error()
        except PermissionError as e:
            self.set_error(f"{e} — text saved to history (Space)")
        except Exception as e:
            self.set_error(f"Could not type the text ({e}) — saved to history")
        finally:
            # Let the last synthetic events drain past the listener.
            time.sleep(0.15)
            self._suppress_keys = False

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, capture_target=True):
        if self.recording or self.paused:
            return
        if not self.model_ready:
            self.ui.call_soon(lambda: self.ui.show_loading(self.model_name))
            return
        self._touch()
        self.recording = True
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
        self._rec_gen += 1
        self.ui.call_soon(lambda: self.ui.show_recording(name))
        # Target and stop event are passed in, not read back off self: by the
        # time this thread finishes they may belong to a newer recording.
        self._rec_thread = threading.Thread(
            target=self._record_and_enqueue,
            args=(self._rec_gen, self.stop_event, self.target, name),
            daemon=True)
        self._rec_thread.start()
        log(f"Recording started, target: {name or '(none)'}")

    def stop_recording(self, discard=False, send_enter=False):
        if not self.recording:
            return
        self.recording = False
        if discard:
            self._discard_gen = self._rec_gen
        self.enter_stop = send_enter
        self.stop_event.set()

    def _record_and_enqueue(self, gen, stop_ev, target, window_name):
        try:
            capture = audio.record_until_stop(
                self.cfg, stop_ev,
                level_callback=self.ui.push_level,
                on_first_chunk=self.ui.on_capture_started)
            data = capture.data
            self._touch()

            # A newer recording may have started while we drained out of
            # stream.read(). This thread then owns none of the shared state
            # and must not drive the overlay — but it still owns its audio.
            superseded = gen != self._rec_gen
            discarded = gen == self._discard_gen

            if not superseded:
                self.recording = False

            if not data or discarded:
                if not superseded:
                    self.enter_stop = False
                    if not self.ui.history_mode:
                        self.ui.call_soon(self.ui.on_recording_stopped)
                log("Recording discarded" if data else "No audio captured")
                return

            # A clip with no speech in it is not silence to be transcribed —
            # Whisper reliably invents a stock phrase for silence, and that
            # phrase would be typed into whatever the user is working on.
            if capture.speech_seconds < self.cfg.min_speech_seconds:
                if not superseded:
                    self.enter_stop = False
                    self.ui.call_soon(lambda: self.ui.show_notice("Nothing heard"))
                log(f"Nothing heard ({capture.speech_seconds:.2f}s above "
                    f"threshold) — clip dropped")
                return

            duration = len(data) / (self.cfg.rate * 2)   # 16-bit mono
            try:
                app_name = self.backend.target_app(target) if target is not None else "?"
            except Exception:
                app_name = "?"

            if superseded:
                # The Enter flag and the benchmark arming belong to whichever
                # recording is live now. This clip is still transcribed — being
                # replaced is not a reason to throw away something already said.
                log(f"Recording {gen} was superseded by {self._rec_gen} — "
                    f"transcribing its audio anyway")

            if self.benchmark_next and not superseded:
                self.benchmark_next = False
                self.enter_stop = False
                self.ui.call_soon(self.ui.refresh_tray)
                downloaded = [m.name for m in self.engine.catalog() if m.downloaded]
                if not downloaded:
                    log("Benchmark skipped: no models are downloaded")
                    self.ui.call_soon(self.ui.on_recording_stopped)
                    return
                bench = BenchmarkJob(
                    job_id=self.jobs.next_id(),
                    audio_bytes=data,
                    audio_duration=duration,
                    window_name=window_name,
                    app_name=app_name,
                    results=[BenchmarkResult(model=m) for m in downloaded],
                )
                self.ui.call_soon(self.ui.on_recording_stopped)
                self.jobs.submit_control(bench)
                log(f"Enqueued BENCHMARK job {bench.job_id} "
                    f"({duration:.1f}s, {len(downloaded)} models)")
                self.ui.set_tray_state("transcribing")
                return

            if superseded:
                send_enter = False
            else:
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
            # Skipped when superseded: on_recording_stopped would cancel the
            # timers of the recording that is actually live.
            self.ui.call_soon(self.ui.refresh if superseded
                              else self.ui.on_recording_stopped)
            self.jobs.submit(job)
            log(f"Enqueued job {job.job_id} for '{window_name}' ({duration:.1f}s)")
            if not superseded:
                self.ui.set_tray_state("transcribing")
        except Exception as e:
            if gen == self._rec_gen:
                self.recording = False
                self.ui.call_soon(self.ui.on_recording_stopped)
            self.set_error(f"Recording failed: {e}")

    # ── History mode ─────────────────────────────────────────────────────

    def toggle_history(self):
        """Show or hide the history list.

        Leaving history deliberately does NOT start a recording any more: with
        the overlay up, every Space pressed in another app used to alternate
        start/discard, and one of those phantom clips would eventually be
        transcribed into the user's document.
        """
        self.ui.set_history_mode(not self.ui.history_mode)

    def show_history(self):
        if self.recording:
            self.stop_recording(discard=True)
        self.ui.call_soon(lambda: self.ui.set_history_mode(True))

    def cancel_job(self, job):
        self.jobs.cancel(job)
        log(f"Cancelled job {job.job_id}")
        self.ui.refresh()

    def set_language(self, code):
        """Language is read fresh for every job, so this needs no restart."""
        if code == self.cfg.language:
            return
        self.cfg.set("language", code)
        self.cfg.save()
        log(f"Language set to {code}")
        self.ui.call_soon(self.ui.refresh_tray)
        self.ui.show_notice(f"Language: {code}")

    def set_idle_unload(self, minutes):
        """Change when an idle model is released. 0 keeps it resident.

        The watchdog re-reads the limit on every pass, so the only thing that
        needs starting is the thread itself, when it was switched off at boot.
        """
        if abs(self.cfg.idle_unload_seconds - minutes * 60) < 1:
            return
        self.cfg.set("idle_unload_minutes", minutes)
        self.cfg.save()
        log(f"Idle unload set to {minutes} min" if minutes
            else "Idle unload disabled — model stays resident")
        if minutes > 0 and not self._idle_thread_running:
            self._start_idle_watchdog()
        self.ui.call_soon(self.ui.refresh_tray)
        self.ui.show_notice("Model stays resident" if not minutes
                            else f"Release model after {minutes} min")

    def set_paused(self, paused):
        self.paused = paused
        if paused and self.recording:
            self.stop_recording(discard=True)
        self.ui.set_tray_state("paused" if paused else "idle")
        self.ui.call_soon(self.ui.refresh_tray)
        log(f"Dictation {'paused' if paused else 'resumed'}")

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

        # Scoped to the states where the user is actually interacting with the
        # overlay. Gating on "overlay visible" meant every space bar pressed in
        # another app during a transcription toggled history mode.
        # History is a menu-bar item now. Binding it to Space meant a key you
        # press constantly was being watched globally for the whole time the
        # overlay happened to be up.

        if key == K.esc and self.ui.visible:
            if self.recording:
                self.stop_recording(discard=True)
            self.clear_error()
            self.ui.call_soon(self.ui.hide)
            return

        if key == K.enter and self.recording:
            self.stop_recording(send_enter=True)
            log("Recording stopped by Enter (will send Enter after transcription)")
            return

        if key != self.ptt_key or self.model_switching or self.paused:
            return

        now = time.time()
        if self.recording:
            self.stop_recording()
            log("Recording stopped by keypress")
        else:
            if (now - self._last_tap) * 1000 < DOUBLE_TAP_MS:
                self.start_recording()
            self._last_tap = now
