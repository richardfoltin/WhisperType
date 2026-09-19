"""The CUDA engine, moved out of the daemon's own process.

On 2026-08-18 a fast-fail inside nvcuda64.dll (0xc0000409, stack-cookie check)
killed WhisperType mid-decode. Nothing in Python could have caught it: a
fast-fail is the OS terminating the process on the spot, with no exception, no
unwinding and no handler. The only defence against a native fault in a library
is to not share a process with it.

So the decoder lives here, in a child process. When the CUDA driver falls over
it takes this process down and the daemon notices, restarts it and retries the
clip — from memory, because the parent is still holding the audio, and from
the spool if even that fails.

Two things the transport has to get right:

* Nothing except the protocol may reach the child's stdout. torch and whisper
  both print, and one stray line in the middle of a frame desynchronises the
  stream permanently — so fd 1 is duplicated for the wire and then pointed at
  stderr, which the parent drains into the log.
* Nothing the UI asks may cross the pipe. The tray reads `loaded`, `catalog()`
  and `is_downloaded()` while a two-minute decode is in flight, and a request
  that queued behind that decode would freeze the menu for two minutes. All
  three are answered from the parent's own state.

Run as: python -m whispertype.engine_proc
"""
import os
import pickle
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .log import log

#: Frame = 4-byte big-endian length, then a pickle. Length-prefixed because a
#: pipe is a byte stream: without a frame boundary a 4.5 MB clip and the reply
#: that follows it are indistinguishable from one another.
_HEADER = struct.Struct("!I")

#: Exit codes worth naming. A bare "-1073740791" in a log is a dead end; the
#: name is what tells you the driver fast-failed rather than, say, running out
#: of memory.
_NTSTATUS = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000374: "HEAP_CORRUPTION",
    0xC0000409: "STACK_BUFFER_OVERRUN (fast-fail)",
    0xC0000602: "FAIL_FAST_EXCEPTION",
}

#: Lines of the child's stderr kept for the post-mortem. Enough to hold a torch
#: traceback, short enough not to grow without bound over days of uptime.
_STDERR_TAIL = 40


class EngineCrashed(RuntimeError):
    """The decoder process died. Distinct from a decode that merely raised:
    this one is worth restarting and retrying, that one is not."""


def _send(stream, obj):
    blob = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(blob)))
    stream.write(blob)
    stream.flush()


def _read_exactly(stream, n):
    """Pipes are allowed to return short reads; a frame is not."""
    chunks = []
    while n > 0:
        block = stream.read(n)
        if not block:
            return None                  # EOF: the far end is gone
        chunks.append(block)
        n -= len(block)
    return b"".join(chunks)


def _recv(stream):
    head = _read_exactly(stream, _HEADER.size)
    if head is None:
        return None
    body = _read_exactly(stream, _HEADER.unpack(head)[0])
    if body is None:
        return None
    return pickle.loads(body)


