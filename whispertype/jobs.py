"""Transcription queue, job records and history — platform independent."""
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .log import log

MAX_HISTORY = 50

#: History is where a transcript lands when it cannot be typed (target app
#: gone, secure input, permission revoked), so it has to survive a quit and a
#: login — otherwise the documented fallback loses exactly the text the user
#: most needs to recover.
HISTORY_PATH = Path.home() / ".whispertype" / "history.json"


class JobStatus(Enum):
    WAITING = "waiting"
    TRANSCRIBING = "transcribing"
    CANCELLED = "cancelled"


@dataclass
class ModelSwitch:
    """Control message routed through the job queue.

    MLX is thread-affine: the thread that loads a model must be the one that
    evaluates it. Sending the switch down the same queue as the jobs keeps all
    model work on the single transcription worker thread, and makes switching
    mid-queue impossible by construction.
    """
    name: str


@dataclass
class ModelUnload:
    """Control message: release the model to reclaim memory after an idle
    period. Goes through the queue for the same reason ModelSwitch does — MLX
    will not let another thread free what the worker allocated."""


@dataclass
class TranscriptionJob:
    job_id: int
    audio_bytes: bytes
    target: object          # opaque platform target handle (HWND / MacTarget)
    window_name: str
    app_name: str = ""
    audio_duration: float = 0.0
    status: JobStatus = JobStatus.WAITING
    created_at: float = field(default_factory=time.time)
    send_enter: bool = False


@dataclass
class BenchmarkResult:
    """One model's run over the benchmark clip."""
    model: str
    #: waiting -> loading -> running -> done | error
    status: str = "waiting"
    load_secs: float = None
    transcribe_secs: float = None
    text: str = None
    error: str = None


@dataclass
class BenchmarkJob:
    """Run one recording through every downloaded model and compare.

    Carries no delivery target: a benchmark is never typed anywhere, so there
    is no reason to hold a live window handle alive for the several minutes a
    full run takes. window_name/app_name are kept for the record only.
    """
    job_id: int
    audio_bytes: bytes
    audio_duration: float
    window_name: str = ""
    app_name: str = ""
    created_at: float = field(default_factory=time.time)
    results: list = field(default_factory=list)


class JobQueue:
    """Owns the pending queue, the visible job list and the history list.

    All three used to be module-level globals guarded by ad-hoc locks; keeping
    them together makes the cancel path correct (the original removed a job
    from the visible list but left it in the queue, so it was still typed out).
    """

    def __init__(self):
        self._q = queue.Queue()
        self._active = []
        self._lock = threading.Lock()
        self._history = []
        self._history_lock = threading.Lock()
        self._counter = 0

    # ── Job lifecycle ──

    def next_id(self):
        with self._lock:
            self._counter += 1
            return self._counter

    def submit(self, job):
        with self._lock:
            self._active.append(job)
        self._q.put(job)

    def submit_control(self, msg):
        """Queue a control message (e.g. ModelSwitch) without showing it in the
        visible job list."""
        self._q.put(msg)

    def take(self):
        """Block for the next job. Returns None for a cancelled job so the
        worker can skip it without transcribing."""
        return self._q.get()

    def done(self):
        self._q.task_done()

    def finish(self, job):
        with self._lock:
            if job in self._active:
                self._active.remove(job)

    def cancel(self, job):
        """Mark cancelled. The worker checks the status when it dequeues, so a
        queued job is really skipped rather than merely hidden.

        A job that is already TRANSCRIBING stays in the active list until the
        worker retires it: busy() is what quit() consults to decide whether to
        wait, and dropping a running job early would let the UI be torn down
        while the model is still working.
        """
        with self._lock:
            was_waiting = job.status == JobStatus.WAITING
            job.status = JobStatus.CANCELLED
            if was_waiting and job in self._active:
                self._active.remove(job)

    def set_status(self, job, status):
        with self._lock:
            if job.status != JobStatus.CANCELLED:
                job.status = status

    # ── Queries ──

    def active(self):
        with self._lock:
            return list(self._active)

    def active_count(self):
        with self._lock:
            return len(self._active)

    def busy(self):
        """True while anything is queued or in flight."""
        with self._lock:
            return bool(self._active)

    def pending(self):
        return self._q.qsize()

    def join(self):
        self._q.join()

    # ── History ──

    def load_history(self, path=HISTORY_PATH):
        self._history_path = path
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    with self._history_lock:
                        self._history = [i for i in items if isinstance(i, dict)][-MAX_HISTORY:]
                    log(f"Loaded {len(self._history)} history entries")
        except Exception as e:
            log(f"Could not read history ({e}) — starting empty")

    def _save_history(self):
        path = getattr(self, "_history_path", HISTORY_PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._history, f, ensure_ascii=False, indent=1)
            tmp.replace(path)          # atomic: a crash cannot truncate history
        except Exception as e:
            log(f"Could not save history: {e}")

    def add_history(self, entry):
        with self._history_lock:
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                self._history.pop(0)
            self._save_history()

    def history(self):
        with self._history_lock:
            return list(self._history)

    def history_count(self):
        with self._history_lock:
            return len(self._history)

    def delete_history(self, idx):
        with self._history_lock:
            if 0 <= idx < len(self._history):
                self._history.pop(idx)
                self._save_history()

    def clear_history(self):
        with self._history_lock:
            self._history = []
            self._save_history()
