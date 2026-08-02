"""Transcription queue, job records and history — platform independent."""
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

MAX_HISTORY = 50


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
        """Mark cancelled *and* drop from the visible list. The worker checks
        the status when it dequeues, so a queued job is really skipped."""
        with self._lock:
            job.status = JobStatus.CANCELLED
            if job in self._active:
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

    def add_history(self, entry):
        with self._history_lock:
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                self._history.pop(0)

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