def _child_executable():
    """python.exe, even when the daemon itself is running under pythonw.

    pythonw exists to avoid a console window, and it gets there by leaving the
    standard handles unattached — which is the opposite of what a child holding
    three pipes needs. CREATE_NO_WINDOW keeps the console-mode child just as
    invisible, without the stdio quirks.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def _exit_label(code):
    if code is None:
        return "still running"
    unsigned = code & 0xFFFFFFFF
    name = _NTSTATUS.get(unsigned)
    return f"exit 0x{unsigned:08X}" + (f" {name}" if name else f" ({code})")


# ── Child ────────────────────────────────────────────────────────────────────

def _serve():
    """Serve requests until stdin closes. Runs in the child."""
    # The wire is a private duplicate of fd 1; fd 1 itself is redirected onto
    # stderr so that anything printing to stdout — torch warnings, tqdm, a
    # stray print in a dependency — lands in the log instead of corrupting a
    # frame. This has to happen before whisper is imported.
    wire_fd = os.dup(1)
    os.dup2(2, 1)
    out = os.fdopen(wire_fd, "wb")
    inp = os.fdopen(os.dup(0), "rb")
    sys.stdout = sys.stderr

    from .transcribe import WhisperEngine

    engine = None
    while True:
        request = _recv(inp)
        if request is None:
            break                        # parent closed the pipe: quit
        seq, method, args = request
        try:
            if method == "__hello__":
                engine = WhisperEngine()
                result = {"device_label": engine.device_label,
                          "device": engine.device, "fp16": engine.fp16,
                          "pid": os.getpid()}
            elif method == "__bye__":
                _send(out, (seq, "ok", None))
                break
            elif method == "transcribe":
                audio, language, fp16 = args
                engine.fp16 = fp16
                result = engine.transcribe(audio, language)
            elif method == "transcribe_with":
                handle, audio, language, fp16 = args
                engine.fp16 = fp16
                result = engine.transcribe_with(handle, audio, language)
            else:
                result = getattr(engine, method)(*args)
            _send(out, (seq, "ok", result))
        except BaseException as e:       # noqa: BLE001 - the parent decides
            # Reported rather than raised: a decode that fails on one clip must
            # not cost the model load that the next one would reuse.
            _send(out, (seq, "err", f"{type(e).__name__}: {e}"))


def _download(name):
    """One-shot model fetch, in its own process. Runs in the child."""
    from .transcribe import WhisperEngine
    WhisperEngine().predownload(name)


# ── Parent ───────────────────────────────────────────────────────────────────

class RemoteWhisperEngine:
    """Parent-side stand-in for WhisperEngine, same interface.

    Every method that touches the GPU is a round trip; everything the UI reads
    is local state, so the tray keeps working while a decode runs and keeps
    working after the decoder has died.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._stderr_tail = deque(maxlen=_STDERR_TAIL)
        self._seq = 0
        #: Mirrors of the child's state, so `loaded` never waits on a pipe.
        self._loaded_name = None
        self._closing = False
        self.fp16 = True
        self.device_label = "CUDA"
        self.device = "cuda"
        self._start()

    # ── Process lifecycle ──

    def _start(self):
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        # Unbuffered: the child's stderr is this process's only view of what
        # the decoder is doing, and a buffered crash message is no message.
        env["PYTHONUNBUFFERED"] = "1"
        # And UTF-8, or the child encodes its log lines in the console's ANSI
        # code page (cp1250 here) while this end decodes them as UTF-8 — which
        # turns every dash and accented character in the decoder's output into
        # a replacement character on its way into the log.
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._closing = False
        self._stderr_tail.clear()
        self._proc = subprocess.Popen(
            [_child_executable(), "-m", "whispertype.engine_proc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(root), env=env, **kwargs)
        threading.Thread(target=self._drain_stderr, args=(self._proc,),
                         daemon=True).start()

        t0 = time.perf_counter()
        info = self._call("__hello__")
        self.device_label = info.get("device_label", "CUDA")
        self.device = info.get("device", "cuda")
        # The child decides the fp16 default: it is the process that can see
        # the compute capability.
        self.fp16 = bool(info.get("fp16", True))
        log(f"Decoder process {info.get('pid')} up on {self.device} "
            f"({time.perf_counter() - t0:.1f}s) — a driver crash in here can "
            f"no longer take the daemon with it")

    def _drain_stderr(self, proc):
        """Copy the child's stderr into the log, and keep a tail for the
        post-mortem. Its own thread: the pipe must keep draining while the
        parent is blocked waiting for a reply, or a chatty decode deadlocks."""
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                log(f"[decoder] {line}")
        except Exception:
            pass

    def _death_note(self):
        proc = self._proc
        if proc is None:
            return "the decoder process was never started"
        try:
            code = proc.wait(timeout=5.0)
        except Exception:
            code = proc.poll()
        note = f"decoder process died, {_exit_label(code)}"
        tail = [l for l in self._stderr_tail][-6:]
        if tail:
            note += " | last output: " + " / ".join(tail)
        return note

    def close(self, note="Decoder process stopped"):
        """Shut the child down. Called on quit, when the engine is swapped and
        on an idle release — without it the old decoder keeps its VRAM, and its
        host memory, for the rest of the session.
        """
        with self._lock:
            self._closing = True
            proc, self._proc = self._proc, None
            self._loaded_name = None
        if proc is None or proc.poll() is not None:
            return
        try:
            self._seq += 1
            _send(proc.stdin, (self._seq, "__bye__", ()))
            proc.wait(timeout=10.0)
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        log(note)

    def _discard(self, why):
        """Drop a child that must not serve another request, without waiting
        for it to agree.

        Used after the child reports a failure that leaves it unfit rather than
        merely unlucky. `close` asks politely and waits ten seconds for an
        answer; a decoder that just ran out of memory part-way through building
        a model is exactly the one that will not answer.
        """
        with self._lock:
            proc, self._proc = self._proc, None
            self._loaded_name = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        log(f"Decoder process discarded — {why}")

    def _ensure_up(self):
        """Guarantee there is a live child to talk to.

        Having none is now an ordinary state, not an error: an idle release
        drops the whole decoder, so the first dictation after a quiet ten
        minutes arrives here with nothing running. A child that died since the
        last call lands here too, which is what stops one crash from making
        every later dictation fail.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        if proc is not None:
            log(f"Decoder process is gone ({self._death_note()}) — "
                f"starting a fresh one")
        self._loaded_name = None
        self._start()

    def _restart(self):
        """Bring a fresh child up and put back whatever was loaded in the old
        one, so the caller's retry sees the state it expects."""
        want = self._loaded_name
        self._loaded_name = None
        old = self._proc
        if old is not None and old.poll() is None:
            old.kill()
        self._start()
        if want:
            log(f"Reloading {want} in the new decoder process")
            self._call("load", want)
            self._loaded_name = want

    # ── Transport ──

    def _call(self, method, *args):
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise EngineCrashed(self._death_note())
            self._seq += 1
            seq = self._seq
            try:
                _send(proc.stdin, (seq, method, args))
                reply = _recv(proc.stdout)
            except (BrokenPipeError, OSError, ValueError) as e:
                self._loaded_name = None
                raise EngineCrashed(f"{self._death_note()} [{e}]") from None
            if reply is None:
                self._loaded_name = None
                raise EngineCrashed(self._death_note())
            rseq, status, payload = reply
            if rseq != seq:
                # Can only happen if a frame was lost, and every later reply
                # would be off by one. Kill it rather than return one job's
                # transcript as another's.
                self._loaded_name = None
                raise EngineCrashed(
                    f"decoder replied to {rseq} while {seq} was outstanding")
            if status == "err":
                raise RuntimeError(payload)
            return payload

    # ── Engine interface: local answers ──

    @property
    def loaded(self):
        return self._loaded_name is not None

    def catalog(self):
        from .transcribe import local_catalog
        return local_catalog()

    def is_downloaded(self, name):
        from .transcribe import local_is_downloaded
        return local_is_downloaded(name)

    # ── Engine interface: round trips ──

    def load(self, name):
        """Put a model in the decoder, starting or replacing it as needed.

        `transcribe` has always restarted a dead decoder; this did not, and
        that asymmetry is what turned one crash into a permanent failure. The
        model is reloaded on the first dictation after every idle release, so
        this is the call the daemon makes most — and once the child was gone it
        raised the same post-mortem for every dictation from then on, without
        ever trying to start a replacement.
        """
        # Two attempts, so that a decoder which died while nothing was
        # watching costs one restart rather than every dictation from here on.
        # EngineCrashed is caught before RuntimeError because it is one.
        with self._lock:
            for last in (False, True):
                self._ensure_up()
                try:
                    self._call("load", name)
                except EngineCrashed as e:
                    if last:
                        raise
                    log(f"Decoder lost while loading {name}: {e}")
                    log("Starting a fresh one and loading it again")
                    # Not left to _ensure_up: the pipe can break while the
                    # child is still running, and reusing a process whose
                    # stream is out of step would answer one job with
                    # another's transcript.
                    self._restart()
                    continue
                except RuntimeError:
                    # The child lived to report this, so the transport is fine
                    # — but a load that failed part-way leaves torch and the
                    # CUDA context half-built, and the next call into that
                    # process did not raise, it faulted: the MemoryError of
                    # 2026-09-01 12:24:21 was followed by an ACCESS_VIOLATION
                    # on the very next load, in the same pid. It is unfit now;
                    # the caller's next attempt gets a new one.
                    self._discard(f"the {name} load failed in it")
                    raise
                self._loaded_name = name
                return

    def unload(self):
        """Release the model — by dropping the decoder that holds it.

        Sending "unload" freed the weights and left the child sitting on its
        CUDA context and its host heap, and every later dictation reloaded into
        that same process. Measured on the GTX 1070 Ti, one decoder:

            model resident      child commit 9023 MB   system commit free 2.5 GB
            "unload" only       child commit 4291 MB   system commit free 7.2 GB
            process dropped     child gone             system commit free 11.6 GB

        So the old release handed back 4.7 GB and kept 4.3 GB — a CUDA context
        and a torch heap — for the ten-plus idle minutes until the next
        dictation, on a machine that sits at 81% memory load. Dropping the
        process hands back the other 4.3 GB as well, and stops the slow ratchet
        that came with reusing one child all day: peak commit in a single
        long-lived decoder climbed 727 MB over six load/release cycles, and it
        was after eleven of them, over 24 hours, that a reload finally did not
        fit and raised MemoryError.

        What this does NOT do is make the load itself cheap. A resident model
        needs about 9 GB of commit on this 8 GB card, whichever process it is
        loaded into, so a machine short of commit can still fail the load. That
        is why `load` above recovers instead of merely reporting: the fix for
        the MemoryError is that it no longer escalates into an ACCESS_VIOLATION
        and no longer leaves every later dictation failing.

        The cost is a process start on the first dictation after an idle
        period, measured at 1.4-8.2 s, on top of the model load already paid
        for there.
        """
        if self._proc is None:
            self._loaded_name = None
            return
        self.close("Decoder process released — idle")

    def free_cache(self):
        try:
            self._call("free_cache")
        except EngineCrashed:
            pass

    def predownload(self, name):
        """Fetch a model in a throwaway process of its own.

        Not a round trip: a multi-gigabyte download would hold the lock, and
        with it every dictation, for as long as it takes.
        """
        root = Path(__file__).resolve().parent.parent
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            [_child_executable(), "-m", "whispertype.engine_proc",
             "--download", name],
            cwd=str(root), capture_output=True, **kwargs)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"download of {name} failed "
                               f"({_exit_label(proc.returncode)}): "
                               f"{err[-1] if err else 'no output'}")

    def transcribe(self, audio_bytes, language):
        """Decode, and survive the decoder dying while doing it.

        Retried exactly once. The fault this guards against is reproducible
        often enough that a second failure means the clip or the driver is the
        problem, and a loop here would burn the GPU on it forever — the spooled
        WAV is what carries the recording from there.
        """
        # Held across the retry so nothing else can talk to a decoder that is
        # being replaced. Reentrant: _call and _restart take it again.
        with self._lock:
            self._ensure_up()
            try:
                return self._call("transcribe", audio_bytes, language, self.fp16)
            except EngineCrashed as e:
                log(f"Decoder lost during transcription: {e}")
                log("Restarting it and retrying the clip once — the audio is "
                    "still in memory here, nothing is lost yet")
                self._restart()
                return self._call("transcribe", audio_bytes, language, self.fp16)

    def load_for_benchmark(self, name):
        # The benchmark unloads the dictation model first, which now takes the
        # process with it.
        with self._lock:
            self._ensure_up()
            return self._call("load_for_benchmark", name)

    def release_benchmark_model(self):
        try:
            self._call("release_benchmark_model")
        except EngineCrashed:
            pass

    def transcribe_with(self, handle, audio_bytes, language):
        return self._call("transcribe_with", handle, audio_bytes,
                          language, self.fp16)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--download":
        _download(sys.argv[2])
    else:
        _serve()
