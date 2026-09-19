"""Application orchestration — shared by the Windows and macOS builds.

Owns the state machine (idle / recording / transcribing / history / error), the
hotkey listener, the recording thread and the single transcription worker.
Everything platform specific goes through `self.backend` (input synthesis,
window targeting, GPU counter) and `self.ui` (overlay + tray).
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pynput.keyboard

_IS_MAC = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"

#: Whether the platform's key hook can be owned well enough to be allowed to
#: swallow keys. Set properly below; False keeps the listener a pure observer.
_CAN_OWN_HOOK = False

if _IS_MAC:
    import Quartz

    #: Virtual keycodes, from Carbon's Events.h.
    _KEY_RETURN, _KEY_KP_ENTER, _KEY_ESCAPE = 0x24, 0x4C, 0x35
    #: Keys the intercept may swallow, and only while recording. Anything not
    #: listed here is never touched — a global listener that eats keystrokes is
    #: far worse than one that misses a shortcut.
    _SWALLOWED_KEYS = frozenset({_KEY_RETURN, _KEY_KP_ENTER, _KEY_ESCAPE})
    _DISCARD_KEY = _KEY_ESCAPE

    #: The two ways macOS tells a tap it has been switched off. Masked because
    #: the event type arrives as an unsigned 32-bit value.
    _TAP_DISABLED = frozenset({
        Quartz.kCGEventTapDisabledByTimeout & 0xFFFFFFFF,
        Quartz.kCGEventTapDisabledByUserInput & 0xFFFFFFFF,
    })

    class _TapOwningListener(pynput.keyboard.Listener):
        """A listener that keeps hold of its event tap.

        pynput creates the tap in a local, enables it once and never looks at
        it again (_util/darwin.py). macOS switches a tap off the moment one of
        its callbacks overruns the system timeout, and with nobody holding the
        port it stays off — the push-to-talk key then dies silently until the
        daemon is restarted, which is exactly how suppression was lost the
        first time (commit b116084). Holding the port is what lets _intercept
        turn it back on.
        """
        tap = None

        def _create_event_tap(self):
            self.tap = super()._create_event_tap()
            return self.tap

    #: Suppression on macOS is only safe while the tap can be revived, and that
    #: rides on a pynput internal. If the internal ever moves, the listener
    #: stays an observer rather than becoming a tap nobody can bring back.
    _CAN_OWN_HOOK = hasattr(pynput.keyboard.Listener, "_create_event_tap")

if _IS_WIN:
    #: Virtual key codes. The numeric keypad's Enter reports VK_RETURN too.
    _VK_RETURN, _VK_ESCAPE = 0x0D, 0x1B
    _SWALLOWED_KEYS = frozenset({_VK_RETURN, _VK_ESCAPE})
    _DISCARD_KEY = _VK_ESCAPE

    #: WM_KEYDOWN / WM_SYSKEYDOWN and their releases, as the low-level hook
    #: reports them.
    _KEY_DOWN_MESSAGES = frozenset({0x0100, 0x0104})
    _KEY_UP_MESSAGES = frozenset({0x0101, 0x0105})

    #: Nothing to own here: a WH_KEYBOARD_LL hook is not a tap the system
    #: switches off behind our back, and the filter runs in a callback pynput
    #: already puts in that path.
    _CAN_OWN_HOOK = True

from . import __version__, audio, config, spool
from .jobs import (BenchmarkJob, BenchmarkResult, DictationSession,
                   EngineSwitch, JobQueue, JobStatus, ModelSwitch, ModelUnload,
                   TranscriptionJob)
from . import transcribe as transcribe_mod
from .log import log
from .transcribe import create_engine

DOUBLE_TAP_MS = 400
GPU_WINDOW = 60          # samples kept for the sparkline (1 Hz => 60 s)

#: How long the model may stay resident while something is running full
#: screen, regardless of idle_unload_minutes. A game needs the VRAM now, not
#: in ten minutes, and reloading costs the same second it always did.
FULLSCREEN_IDLE_SECONDS = 20

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
        #: Idle RMS measured at startup, for the settings window. None until
        #: the warm-up finishes.
        self.noise_floor = None

        #: Benchmark mode. Armed from the tray; the next recording is run
        #: through every downloaded model instead of being typed anywhere.
        self.benchmark_next = False
        self.benchmark_job = None       # running BenchmarkJob, for the live panel
        self.last_benchmark = None      # last finished one, for "Open last"
        self.downloading_all = False
        #: Engine the user just picked, while its model is still loading.
        self._pending_engine = None

        # Our own synthetic keystrokes are visible to the global listener. A
        # transcript containing a space would otherwise toggle history mode,
        # and the Enter we send would stop the next recording.
        self._suppress_keys = False

        #: Whether the listener may swallow a key rather than only observe it.
        #: Without it, Enter both ends the dictation and reaches whatever is
        #: focused: a newline in the very document the transcript is about to
        #: be typed into.
        #:
        #: This was off for a long time, and for a real reason. Passing
        #: darwin_intercept switches pynput's tap from
        #: kCGEventTapOptionListenOnly to kCGEventTapOptionDefault, which puts
        #: our Python callbacks in the critical path of every keystroke on the
        #: machine. When such a callback overruns the system timeout — and
        #: under GIL contention during a long dictation it did — macOS posts
        #: kCGEventTapDisabledByTimeout and disables the tap. pynput calls
        #: CGEventTapEnable exactly once at startup (_util/darwin.py) and
        #: handles that event nowhere, so the tap stayed dead and the hotkey
        #: silently stopped working until the daemon was restarted (b116084).
        #:
        #: Both halves of that are handled now. No callback does any work on
        #: the hook's own thread any more — they decide and hand off to
        #: _key_worker — and the tap is owned, so the disable event is caught
        #: and CGEventTapEnable called again. Windows was never exposed to it
        #: the same way: pynput's WH_KEYBOARD_LL hook only posts a message and
        #: runs on_press later on its own loop, so the filter added here reads
        #: three fields and returns.
        self._can_suppress = _CAN_OWN_HOOK
        self._swallowed = set()

        #: Stamped into every keystroke this app synthesises, so the hook can
        #: tell our own typing from the user's and never swallow it. None on a
        #: backend that cannot mark its events — then _suppress_keys is the
        #: only guard.
        self._event_marker = getattr(backend, "event_marker", None)

        #: Work handed over by the key hooks, drained by _key_worker.
        self._key_work = queue.SimpleQueue()

        #: A hook that raises is a bug, and it would raise once per keystroke.
        #: Logging that per keystroke would itself blow the hook's budget.
        self._hook_error_logged = False

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

        threading.Thread(target=self._key_worker, daemon=True).start()

        listener_cls = pynput.keyboard.Listener
        listener_kwargs = {"on_press": self._on_press}
        if self._can_suppress and _IS_MAC:
            # Switches the tap from ListenOnly to Default, which is what makes
            # suppression possible at all — and what makes owning the tap
            # mandatory rather than tidy.
            listener_kwargs["darwin_intercept"] = self._intercept
            listener_cls = _TapOwningListener
        elif self._can_suppress and _IS_WIN:
            listener_kwargs["win32_event_filter"] = self._win32_filter
        self._listener = listener_cls(**listener_kwargs)
        self._listener.start()

        log(f"PTT={self.cfg.ptt_key_name} (double-tap) | Model={self.model_name}")

    def run(self):
        self.ui.run()          # blocks on the platform main loop
        if self._listener:
            self._listener.stop()
        self._key_work.put(None)
        # The decoder is a child process now, and a child holding 1.8 GB of
        # VRAM does not go away just because its parent's main loop returned.
        self._close_engine()
        log("Daemon stopped.")

    def _close_engine(self):
        """Shut down an out-of-process engine. A no-op for the in-process
        ones, which have nothing to shut down."""
        close = getattr(self.engine, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as e:
            log(f"Could not stop the decoder process: {e}")

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
            # Kept so the settings window can show the measured idle level next
            # to the threshold — otherwise "silence_threshold: 200" is a number
            # with nothing to compare it against.
            self.noise_floor = audio.warm_up(self.cfg)
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

    def yield_gpu(self):
        """Whether the model should get out of the way right now.

        True while a game (or anything else) is running full screen and the
        user has not switched the behaviour off. The model is the single
        biggest thing this app holds, and it is holding it against something
        the user is actively looking at.
        """
        if not self.cfg.get("release_gpu_for_fullscreen", True):
            return False
        try:
            return bool(self.backend.fullscreen_app_running())
        except Exception as e:
            log(f"Could not tell whether a full-screen app is running: {e}")
            return False

    def release_gpu(self, reason="requested"):
        """Drop the resident model. Queued through the worker like every other
        model operation, so nothing frees memory out from under a decode."""
        if not self.engine.loaded or self.model_switching:
            return False
        log(f"Releasing the model ({reason})")
        self.jobs.submit_control(ModelUnload())
        return True

    def _idle_watchdog(self):
        """Release the model after a configurable idle period.

        Reloading from the local cache measures about a second, so holding
        ~1.6 GB resident all day for a tool used a few times an hour is a bad
        trade. Set idle_unload_minutes to 0 to keep it resident.

        A full-screen app overrides that, including "keep resident": on an
        8 GB card the model is the difference between a game fitting in VRAM
        and thrashing, and "keep it resident" was a preference about idling,
        not about competing with a game.
        """
        while not self.shutting_down:
            time.sleep(10.0)
            limit = self.cfg.idle_unload_seconds
            gaming = self.yield_gpu()
            if gaming:
                limit = (min(limit, FULLSCREEN_IDLE_SECONDS) if limit > 0
                         else FULLSCREEN_IDLE_SECONDS)
            if limit <= 0 or not self.model_ready or self.model_switching:
                continue
            if self.recording or self.jobs.busy():
                continue
            if not self.engine.loaded:
                continue
            if time.monotonic() - self._last_activity >= limit:
                log("Releasing the model — a full-screen app needs the GPU"
                    if gaming else "Releasing the model — idle")
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
        """What the menu should show as selected.

        Prefers the pending choice: swapping the engine reloads a model, which
        takes seconds, and until then cfg still holds the old value — so the
        menu would keep the old engine ticked and, worse, keep listing the old
        engine's models as if the click had not registered.
        """
        return self._pending_engine or self.cfg.stt_engine

    def request_engine(self, kind):
        """Switch between the local model and the hosted API."""
        if kind == self.engine_kind or self.recording or self.model_switching:
            return
        if kind == "openai" and not self.cfg.openai_api_key:
            self.set_error("No OpenAI API key — set one from the menu first")
            return
        self.model_switching = True
        self._pending_engine = kind
        self.ui.set_tray_state("loading")
        # Redraw now, not when the switch finishes: the menu has to stop
        # showing the previous engine's models the moment the click lands.
        self.ui.refresh_tray()
        if not getattr(self.ui, "settings_open", False):
            # The settings window already says "Switching…" in place. Popping
            # the overlay over it as well is just a flash of noise.
            self.ui.show_notice("Switching to "
                                + ("OpenAI API…" if kind == "openai"
                                   else "local model…"))
        self.jobs.submit_control(EngineSwitch(kind))

    def _handle_engine_switch(self, msg):
        previous = self.cfg.stt_engine
        try:
            # Release the outgoing engine's memory before building the next.
            try:
                self.engine.unload()
            except Exception as e:
                log(f"Could not unload the previous engine: {e}")
            # And its process, if it had one — unload frees the model, not the
            # torch and CUDA context the child is still sitting on.
            self._close_engine()
            self.cfg.set("stt_engine", msg.engine)
            self.engine = create_engine(self.cfg)
            self._apply_engine_options()
            self.model_name = self._model_for_engine()
            self.engine.load(self.model_name)
            self.model_ready = True
            self.cfg.save()
            self.clear_error()
            # A freshly loaded model is not idle. Without this the watchdog
            # sees activity from before the switch and releases it seconds
            # after it finished loading.
            self._touch()
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
            self._pending_engine = None
            # Direct, not call_soon: the menu contents changed, and routing the
            # rebuild through the Tk loop makes it depend on a main loop that
            # may be busy. refresh_tray touches no Tk.
            self.ui.refresh_tray()

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
        self.ui.refresh_tray()
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
        if self.yield_gpu() and self.engine.is_downloaded(self.model_name):
            # Booting into a game — usually because the machine was just
            # restarted — must not claim 1.8 GB of VRAM to sit idle in.
            # _handle_job loads on demand, so dictation still works; the first
            # one after this pays the load.
            self.model_ready = True
            log(f"A full-screen app is running — {self.model_name} will be "
                f"loaded on the first dictation instead of now")
            try:
                transcribe_mod.report_prompt_budget(self.cfg.language)
            except Exception as e:
                # Diagnostics only, and the tokenizer may want the model that
                # was deliberately not loaded.
                log(f"Prompt budget not measured yet: {e}")
            self.ui.set_tray_state("idle")
            self.ui.refresh_tray()
            log("Ready.")
            self._pump()
            return

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
            self.ui.refresh_tray()
            log("Ready.")
        except Exception as e:
            self.set_error(f"Could not load {self.model_name}: {e}")
            self.ui.refresh_tray()

        self._pump()

    def _recover_spool(self):
        """Re-transcribe whatever the previous run never finished with.

        The transcript goes to history and nowhere else. The window a clip was
        dictated into stopped existing when the process did, and typing a
        two-minute-old sentence into whatever happens to be focused now is a
        worse outcome than the crash it is recovering from.

        Runs on the worker thread, before the queue is served, because loading
        the model is a worker-thread operation and because the recovered text
        should be in history before the user starts dictating over it.
        """
        try:
            clips = spool.orphans()
        except Exception as e:
            log(f"Spool: could not be read ({e})")
            return

        for clip in clips:
            if clip.attempts >= spool.MAX_ATTEMPTS:
                log(f"Spool: {clip.wav.name} already failed "
                    f"{clip.attempts}x — not retried. The audio is kept at "
                    f"{clip.wav}")
        live = [c for c in clips if c.attempts < spool.MAX_ATTEMPTS]
        if not live:
            return

        log(f"Recovering {len(live)} unfinished recording(s) from the "
            f"previous run — transcripts go to history, nothing is typed")
        for clip in live:
            try:
                # Persisted before the decode, not after: the crash this
                # recovers from leaves no chance to write anything afterwards,
                # and a clip that reliably kills the decoder must not be able
                # to kill every launch from here on.
                clip.note_attempt()
                if not self.engine.loaded:
                    self.ui.set_tray_state("loading")
                    self.engine.load(self.model_name)
                self.ui.set_tray_state("transcribing")
                text = self.engine.transcribe(
                    clip.audio_bytes(),
                    clip.meta.get("language") or self.cfg.language)
                log(f"Recovered {clip.wav.name} ({clip.duration:.1f}s): {text}")
                if text:
                    self.jobs.add_history({
                        "at": int(clip.created_at),
                        "secs": round(clip.duration, 2),
                        "ts": time.strftime("%H:%M:%S",
                                            time.localtime(clip.created_at)),
                        "dur": f"{clip.duration:.1f}s",
                        "app": clip.app_name,
                        "bundle": "",
                        "window": clip.window_name,
                        "text": text,
                        # Marks it as rescued rather than dictated, so the
                        # history view is not lying about where it came from.
                        "recovered": True,
                    })
                spool.discard(clip.wav)
            except Exception as e:
                # Left on disk on purpose — the attempt counter is what stops
                # this from repeating forever.
                log(f"Could not recover {clip.wav.name}: {e}")

        self.ui.call_soon(lambda: self.ui.show_notice(
            f"Recovered {len(live)} recording{'' if len(live) == 1 else 's'} "
            f"— see history"))
        self.ui.set_tray_state("error" if self.last_error else "idle")
        self.ui.call_soon(self.ui.refresh)
        # Same etiquette _handle_job keeps: a recovery that had to load the
        # model while a game is running hands the VRAM straight back.
        if self.yield_gpu():
            self._handle_unload()

    def _pump(self):
        """The worker's message loop. Every model operation happens on this
        thread and only this thread — see ModelSwitch's docstring."""
        self._recover_spool()
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
            self.ui.refresh_tray()
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
            self.ui.refresh_tray()

    def _history_entry(self, job):
        """The history record for a finished job.

        Timings come from the dictation rather than the job when the clip was
        decoded in segments: the last segment's clock would say a five-minute
        dictation took twenty seconds and happened at the moment it ended.
        """
        source = job.session or job
        return {
            # `at` and `secs` are the raw values the UI formats and groups by;
            # `ts`/`dur` stay for tk_ui and for history.json written by older
            # builds. `app` is no longer truncated — the view ellipsises.
            "at": int(source.created_at),
            "secs": round(source.audio_duration, 2),
            "ts": time.strftime("%H:%M:%S", time.localtime(source.created_at)),
            "dur": f"{source.audio_duration:.1f}s",
            "app": job.app_name or "?",
            # Asked of the backend rather than read off the target: only the
            # backend knows what identifies an application on its platform (a
            # bundle id, an executable path), and on Windows the target is a
            # bare integer with no attributes to read at all.
            "bundle": self._target_bundle(job.target),
            "window": job.window_name,
            "text": "",
        }

    def _handle_job(self, job):
        if job.status == JobStatus.CANCELLED:
            log(f"Skipped cancelled job {job.job_id}")
            return
        entry = self._history_entry(job)
        try:
            self.jobs.set_status(job, JobStatus.TRANSCRIBING)
            self.ui.call_soon(self.ui.refresh)

            # The idle watchdog releases the model to reclaim memory, and
            # nothing brought it back: the next dictation after ten quiet
            # minutes failed with "no model is loaded" and the transcript was
            # only recoverable from history. Reloading here is safe — this is
            # the one thread allowed to touch the model — and costs about the
            # second the release was documented to cost.
            if not self.engine.loaded:
                log(f"Model was released while idle — reloading {self.model_name}")
                self.ui.set_tray_state("loading")
                self.engine.load(self.model_name)

            self.ui.set_tray_state("transcribing")
            text = self.engine.transcribe(job.audio_bytes, self.cfg.language)
            self._touch()
            log(f"Transcribed (job {job.job_id}, target={job.window_name}): {text}")

            session = job.session
            if session is not None:
                if session.cancelled:
                    log(f"Job {job.job_id} belongs to a cancelled dictation "
                        f"— its text is dropped")
                    return
                session.add(job.segment_index, text)
                done, total = session.progress()
                if not session.complete():
                    # Deliberately not history and not delivery: a dictation
                    # reaches the document once, whole. Until then the overlay
                    # is the only place the words appear.
                    running = session.text()
                    self.ui.call_soon(lambda: self.ui.set_partial(running, done,
                                                                  total))
                    log(f"Segment {job.segment_index + 1}/{total or '?'} of "
                        f"dictation {session.session_id} decoded")
                    return
                text = session.text()
                # Rebuilt now that set_final has landed. The worker usually
                # dequeues the last segment while the recorder is still
                # spooling the clip, so the entry built at the top of this
                # method was made against an audio_duration of 0.0.
                entry = self._history_entry(job)
                job.spool_path = session.spool_path
                log(f"Dictation {session.session_id} complete "
                    f"({done} segments): {text}")

            if not text:
                # The decoder ran and found nothing to write down. Keeping the
                # audio would not give back anything that was lost.
                spool.discard(job.spool_path)
                return
            # Run it across the overlay before it is typed, so there is a
            # moment where you can see what is about to land in your document.
            self.ui.set_ticker(text)
            entry["text"] = text
            self.jobs.add_history(entry)
            # history.json is now a durable copy of these words, so the audio
            # has done its job. Dropped here rather than after delivery: a
            # keystroke that fails to land is already covered by history, and
            # this is the last point where the clip could still be needed.
            spool.discard(job.spool_path)

            if self.shutting_down:
                log(f"Shutdown: kept job {job.job_id} in history, not typing")
            elif job.status == JobStatus.CANCELLED:
                log(f"Job {job.job_id} cancelled during transcription — history only")
            else:
                self._deliver(text, job)
        except Exception as e:
            if job.session is not None:
                # A segment that cannot be decoded means the dictation can
                # never be assembled — and an un-finalised session never
                # completes, so its remaining segments would sit in `_active`
                # forever and quit() would wait on a dictation that is not
                # coming. Cancelling is also what keeps the spooled clip's
                # recovery honest: the whole recording is still on disk, and
                # the next launch re-decodes all of it rather than trying to
                # patch a hole in the middle.
                self._cancel_session(job.session)
            # Record the failure in history too, so the user at least sees that
            # a clip was lost and when. The spool entry is deliberately NOT
            # dropped here: a decode that raised is exactly the case worth
            # retrying, and the next launch will pick the clip back up.
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
            # Dictating into a game means the model was just loaded for one
            # sentence. Hand the VRAM straight back rather than sitting on it
            # until the watchdog's next pass.
            if not self.jobs.busy() and not self.recording and self.yield_gpu():
                self._handle_unload()

    # ── Benchmark ────────────────────────────────────────────────────────

    def benchmark_supported(self):
        return getattr(self.ui, "supports_benchmark", False)

    def toggle_benchmark_next(self):
        if not self.benchmark_supported():
            return
        self.benchmark_next = not self.benchmark_next
        log(f"Benchmark mode {'ARMED' if self.benchmark_next else 'disarmed'}")
        self.ui.refresh_tray()
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
                self.ui.refresh_tray()
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
            self.ui.refresh_tray()

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
            handle = None
            try:
                result.status = "loading"
                refresh()
                handle, result.load_secs = self.engine.load_for_benchmark(result.model)

                result.status = "running"
                refresh()
                t0 = time.perf_counter()
                result.text = self.engine.transcribe_with(
                    handle, job.audio_bytes, self.cfg.language)
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
                # Asked of the engine rather than dropped locally: the model
                # may be resident in another process, where rebinding a name
                # here frees nothing.
                self.engine.release_benchmark_model()
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
        self.ui.refresh_tray()
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

    def _target_bundle(self, target):
        if target is None:
            return ""
        try:
            return self.backend.target_bundle(target) or ""
        except Exception as e:
            log(f"Could not identify the target application: {e}")
            return ""

    def _deliver(self, text, job):
        if job.target is None:
            # No captured target means we do not know where this belongs.
            # Typing into whatever happens to be in front would drop a
            # transcript into an unrelated document.
            self.set_error("No target window was captured — "
                           "text saved to history")
            return
        try:
            activated = self.backend.activate(job.target)
        except Exception as e:
            log(f"activate failed: {e}")
            activated = False
        if not activated:
            self.set_error(
                f"{job.app_name or 'The target window'} is gone — "
                f"text saved to history")
            return
        self._suppress_keys = True
        try:
            # Asked once the window is really in front: on Windows this is
            # where an elevated target is caught, and SendInput would otherwise
            # report success while delivering nothing.
            self.backend.preflight_typing(job.target)
            self.backend.type_text(text)
            if job.send_enter:
                time.sleep(0.05)
                self.backend.send_enter()
                log(f"Sent Enter after transcription (job {job.job_id})")
            self.clear_error()
        except PermissionError as e:
            self.set_error(f"{e} — text saved to history")
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
        session = None
        try:
            app_name = "?"
            try:
                if target is not None:
                    app_name = self.backend.target_app(target)
            except Exception:
                pass

            if self.cfg.stream_transcription:
                session = DictationSession(gen)

            def submit_segment(data):
                """Queue one segment for decoding while capture continues."""
                job = TranscriptionJob(
                    job_id=self.jobs.next_id(),
                    audio_bytes=data,
                    target=target,
                    window_name=window_name,
                    app_name=app_name,
                    session=session,
                    segment_index=len(session.jobs()),
                )
                session.register(job)
                self.jobs.submit(job)
                log(f"Enqueued segment {job.segment_index} of dictation "
                    f"{session.session_id} ({len(data) / (self.cfg.rate * 2):.1f}s)")
                if gen == self._rec_gen:
                    self.ui.set_tray_state("transcribing")

            capture = audio.record_until_stop(
                self.cfg, stop_ev,
                level_callback=self.ui.push_level,
                on_first_chunk=self.ui.on_capture_started,
                on_segment=submit_segment if session is not None else None)
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
                if session is not None:
                    self._cancel_session(session)
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
                if session is not None:
                    self._cancel_session(session)
                if not superseded:
                    self.enter_stop = False
                    self.ui.call_soon(lambda: self.ui.show_notice("Nothing heard"))
                log(f"Nothing heard ({capture.speech_seconds:.2f}s above "
                    f"threshold) — clip dropped")
                return

            duration = len(data) / (self.cfg.rate * 2)   # 16-bit mono

            if superseded:
                # The Enter flag and the benchmark arming belong to whichever
                # recording is live now. This clip is still transcribed — being
                # replaced is not a reason to throw away something already said.
                log(f"Recording {gen} was superseded by {self._rec_gen} — "
                    f"transcribing its audio anyway")

            if self.benchmark_next and not superseded:
                # A benchmark runs the whole clip through every model, so the
                # segments it was cut into are of no use to it.
                if session is not None:
                    self._cancel_session(session)
                    session = None
                self.benchmark_next = False
                self.enter_stop = False
                self.ui.refresh_tray()
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

            # On disk before it is queued, never after: the window this
            # protects opens the instant the job becomes something a worker
            # can pick up, and a native crash inside the decoder gives no
            # notice at all. The write costs a few milliseconds on an SSD
            # against a recording that took a couple of minutes to make.
            spool_path = spool.save(
                self.jobs.next_id() if session is None else session.session_id,
                data, self.cfg.rate,
                {"duration": duration, "window_name": window_name,
                 "app_name": app_name, "language": self.cfg.language})

            if session is not None:
                # The clip is on disk and every segment is queued, so the
                # dictation can now be declared closed. Order matters: the
                # worker may complete the session the instant set_final lands,
                # and it drops the spool entry on completion — so the path has
                # to be there first.
                session.spool_path = spool_path
                queued = session.jobs()
                if not queued:
                    log("No segments were produced — nothing to transcribe")
                    self._cancel_session(session)
                    self.ui.call_soon(self.ui.on_recording_stopped)
                    return
                queued[-1].send_enter = send_enter
                session.set_final(len(queued), duration)
                self.ui.call_soon(self.ui.refresh if superseded
                                  else self.ui.on_recording_stopped)
                log(f"Dictation {session.session_id} closed: {len(queued)} "
                    f"segments, {duration:.1f}s")
                return

            job = TranscriptionJob(
                job_id=self.jobs.next_id(),
                audio_bytes=data,
                target=target,
                window_name=window_name,
                app_name=app_name,
                audio_duration=duration,
                send_enter=send_enter,
            )
            job.spool_path = spool_path

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
            if session is not None:
                self._cancel_session(session)
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
        if job.session is not None:
            self._cancel_session(job.session)
            return
        self.jobs.cancel(job)
        log(f"Cancelled job {job.job_id}")
        self.ui.refresh()

    def _cancel_session(self, session):
        """Cancel every segment of a dictation.

        The overlay shows a dictation as one row, so a cancel aimed at it means
        the whole thing. Typing the half of a sentence that happened to be
        decoded already would be worse than typing none of it.

        Also the only correct answer on every path in _record_and_enqueue that
        returns without a final segment: an un-finalised session can never
        complete, so its segments would sit in `_active` forever, `busy()`
        would never go false, and quit() would wait for a dictation that is
        never coming.
        """
        session.cancel()
        for job in session.jobs():
            self.jobs.cancel(job)
        log(f"Cancelled dictation {session.session_id}")
        self.ui.refresh()

    def set_config(self, key, value):
        """Change one setting and persist it.

        Everything reachable this way is read per job (or per recording), so
        nothing here needs a restart — the exception is push_to_talk_key, which
        the listener binds once at startup.
        """
        if self.cfg.get(key) == value:
            return
        self.cfg.set(key, value)
        self.cfg.save()
        log(f"Config: {key} = {value!r}")
        if key == "input_device":
            # The resolver caches its lookup, so a device change has to
            # invalidate it or the next recording opens the old one.
            audio.reset_device_cache()
        self.ui.refresh_tray()

    def set_language(self, code):
        """Language is read fresh for every job, so this needs no restart."""
        if code == self.cfg.language:
            return
        self.cfg.set("language", code)
        self.cfg.save()
        log(f"Language set to {code}")
        self.ui.refresh_tray()
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
        self.ui.refresh_tray()
        self.ui.show_notice("Model stays resident" if not minutes
                            else f"Release model after {minutes} min")

    def set_paused(self, paused):
        self.paused = paused
        if paused and self.recording:
            self.stop_recording(discard=True)
        if paused:
            # Pausing is the user saying they will not dictate for a while.
            # Holding the model through that is the one thing they did not ask
            # for.
            self.release_gpu("dictation paused")
        self.ui.set_tray_state("paused" if paused else "idle")
        self.ui.refresh_tray()
        log(f"Dictation {'paused' if paused else 'resumed'}")

    # ── Hotkeys ──────────────────────────────────────────────────────────

    def _on_press(self, key):
        """Hand the key to _key_worker rather than acting on it here.

        On macOS this runs inside the event tap: pynput dispatches on_press
        straight from the tap callback, so anything slow here is time the whole
        machine spends waiting for us, and overrunning the timeout is what got
        the tap disabled and the hotkey killed once already. capture_target
        alone — an Accessibility round trip — can outlast that budget.

        The timestamp is taken here rather than in the worker, so a double tap
        stays a measurement of the user's hand and not of our scheduling.
        """
        if self._suppress_keys:
            return
        now = time.time()
        self._key_work.put(lambda: self._handle_key(key, now))

    def _key_worker(self):
        """Runs everything the key hooks decided to do.

        Both hooks sit in front of every keystroke on the machine — a
        WH_KEYBOARD_LL hook on Windows, a CGEventTap on macOS. Work done there
        does not merely slow this app down; overrun the OS budget and the hook
        is torn down or the tap disabled, and that costs the hotkey. So the
        hooks decide and return, and everything real happens on this thread.
        """
        while True:
            fn = self._key_work.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as e:
                log(f"Hotkey error: {e}")

    def _stop_by_key(self, discard):
        """What a swallowed Enter or Escape actually does. Queued, never run by
        the hook itself — see _key_worker."""
        if discard:
            self.stop_recording(discard=True)
            self.clear_error()
            self.ui.call_soon(self.ui.hide)
            log("Recording discarded by Escape")
        else:
            self.stop_recording(send_enter=True)
            log("Recording stopped by Enter (will send Enter after transcription)")

    def _swallow(self, is_up, code, marker):
        """Whether this key event should be eaten, plus the bookkeeping that
        goes with saying yes.

        Split out of the two platform hooks because it is the entire decision
        and worth being able to exercise without a keyboard: `is_up` says
        whether this is a release, `code` is the platform's key code, `marker`
        whatever the event carries to identify who synthesised it.

        Never raises. A hook that throws is a hook the OS may take away.
        """
        try:
            if code not in _SWALLOWED_KEYS:
                return False
            if self._event_marker is not None and marker == self._event_marker:
                return False                    # our own synthetic typing
            if is_up:
                # Swallow the release of a press we swallowed, so no app ever
                # sees half a keystroke. Checked before `recording`, which by
                # now is already False — the release arrives well after the
                # press that ended the dictation.
                if code in self._swallowed:
                    self._swallowed.discard(code)
                    return True
                return False
            if not self.recording or self._suppress_keys:
                return False
            self._swallowed.add(code)
            self._key_work.put(
                lambda: self._stop_by_key(discard=code == _DISCARD_KEY))
            return True
        except Exception as e:
            if not self._hook_error_logged:
                self._hook_error_logged = True
                log(f"Key hook error: {e} — keys will not be swallowed")
            return False

    def _win32_filter(self, msg, data):
        """Consume Enter/Escape while recording instead of only observing them.

        pynput's Windows listener is a WH_KEYBOARD_LL hook and this filter runs
        inside it, in front of every keystroke on the machine. suppress_event()
        raises, and that exception is precisely how the hook comes to return 1
        and the key reaches no application at all — so it is called outside any
        try, where nothing can swallow it by accident.
        """
        if msg not in _KEY_DOWN_MESSAGES and msg not in _KEY_UP_MESSAGES:
            return True
        if self._swallow(msg in _KEY_UP_MESSAGES, data.vkCode, data.dwExtraInfo):
            self._listener.suppress_event()
        return True

    def _intercept(self, event_type, event):
        """Consume Enter/Escape while recording instead of only observing them.

        pynput's listener is otherwise a ListenOnly tap: it sees a keystroke
        but the keystroke still reaches whatever is focused. So pressing Enter
        to finish a dictation also dropped a newline into the document the
        transcript was about to be typed into. Returning anything other than
        the event suppresses it system wide.

        This runs on the event tap's thread, so it decides and returns — the
        stop itself is queued for _key_worker. It also has to notice macOS
        switching the tap off, because pynput never will.
        """
        try:
            if (event_type & 0xFFFFFFFF) in _TAP_DISABLED:
                tap = getattr(self._listener, "tap", None)
                if tap is None:
                    log("The system switched the key tap off and it cannot be "
                        "revived — restart WhisperType to get the hotkey back")
                else:
                    Quartz.CGEventTapEnable(tap, True)
                    log("The system switched the key tap off (a callback "
                        "overran) — re-enabled")
                return event
            if self._suppress_keys:
                return event                    # our own synthetic typing
            if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return event
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            marker = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGEventSourceUserData)
            if self._swallow(event_type == Quartz.kCGEventKeyUp, keycode, marker):
                return None
            return event
        except Exception as e:
            log(f"Key intercept error: {e}")
            return event                        # never eat a key by accident

    def _handle_key(self, key, now=None):
        if self._suppress_keys:
            return
        K = pynput.keyboard.Key

        # Scoped to the states where the user is actually interacting with the
        # overlay. Gating on "overlay visible" meant every space bar pressed in
        # another app during a transcription toggled history mode.
        # History is a menu-bar item now. Binding it to Space meant a key you
        # press constantly was being watched globally for the whole time the
        # overlay happened to be up.

        # While recording, Escape belongs to the hook that swallows it, and
        # never arrives here at all. Outside a recording it only hides the
        # overlay, which is harmless to let through to whatever the user is
        # doing.
        if key == K.esc and self.ui.visible and not (self._can_suppress and self.recording):
            if self.recording:
                self.stop_recording(discard=True)
            self.clear_error()
            self.ui.call_soon(self.ui.hide)
            return

        # Where the listener can swallow keys, Enter during a recording is
        # handled by the hook instead — _win32_filter or _intercept, both by
        # way of _swallow. Handling it here as well would stop the recording
        # twice, and only on the platforms where it already works.
        if key == K.enter and self.recording and not self._can_suppress:
            self.stop_recording(send_enter=True)
            log("Recording stopped by Enter (will send Enter after transcription)")
            return

        if key != self.ptt_key or self.model_switching or self.paused:
            return

        # Passed in by the hook when there is one: the callbacks queue their
        # work, and a double tap is a measurement of the user's hand.
        now = time.time() if now is None else now
        if self.recording:
            self.stop_recording()
            log("Recording stopped by keypress")
        else:
            if (now - self._last_tap) * 1000 < DOUBLE_TAP_MS:
                self.start_recording()
            self._last_tap = now
