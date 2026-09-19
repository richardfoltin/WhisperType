# Streaming Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decode a dictation while the microphone is still open, so that releasing the key leaves at most one segment outstanding instead of a whole clip.

**Architecture:** The capture loop hands closed segments to a callback as it finds silence to cut at; each segment becomes a partial `TranscriptionJob` on the existing queue and is decoded during capture. A `DictationSession` collects the numbered results, and only when the last one lands is a single history entry written and the joined text delivered — through the existing `_deliver` path, unchanged.

**Tech Stack:** Python 3.12, openai-whisper on CUDA (in the `engine_proc` child process), PyAudio capture, Tkinter overlay and settings window on Windows, AppKit on macOS.

**Spec:** `docs/superpowers/specs/2026-09-08-streaming-transcription-design.md`

## Global Constraints

- **Python 3.12 only.** The Bash `python` on this machine is 3.10 and has neither whisper nor torch. Every run command uses `"/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe"` with `PYTHONIOENCODING=utf-8` set.
- **Probe scripts go in `.tmp/`.** That directory is gitignored and writable from every scope; per `CLAUDE.md` nothing temporary may land anywhere else. The existing probes (`.tmp/test_spool.py`, `.tmp/test_engine_proc.py`) set the style: plain `assert` plus `print`, run directly, ending in a `ALL … TESTS PASSED` line. There is no pytest suite in this repo and this plan does not introduce one.
- **`sys.path` bootstrap.** Probe scripts start with the three lines every existing probe starts with, so they import the package from the repo root.
- **Committing.** This repo commits through the spec-hub's automatic commits and the `/commit` skill. Do not hand-write `git commit` calls; finish a task and let the repo's own flow take it.
- **Scope discipline.** Every file this plan touches is under `whispertype/`. A session must be bound to that scope before writing (see `.claude/skills/scope-bind/`). Never work around a guard denial.
- **`stream_transcription: false` must restore today's behaviour exactly.** Every task keeps the non-streaming path reachable and untouched.
- **Decode options are not to be changed.** `DECODE_OPTIONS` in `transcribe.py` — especially `condition_on_previous_text=False` — is the premise the whole design rests on.

---

### Task 1: `SegmentCutter`

The cutting decision, isolated from PortAudio and from Whisper so it can be tested from a synthetic level sequence.

**Files:**
- Modify: `whispertype/audio.py` (add the class above `class Capture`, around line 212)
- Test: `.tmp/test_segment_cutter.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `audio.SegmentCutter(rate, chunk, *, threshold, min_seconds, max_seconds, cut_silence)` with `feed(data: bytes, rms: float) -> bytes | None` and `flush() -> bytes | None`.

- [x] **Step 1: Write the failing probe**

Create `.tmp/test_segment_cutter.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whispertype.audio import SegmentCutter

RATE, CHUNK = 16000, 1600          # 0.1 s per chunk, so the maths is readable
LOUD, QUIET = 500.0, 10.0

def cutter(**kw):
    opts = dict(threshold=200.0, min_seconds=3.0, max_seconds=6.0,
                cut_silence=0.5)
    opts.update(kw)
    return SegmentCutter(RATE, CHUNK, **opts)

def feed(c, seconds, rms, tag=b"x"):
    """Push `seconds` of chunks at one level. Returns the segments it closed."""
    out = []
    for _ in range(int(seconds / 0.1)):
        seg = c.feed(tag * CHUNK * 2, rms)
        if seg is not None:
            out.append(seg)
    return out

# 1. Below the floor nothing is ever cut, however long the silence.
#    Note the floor counts ALL accumulated audio, not just the speech in it —
#    1.0 s of speech plus 1.5 s of pause is 2.5 s, still under the 3 s floor.
c = cutter()
assert feed(c, 1.0, LOUD) == []
assert feed(c, 1.5, QUIET) == [], "cut below the 3 s floor"
print("floor respected")

# 2. Past the floor, the first qualifying pause cuts.
c = cutter()
assert feed(c, 3.5, LOUD) == []
segs = feed(c, 0.5, QUIET)
assert len(segs) == 1, segs
# 3.5 s of speech + the 0.5 s pause that closed it = 4.0 s of audio.
assert len(segs[0]) == int(4.0 / 0.1) * CHUNK * 2, len(segs[0])
print("cuts at the first qualifying pause")

# 3. A pause shorter than cut_silence is not a cut point.
c = cutter()
feed(c, 3.5, LOUD)
assert feed(c, 0.3, QUIET) == []
assert feed(c, 1.0, LOUD) == []
print("short pause ignored")

# 4. At the ceiling it cuts anyway — at the quietest chunk seen, not blind.
c = cutter()
feed(c, 3.5, LOUD)
c.feed(b"q" * CHUNK * 2, 210.0)     # above threshold, but the local minimum
segs = feed(c, 3.0, LOUD)
assert len(segs) == 1, segs
# The quietest chunk was the 36th (3.5 s = 35 chunks, then that one).
assert len(segs[0]) == 36 * CHUNK * 2, len(segs[0])
print("ceiling cuts at the quietest candidate")

# 5. Speech is required: a silent recording is never cut into pieces.
c = cutter()
assert feed(c, 10.0, QUIET) == [], "cut a recording with no speech in it"
print("silence alone never cuts")

# 6. flush returns the tail once a cut has been made and it holds speech.
c = cutter()
feed(c, 3.5, LOUD)
feed(c, 0.5, QUIET)
feed(c, 1.0, LOUD)
tail = c.flush()
assert tail is not None and len(tail) == int(1.0 / 0.1) * CHUNK * 2, tail and len(tail)
assert c.flush() is None, "flush is not idempotent"
print("flush returns a spoken tail")

# 7. A silence-only tail after a cut is dropped: Whisper answers a call that is
#    all padding with an invented stock phrase.
c = cutter()
feed(c, 3.5, LOUD)
feed(c, 0.5, QUIET)
feed(c, 1.0, QUIET)
assert c.flush() is None, "silent tail was kept"
print("silent tail dropped")

# 8. But when no cut was ever made, flush returns the whole clip even if it is
#    silent — min_speech_seconds owns that decision, not the cutter.
c = cutter()
feed(c, 1.0, QUIET)
assert c.flush() is not None, "uncut silent clip was swallowed"
print("uncut clip always returned")

print("\nALL SEGMENT CUTTER TESTS PASSED")
```

- [x] **Step 2: Run it to see it fail**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_segment_cutter.py
```

Expected: `ImportError: cannot import name 'SegmentCutter' from 'whispertype.audio'`

- [x] **Step 3: Implement `SegmentCutter`**

Add to `whispertype/audio.py`, immediately above `class Capture`:

```python
class SegmentCutter:
    """Decides where a recording may be split into separately-decodable pieces.

    Fed the (chunk, rms) pair the capture loop already computes, and answers
    one question: close the segment here, or keep going. It holds no state
    beyond the segment it is accumulating, touches neither PortAudio nor
    Whisper, and is therefore testable from a synthetic level sequence.

    The rule is a floor, a pause and a ceiling.

    The FLOOR exists because Whisper pads every call out to 30 seconds, and a
    segment that is mostly padding is exactly where it invents a stock phrase.
    No cut may leave one behind.

    The PAUSE exists because the only thing splitting audio can genuinely
    break is a word cut in half. There is no textual context to lose:
    DECODE_OPTIONS sets condition_on_previous_text=False, so Whisper already
    decodes each 30-second window independently.

    The CEILING exists so that speech with no usable pause in it is still cut
    eventually — but even then at the quietest chunk seen since the floor,
    never at an arbitrary offset.
    """

    def __init__(self, rate, chunk, *, threshold, min_seconds, max_seconds,
                 cut_silence):
        self._chunk_seconds = chunk / float(rate)
        self._threshold = threshold
        self._min_chunks = max(1, int(min_seconds / self._chunk_seconds))
        self._max_chunks = max(self._min_chunks,
                               int(max_seconds / self._chunk_seconds))
        self._silence_chunks = max(1, int(cut_silence / self._chunk_seconds))
        #: Survives _reset: once a recording has been cut, its tail is a tail
        #: rather than a whole clip, and flush() treats the two differently.
        self._cut_made = False
        self._reset()

    def _reset(self):
        self._frames = []
        self._speech = False
        self._silence_run = 0
        self._best_rms = None
        self._best_idx = None

    def feed(self, data, rms):
        """Take one captured chunk. Returns a closed segment, or None."""
        self._frames.append(data)
        if rms >= self._threshold:
            self._speech = True
            self._silence_run = 0
        else:
            self._silence_run += 1

        n = len(self._frames)
        # Nothing may be cut before the floor, and no candidate from before it
        # is worth remembering: cutting there could only produce a segment
        # shorter than the floor, which is the case the floor exists to stop.
        if n < self._min_chunks or not self._speech:
            return None

        if self._best_rms is None or rms < self._best_rms:
            self._best_rms, self._best_idx = rms, n

        if self._silence_run >= self._silence_chunks:
            return self._close(n)
        if n >= self._max_chunks:
            return self._close(self._best_idx)
        return None

    def _close(self, upto):
        """Cut at `upto` chunks and carry the remainder into the next segment."""
        segment = b"".join(self._frames[:upto])
        rest = self._frames[upto:]
        self._reset()
        self._cut_made = True
        self._frames = rest
        # `rest` may well contain speech, but it has not been measured under
        # the new segment's accounting — leaving _speech False just means the
        # next segment has to earn its own cut, which is the safe direction.
        return segment

    def flush(self):
        """Whatever is left when capture ends, or None.

        None when there is nothing left, and also when a cut has already been
        made and what remains never rose above the threshold: that tail is
        pure silence, and a silence-only call is precisely what Whisper
        answers with an invented stock phrase.

        When no cut was ever made this is the entire recording, and it is
        returned whatever it contains — min_speech_seconds owns that decision,
        and has owned it since long before this class existed.
        """
        if not self._frames:
            return None
        if self._cut_made and not self._speech:
            self._reset()
            return None
        segment = b"".join(self._frames)
        self._reset()
        return segment
```

- [x] **Step 4: Run the probe to verify it passes**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_segment_cutter.py
```

Expected: eight `print` lines, then `ALL SEGMENT CUTTER TESTS PASSED`.

---

### Task 2: `DictationSession` and session-aware `JobQueue`

**Files:**
- Modify: `whispertype/jobs.py` (add `DictationSession` after `TranscriptionJob`; change `JobQueue.active` and `JobQueue.active_count`)
- Modify: `whispertype/jobs.py` (`TranscriptionJob`: two new fields)
- Test: `.tmp/test_dictation_session.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `jobs.DictationSession(session_id)` with `created_at`, `audio_duration`, `spool_path`, `register(job)`, `jobs()`, `add(index, text)`, `set_final(count, duration)`, `complete()`, `text()`, `progress()`, `cancel()`, `cancelled`. `TranscriptionJob` gains `session=None` and `segment_index=0`.

- [x] **Step 1: Write the failing probe**

Create `.tmp/test_dictation_session.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whispertype.jobs import (DictationSession, JobQueue, JobStatus,
                              TranscriptionJob)

def job(jid, session=None, index=0):
    return TranscriptionJob(job_id=jid, audio_bytes=b"", target=None,
                            window_name="w", session=session,
                            segment_index=index)

# 1. Incomplete until set_final says how many segments there were.
s = DictationSession(1)
s.add(0, "első")
assert not s.complete(), "complete before set_final"
s.add(1, "második")
assert not s.complete()
s.set_final(2, 12.5)
assert s.complete() and s.audio_duration == 12.5
print("complete only after set_final")

# 2. Assembled by index, not by arrival order.
s = DictationSession(2)
s.add(2, "harmadik")
s.add(0, "első")
s.add(1, "második")
s.set_final(3, 9.0)
assert s.text() == "első második harmadik", s.text()
print("assembles out of order")

# 3. Empty parts drop out of the join instead of doubling a space.
s = DictationSession(3)
s.add(0, "első"); s.add(1, ""); s.add(2, "harmadik")
s.set_final(3, 9.0)
assert s.text() == "első harmadik", repr(s.text())
print("empty parts skipped")

# 4. progress() is what the overlay reads.
s = DictationSession(4)
s.add(0, "a")
assert s.progress() == (1, None), s.progress()
s.set_final(3, 9.0)
assert s.progress() == (1, 3), s.progress()
print("progress reports done/total")

# 5. Cancellation is visible and sticky.
s = DictationSession(5)
assert not s.cancelled
s.cancel()
assert s.cancelled
print("cancel sticks")

# 6. A session's jobs are reachable, so cancelling one cancels them all.
s = DictationSession(6)
a, b = job(1, s, 0), job(2, s, 1)
s.register(a); s.register(b)
assert s.jobs() == [a, b]
print("session tracks its jobs")

# 7. The queue collapses a session into one visible row, but stays busy
#    while any of its segments is in flight.
q = JobQueue()
s = DictationSession(7)
a, b, c = job(1, s, 0), job(2, s, 1), job(3, s, 2)
for j in (a, b, c):
    s.register(j)
    q.submit(j)
lone = job(4)
q.submit(lone)
assert q.active_count() == 2, q.active_count()
assert q.active() == [a, lone], q.active()
assert q.busy()
print("session is one visible row")

# 8. Retiring one segment does not retire the dictation.
q.finish(a)
assert q.active() == [b, lone], q.active()
assert q.busy()
q.finish(b); q.finish(c)
assert q.active() == [lone]
print("dictation stays visible until its last segment retires")

# 9. Cancelling a waiting segment drops it from the visible list, exactly as
#    it does for a standalone job.
q2 = JobQueue()
s2 = DictationSession(8)
d = job(9, s2, 0)
s2.register(d); q2.submit(d)
q2.cancel(d)
assert d.status == JobStatus.CANCELLED and not q2.busy()
print("cancelled segment leaves the queue")

print("\nALL DICTATION SESSION TESTS PASSED")
```

- [x] **Step 2: Run it to see it fail**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_dictation_session.py
```

Expected: `ImportError: cannot import name 'DictationSession' from 'whispertype.jobs'`

- [x] **Step 3: Add the two fields to `TranscriptionJob`**

In `whispertype/jobs.py`, inside `@dataclass class TranscriptionJob`, after `spool_path`:

```python
    #: The dictation this job is one segment of, when the clip was decoded
    #: while it was still being recorded. None means the job is the whole
    #: recording, which is what spool recovery and the benchmark still submit
    #: — those paths are unchanged by streaming.
    session: object = None
    segment_index: int = 0
```

- [x] **Step 4: Implement `DictationSession`**

In `whispertype/jobs.py`, after the `TranscriptionJob` dataclass:

```python
class DictationSession:
    """The ordered partial transcripts of one dictation.

    A recording decoded while it is still being made arrives as several jobs,
    and none of the text may be typed until all of them are in — the user
    asked for the wait to shrink, not for half a sentence to land in their
    document. This holds the parts, knows when the set is complete, and joins
    them.

    Locked because the recorder thread registers segments while the worker
    thread is adding their transcripts.
    """

    def __init__(self, session_id):
        self.session_id = session_id
        self.created_at = time.time()
        #: Total seconds of the whole recording, known only once capture ends.
        self.audio_duration = 0.0
        #: The one spooled WAV covering the whole dictation. Dropped when the
        #: joined transcript is in history, the same rule a single job follows.
        self.spool_path = None
        self._parts = {}
        self._jobs = []
        self._final_count = None
        self._cancelled = False
        self._lock = threading.Lock()

    def register(self, job):
        with self._lock:
            self._jobs.append(job)

    def jobs(self):
        """Every segment submitted so far, so cancelling a dictation can
        cancel all of them rather than only the one the user clicked."""
        with self._lock:
            return list(self._jobs)

    def add(self, index, text):
        with self._lock:
            self._parts[index] = text

    def set_final(self, count, duration):
        """Say how many segments the dictation turned out to have, and how
        long it was. Called once, when capture has ended and the last segment
        is queued — until then `complete()` can never be true, which is what
        stops a half-decoded dictation from being typed."""
        with self._lock:
            self._final_count = count
            self.audio_duration = duration

    def cancel(self):
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def complete(self):
        with self._lock:
            return (self._final_count is not None
                    and len(self._parts) >= self._final_count)

    def text(self):
        with self._lock:
            parts = [self._parts[i] for i in sorted(self._parts)
                     if self._parts[i]]
        return " ".join(parts).strip()

    def progress(self):
        """(decoded, total-or-None) — what the overlay shows while it runs."""
        with self._lock:
            return len(self._parts), self._final_count
```

- [x] **Step 5: Make `JobQueue` collapse a session to one row**

Replace `JobQueue.active` and `JobQueue.active_count` in `whispertype/jobs.py`:

```python
    def active(self):
        """One entry per visible unit: a standalone job, or the oldest
        still-active segment of a dictation.

        The overlay opens a queue table as soon as more than one thing is
        active, so a five-minute dictation decoded in eight segments would
        otherwise grow an eight-row panel in the middle of one sentence. The
        segments stay in `_active` regardless — `busy()` and the shutdown wait
        read that list, and both must still see every one of them.
        """
        with self._lock:
            out, seen = [], set()
            for job in self._active:
                session = getattr(job, "session", None)
                if session is None:
                    out.append(job)
                elif session.session_id not in seen:
                    seen.add(session.session_id)
                    out.append(job)
            return out

    def active_count(self):
        # Deliberately not `len(self._active)`: this is what the overlay sizes
        # itself from, so it has to count what the overlay will show.
        return len(self.active())
```

- [x] **Step 6: Run the probe to verify it passes**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_dictation_session.py
```

Expected: nine `print` lines, then `ALL DICTATION SESSION TESTS PASSED`.

---

### Task 3: Config keys and segment emission from the capture loop

**Files:**
- Modify: `whispertype/config.py` (`DEFAULTS` around line 28; four new properties near `min_speech_seconds`, line 156)
- Modify: `whispertype/audio.py` (`record_until_stop`, line 270)
- Test: `.tmp/test_record_segments.py`

**Interfaces:**
- Consumes: `audio.SegmentCutter` from Task 1.
- Produces: `record_until_stop(cfg, stop_event, level_callback=None, on_first_chunk=None, on_segment=None)`. `on_segment(data: bytes)` is called on the recorder thread for each closed segment and once more with the tail before the function returns, but only when the tail is non-empty. `Capture.data` is still the complete clip. `cfg.stream_transcription`, `cfg.segment_min_seconds`, `cfg.segment_max_seconds`, `cfg.segment_cut_silence`.

- [x] **Step 1: Add the config keys**

In `whispertype/config.py`, in `DEFAULTS` after `"min_speech_seconds": 0.25,`:

```python
    #: Decode a dictation while it is still being recorded, in pieces cut at
    #: pauses. Off restores the single-job path exactly.
    "stream_transcription": True,
    #: No cut before this much audio: Whisper pads every call out to 30
    #: seconds, and a segment mostly made of padding is where it hallucinates.
    "segment_min_seconds": 30.0,
    #: How long to wait for a usable pause before cutting at the quietest
    #: point seen instead.
    "segment_max_seconds": 120.0,
    #: How long the level must stay below silence_threshold for the gap to
    #: count as a cut point.
    "segment_cut_silence": 0.5,
```

And the properties, next to `min_speech_seconds`:

```python
    @property
    def stream_transcription(self):
        return bool(self.get("stream_transcription",
                             DEFAULTS["stream_transcription"]))

    @property
    def segment_min_seconds(self):
        # Floored at 1 s only, not at the 30 s the settings slider enforces.
        # This file is hand-editable on purpose and the rest of it trusts what
        # it finds — silence_threshold is read the same way, and the slider
        # widens to a hand-set value rather than clamping it. The 30 s reason
        # is written above the default in DEFAULTS, where someone editing this
        # key will read it.
        return max(1.0, float(self.get("segment_min_seconds",
                                       DEFAULTS["segment_min_seconds"])))

    @property
    def segment_max_seconds(self):
        return max(self.segment_min_seconds,
                   float(self.get("segment_max_seconds",
                                  DEFAULTS["segment_max_seconds"])))

    @property
    def segment_cut_silence(self):
        return max(0.1, float(self.get("segment_cut_silence",
                                       DEFAULTS["segment_cut_silence"])))
```

- [x] **Step 2: Write the failing probe**

Create `.tmp/test_record_segments.py`. It replaces the capture backend with a fake stream, so no microphone is involved:

```python
import sys, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whispertype import audio
from whispertype.config import Config

RATE, CHUNK = 16000, 1600
LOUD = b"\x00\x20" * CHUNK        # ~8192 amplitude -> RMS well over 200
QUIET = b"\x00\x00" * CHUNK       # digital silence

class FakeStream:
    def __init__(self, script):
        self._script = list(script)
    def read(self):
        if not self._script:
            raise StopIteration
        return self._script.pop(0)
    def close(self):
        pass

def run(script, **overrides):
    """Drive record_until_stop over a scripted level sequence."""
    data = {"sample_rate": RATE, "chunk_size": CHUNK,
            "silence_threshold": 200, "silence_duration": 0,
            "max_recording_time": 3600, "stream_transcription": True,
            # 3 s / 6 s rather than the real 30 s / 120 s, so a probe stays a
            # probe: at 0.1 s per chunk the real floor would need 300 of them
            # per case and the arithmetic in the assertions would stop being
            # readable. The rule under test is the same either way.
            "segment_min_seconds": 3.0, "segment_max_seconds": 6.0,
            "segment_cut_silence": 0.5}
    data.update(overrides)
    # Config takes its dict directly; `writable=False` keeps a probe from ever
    # writing over ~/.whispertype/config.json.
    cfg = Config(data, writable=False)

    stream = FakeStream(script)
    audio.open_stream = lambda c, d: stream
    audio.resolve_device = lambda spec: None

    stop = threading.Event()
    segments = []
    # The fake stream runs out; stop the loop from the level callback so the
    # recorder ends the way a real one does rather than on an exception.
    seen = [0]
    def level(_rms):
        seen[0] += 1
        if seen[0] >= len(script):
            stop.set()
    cap = audio.record_until_stop(cfg, stop, level_callback=level,
                                  on_segment=segments.append)
    return cap, segments

# 3.5 s of speech, a 0.5 s pause, then 1.0 s more: one cut plus the tail.
script = [LOUD] * 35 + [QUIET] * 5 + [LOUD] * 10

# 1. Capture still returns the whole clip — that is what the spool writes, and
#    the segments must be a second view of the same bytes, not a replacement.
cap, segs = run(script)
assert cap.data == b"".join(script), len(cap.data)
assert b"".join(segs) == cap.data, "segments do not reconstruct the clip"
print("capture still returns the whole clip")

# 2. One cut at the pause, then the tail.
assert len(segs) == 2, len(segs)
assert len(segs[0]) == 40 * CHUNK * 2, len(segs[0])
assert len(segs[1]) == 10 * CHUNK * 2, len(segs[1])
print("cuts once at the pause, tail follows")

# 3. Streaming disabled: no segments at all, clip unchanged.
cap, segs = run(script, stream_transcription=False)
assert segs == [], segs
assert cap.data == b"".join(script)
print("stream_transcription: false emits nothing")

# 4. A recording under the floor is never cut: one segment, the whole clip.
cap, segs = run([LOUD] * 10 + [QUIET] * 10)
assert len(segs) == 1 and segs[0] == cap.data, len(segs)
print("short recording stays whole")

print("\nALL RECORD SEGMENT TESTS PASSED")
```

- [x] **Step 3: Run it to see it fail**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_record_segments.py
```

Expected: `TypeError: record_until_stop() got an unexpected keyword argument 'on_segment'`

- [x] **Step 4: Teach `record_until_stop` to emit segments**

In `whispertype/audio.py`, change the signature and add the cutter. The docstring gains a paragraph; the loop gains three lines; the `finally` gains the flush.

```python
def record_until_stop(cfg, stop_event, level_callback=None, on_first_chunk=None,
                      on_segment=None):
    """Record until stopped, silent for cfg.silence_duration, or timed out.

    `on_first_chunk` fires once, when audio is genuinely flowing, so the UI can
    start its timer from that instant rather than from the keypress.

    `on_segment` fires whenever enough has been recorded to be decoded on its
    own, cut at a pause — and once more with whatever is left when capture
    ends. Every byte is still accumulated into `Capture.data` regardless, so
    the spool keeps writing one WAV per dictation and the crash story is
    unchanged; the segments are a second, transient view of the same audio.
    """
    dev = resolve_device(cfg.input_device)
    stream = open_stream(cfg, dev)
    frames = []
    silence_since = None
    speech_seconds = 0.0
    peak_rms = 0.0
    first = True
    start = time.time()
    threshold = cfg.silence_threshold
    max_secs = cfg.max_recording_time
    silence_secs = cfg.silence_duration
    chunk_seconds = cfg.chunk / float(cfg.rate)
    reason = "stopped"
    cutter = None
    if on_segment is not None and cfg.stream_transcription:
        cutter = SegmentCutter(cfg.rate, cfg.chunk, threshold=threshold,
                               min_seconds=cfg.segment_min_seconds,
                               max_seconds=cfg.segment_max_seconds,
                               cut_silence=cfg.segment_cut_silence)
    try:
        while not stop_event.is_set():
            data = stream.read()
            if first:
                first = False
                if on_first_chunk:
                    on_first_chunk()
            frames.append(data)
            arr = np.frombuffer(data, np.int16).astype(np.float64)
            rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0
            if rms > peak_rms:
                peak_rms = rms
            if level_callback:
                level_callback(rms)
            if cutter is not None:
                segment = cutter.feed(data, rms)
                if segment is not None:
                    # On the recorder thread, so a slow consumer would stall
                    # capture. The consumer only queues a job.
                    on_segment(segment)
            if rms < threshold:
                if silence_since is None:
                    silence_since = time.time()
                elif silence_secs > 0 and time.time() - silence_since > silence_secs:
                    reason = "silence"
                    break
            else:
                silence_since = None
                speech_seconds += chunk_seconds
            if time.time() - start > max_secs:
                reason = "limit"
                break
    finally:
        try:
            with _pa_lock:
                stream.close()
        except Exception as e:
            log(f"Error closing audio stream: {e}")
        if cutter is not None:
            tail = cutter.flush()
            if tail:
                on_segment(tail)
```

The rest of the function — the logging and the `return Capture(...)` — is unchanged.

- [x] **Step 5: Run the probe to verify it passes**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_record_segments.py
```

Expected: four `print` lines, then `ALL RECORD SEGMENT TESTS PASSED`.

---

### Task 4: Submit segments as partial jobs and assemble them in the worker

The behavioural heart of the feature. After this task a dictation is genuinely decoded while it is being recorded.

**Files:**
- Modify: `whispertype/app.py` — `_record_and_enqueue` (line 1034), `_handle_job` (line 711), `cancel_job` (line 1165)
- Test: `.tmp/test_streaming_pipeline.py`

**Interfaces:**
- Consumes: `audio.SegmentCutter` (Task 1), `jobs.DictationSession` and the collapsed `JobQueue.active` (Task 2), `record_until_stop(..., on_segment=...)` (Task 3).
- Produces: `App._cancel_session(session)`, `App._history_entry(job)`.

- [x] **Step 1: Import `DictationSession`**

In `whispertype/app.py`, add `DictationSession` to the existing `from .jobs import …` line.

- [x] **Step 2: Extract the history entry into a helper**

In `whispertype/app.py`, replace the `entry = {…}` literal at the top of `_handle_job` with a call, and add the helper above `_handle_job`:

```python
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
```

`_handle_job` then opens with `entry = self._history_entry(job)`.

- [x] **Step 3: Assemble the session in `_handle_job`**

In `whispertype/app.py`, immediately after the `log(f"Transcribed (job {job.job_id} …")` line and *before* the `if not text:` check, insert:

```python
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
                job.spool_path = session.spool_path
                log(f"Dictation {session.session_id} complete "
                    f"({done} segments): {text}")
```

From that point the method is unchanged: `text` is the whole transcript, `job` carries the target and `send_enter` of the final segment, and `job.spool_path` is the dictation's one spooled clip.

- [x] **Step 4: Fail the dictation when a segment fails**

Still in `_handle_job`, the `except Exception as e:` branch gains one block at its top, before the existing `entry["text"] = …` line:

```python
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
```

The rest of the branch — the failure entry, `add_history`, `set_error`, and the deliberate *absence* of a `spool.discard` — stays exactly as it is.

- [x] **Step 5: Cancel a whole dictation, never one segment of it**

In `whispertype/app.py`, replace `cancel_job` and add `_cancel_session` beside it:

```python
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
```

- [x] **Step 6: Submit segments from the recorder thread**

In `whispertype/app.py`, rewrite `_record_and_enqueue`. The changes: build a session and an `on_segment` closure before capture; submit each segment as a partial job; at the end finalise or cancel. Every existing early return gains a `_cancel_session` call.

```python
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
```

Note the one race this closes: `set_final` is called *after* `session.spool_path` is assigned, because the worker drops the spool entry the moment the session completes and would otherwise drop nothing while the WAV stayed on disk forever.

- [x] **Step 7: Write the pipeline probe**

Create `.tmp/test_streaming_pipeline.py`. It drives `_handle_job` against a stub engine and a stub UI — no GPU, no Tk:

```python
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whispertype.app import App
from whispertype.jobs import DictationSession, JobQueue, TranscriptionJob

class StubUI:
    history_mode = False
    def __init__(self): self.partials = []; self.ticker = []
    def call_soon(self, fn): fn()
    def call_later(self, ms, fn): pass
    def refresh(self): pass
    def refresh_tray(self): pass
    def set_tray_state(self, s): pass
    def check_hide(self): pass
    def set_ticker(self, t): self.ticker.append(t)
    def set_partial(self, t, done, total): self.partials.append(t)

class StubEngine:
    loaded = True
    def __init__(self, texts): self._texts = list(texts)
    def transcribe(self, audio_bytes, language): return self._texts.pop(0)

def make_app(texts):
    app = App.__new__(App)          # no backend, no UI factory, no model load
    app.jobs = JobQueue()
    app.jobs._history_path = Path(__file__).with_name("history-probe.json")
    app.ui = StubUI()
    app.engine = StubEngine(texts)
    app.cfg = types.SimpleNamespace(language="hu")
    app.shutting_down = False
    app.recording = False
    app.last_error = None
    app.delivered = []
    app._deliver = lambda text, job: app.delivered.append(text)
    app._target_bundle = lambda target: ""
    app._touch = lambda: None
    app.yield_gpu = lambda: False
    app.model_name = "stub"
    return app

def segment(app, session, index):
    job = TranscriptionJob(job_id=index + 1, audio_bytes=b"x", target=None,
                           window_name="w", app_name="a",
                           session=session, segment_index=index)
    session.register(job)
    app.jobs.submit(job)
    return job

# 1. Nothing is delivered until the dictation is complete, and then once.
app = make_app(["első rész", "második rész"])
s = DictationSession(1)
a, b = segment(app, s, 0), segment(app, s, 1)
app._handle_job(a)
assert app.delivered == [], app.delivered
assert app.ui.partials == ["első rész"], app.ui.partials
assert app.jobs.history_count() == 0, "wrote history for a segment"
s.set_final(2, 42.0)
app._handle_job(b)
assert app.delivered == ["első rész második rész"], app.delivered
print("delivers once, when the dictation is complete")

# 2. Exactly one history entry, carrying the dictation's own duration.
assert app.jobs.history_count() == 1, app.jobs.history_count()
entry = app.jobs.history()[0]
assert entry["text"] == "első rész második rész", entry
assert entry["secs"] == 42.0, entry
print("one history entry, with the whole dictation's duration")

# 3. A cancelled dictation delivers nothing, even mid-flight.
app = make_app(["megszakítva", "sosem"])
s = DictationSession(2)
a, b = segment(app, s, 0), segment(app, s, 1)
s.set_final(2, 10.0)
app._cancel_session(s)
app._handle_job(a)
app._handle_job(b)
assert app.delivered == [] and app.jobs.history_count() == 0
print("cancelled dictation delivers nothing")

# 4. A standalone job — spool recovery, benchmark — behaves as it always did.
app = make_app(["egyben"])
lone = TranscriptionJob(job_id=9, audio_bytes=b"x", target=None,
                        window_name="w", app_name="a", audio_duration=3.0)
app.jobs.submit(lone)
app._handle_job(lone)
assert app.delivered == ["egyben"] and app.jobs.history_count() == 1
print("non-session job unchanged")

Path(__file__).with_name("history-probe.json").unlink(missing_ok=True)
print("\nALL STREAMING PIPELINE TESTS PASSED")
```

- [x] **Step 8: Run the probe**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_streaming_pipeline.py
```

Expected: four `print` lines, then `ALL STREAMING PIPELINE TESTS PASSED`. If `_handle_job` reaches an attribute the stub does not carry, add it to `make_app` rather than weakening the assertion — a missing attribute means a real dependency the design did not account for, and it is worth naming.

- [x] **Step 9: Re-run the earlier probes**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_segment_cutter.py && PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_dictation_session.py && PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/test_spool.py
```

Expected: all three end in their `ALL … PASSED` line. `test_spool.py` is included because the spool path moved inside `_record_and_enqueue`.

---

### Task 5: Show the text as it arrives on the overlay

**Files:**
- Modify: `whispertype/ui/tk_ui.py` (add `set_partial` near `set_ticker`, line 907)
- Modify: `whispertype/ui/appkit_ui.py` (add `set_partial` near `set_ticker`, line 507)

**Interfaces:**
- Consumes: `self.ui.set_partial(text, done, total)` as called from `_handle_job` in Task 4.
- Produces: nothing later tasks depend on.

- [x] **Step 1: Add `set_partial` to the Tk overlay**

In `whispertype/ui/tk_ui.py`, beside `set_ticker`:

```python
    def set_partial(self, text, done, total):
        """The dictation so far, while the rest of it is still being decoded.

        Not `set_ticker`: that one runs a finished transcript across the
        overlay just before it is typed, and its animation would restart on
        every segment. This is a status line — the last words that landed,
        and how many segments are in.
        """
        self._partial = (text, done, total)
        self._draw_stage()
```

Initialise `self._partial = None` in `__init__` beside the other stage state, clear it in `on_recording_stopped` and in `check_hide`, and render it in `_draw_stage`: when `self._partial` is set, draw the tail of the text on one line under the waveform, with a `f"{done}/{total or '?'}"` counter at the right.

The waveform stays. It is the only feedback showing that the microphone is live, and trading it for text would be the wrong swap.

- [x] **Step 2: Add `set_partial` to the AppKit overlay**

In `whispertype/ui/appkit_ui.py`, beside `set_ticker`:

```python
    def set_partial(self, text, done, total):
        """Segment progress. The macOS overlay has no line to put it on yet, so
        this is deliberately a no-op rather than an AttributeError — the
        streaming path is shared, only its display is not."""
```

- [ ] **Step 3: Verify by running the app**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" main.py
```

Dictate for at least a minute with a clear pause in the middle. Expected: the waveform keeps moving, a line of text appears beneath it after the first pause, and the overlay does **not** grow a queue table. Stop with the key; the full text is typed once.

---

### Task 6: Expose the feature in the settings window

**Files:**
- Modify: `whispertype/ui/tk_ui.py` — the Transcription section of `_open_settings` (line 2066), after the Language row at grid row 4

**Interfaces:**
- Consumes: `cfg.stream_transcription`, `cfg.segment_min_seconds` (Task 3); `_settings_slider` and the `row`/`section` helpers already in the file.
- Produces: nothing later tasks depend on.

- [x] **Step 1: Add the two rows**

In `whispertype/ui/tk_ui.py`, after `row(sec, 4, "Language", lang_box)`:

```python
        stream_var = tk.BooleanVar(value=app.cfg.stream_transcription)
        stream_check = self._themed(
            tk.Checkbutton(sec, text="Transcribe while I speak",
                           variable=stream_var, highlightthickness=0, bd=0,
                           font=("Segoe UI", 9),
                           command=lambda: self._pick_streaming(stream_var.get())),
            bg="bg", fg="text", selectcolor="bar_bg", activebackground="bg",
            activeforeground="text")
        row(sec, 6, "Streaming", stream_check,
            "Decoding starts at the first pause instead of waiting for the "
            "whole recording, so a long dictation is nearly finished by the "
            "time you let go.")

        # 30 s is the floor, not 0: below one full Whisper window a segment is
        # mostly the decoder's own silence padding, which is where it invents
        # stock phrases. A control that lets you make the transcript worse is
        # not a preference.
        seg_box, seg_slider = self._settings_slider(
            sec, lo=30, hi=90, step=5, value=app.cfg.segment_min_seconds,
            unit="s",
            on_change=lambda v: app.set_config("segment_min_seconds", v))
        row(sec, 8, "Segment length", seg_box,
            "The least audio a piece may hold before it is cut off at a "
            "pause. Longer pieces mean fewer, larger decodes.")
        self._set["seg_slider"] = seg_slider
        self._sync_streaming(app.cfg.stream_transcription)
```

- [x] **Step 2: Add the two helpers**

Beside `_pick_gpu_graph` in `whispertype/ui/tk_ui.py`:

```python
    def _pick_streaming(self, on):
        self.app.set_config("stream_transcription", bool(on))
        self._sync_streaming(on)

    def _sync_streaming(self, on):
        """Grey the segment slider out when streaming is off — it configures a
        path that is not running, the same way the GPU checkbox disables itself
        on a machine with no GPU."""
        slider = self._set.get("seg_slider")
        if slider is not None:
            slider.configure(state="normal" if on else "disabled")
```

- [ ] **Step 3: Verify the window**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" main.py
```

Right-click the tray icon, open Settings, and check the Transcription section: the checkbox reflects the config, unticking it greys the slider, and both values survive closing and reopening the window. Confirm `~/.whispertype/config.json` gained `stream_transcription` and `segment_min_seconds`.

---

### Task 7: Prove the transcript did not change

The measurement the whole design rests on. Everything above is worthless if this one fails.

**Files:**
- Test: `.tmp/measure_segmented_vs_whole.py`

**Interfaces:**
- Consumes: `audio.SegmentCutter` (Task 1), `transcribe.create_engine`, `config.Config`.
- Produces: a verdict, not code.

- [ ] **Step 1: Capture a reference dictation**

Run the app and dictate three to five minutes of ordinary Hungarian, with the pauses you normally take. Then take the WAV the spool wrote before it was discarded — or, more simply, set `stream_transcription: false`, dictate, and copy the WAV out of `~/.whispertype/spool/` before the transcript lands in history. Put it at `.tmp/reference.wav`.

- [x] **Step 2: Write the comparison script**

Create `.tmp/measure_segmented_vs_whole.py`:

```python
import difflib, sys, time, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from whispertype import config, transcribe
from whispertype.audio import SegmentCutter

WAV = Path(__file__).with_name("reference.wav")
# The real config, so the measurement runs on the values that will ship.
cfg = config.load()
with wave.open(str(WAV)) as w:
    rate, pcm = w.getframerate(), w.readframes(w.getnframes())
print(f"{WAV.name}: {len(pcm) / (rate * 2):.1f}s at {rate} Hz")

engine = transcribe.create_engine(cfg)
engine.load(cfg.get("last_model", "large-v3-turbo"))

t0 = time.perf_counter()
whole = engine.transcribe(pcm, cfg.language)
whole_secs = time.perf_counter() - t0
print(f"\nwhole clip: {whole_secs:.1f}s")

# Re-run the same cut decisions the recorder would have made.
chunk = cfg.chunk
cutter = SegmentCutter(rate, chunk, threshold=cfg.silence_threshold,
                       min_seconds=cfg.segment_min_seconds,
                       max_seconds=cfg.segment_max_seconds,
                       cut_silence=cfg.segment_cut_silence)
segments = []
for i in range(0, len(pcm) - chunk * 2 + 1, chunk * 2):
    data = pcm[i:i + chunk * 2]
    arr = np.frombuffer(data, np.int16).astype(np.float64)
    rms = float(np.sqrt(np.mean(arr ** 2))) if arr.size else 0.0
    seg = cutter.feed(data, rms)
    if seg is not None:
        segments.append(seg)
tail = cutter.flush()
if tail:
    segments.append(tail)
print(f"cut into {len(segments)} segments: "
      f"{[round(len(s) / (rate * 2), 1) for s in segments]}")

t0 = time.perf_counter()
parts = [engine.transcribe(s, cfg.language) for s in segments]
seg_secs = time.perf_counter() - t0
joined = " ".join(p for p in parts if p).strip()
print(f"segmented: {seg_secs:.1f}s")

a, b = whole.split(), joined.split()
sm = difflib.SequenceMatcher(None, a, b)
print(f"\nwords: {len(a)} whole, {len(b)} segmented")
print(f"word-level similarity: {sm.ratio():.4f}")
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag != "equal":
        print(f"  {tag:7} whole[{i1}:{i2}]={' '.join(a[i1:i2])!r} -> "
              f"segmented[{j1}:{j2}]={' '.join(b[j1:j2])!r}")

Path(__file__).with_name("whole.txt").write_text(whole, encoding="utf-8")
Path(__file__).with_name("segmented.txt").write_text(joined, encoding="utf-8")
print("\nfull texts written next to this script")
```

- [ ] **Step 3: Run it**

```bash
PYTHONIOENCODING=utf-8 "/c/Users/Foltin Csaba/AppData/Local/Programs/Python/Python312/python.exe" .tmp/measure_segmented_vs_whole.py
```

- [ ] **Step 4: Read the verdict honestly**

The design claims the transcript does not change. What counts as confirmation:

- **Passes:** differences are punctuation or capitalisation at segment boundaries only, and word-level similarity is ≥ 0.98. Report the number.
- **Fails:** words are dropped, invented, or reordered — especially a `replace` opcode at a boundary, which means a cut landed inside a word, or an `insert` of a stock phrase, which means a segment was mostly padding.

On a failure, do not adjust the assertion. Raise `segment_min_seconds` (45, then 60) and `segment_cut_silence` (0.8, then 1.2) and re-run, then change the defaults in `config.py` to whatever passed and say so. If nothing passes, the premise is wrong and the feature should not ship — say that too.

- [ ] **Step 5: Manual end-to-end**

Run the app and dictate for three to five minutes into a text editor. Check all of:

- the text is typed **once**, whole, when you release the key;
- the wait after releasing is a few seconds, not the length of the clip;
- `~/.whispertype/history.json` gained exactly **one** entry, with the full text and the full duration;
- `~/.whispertype/spool/` is empty afterwards;
- the overlay never grew a queue table;
- `voice_daemon.log` shows the segments being enqueued *while* the recording was still running — that is the whole feature, and the timestamps are the proof.

Then repeat with `stream_transcription: false` and confirm the log shows one job and the old timing.

---

## Self-review notes

Checked against the spec:

- Every spec section maps to a task: `SegmentCutter` → 1, `DictationSession` and queue visibility → 2, capture/flow and configuration → 3, error handling and delivery → 4, overlay → 5, settings window → 6, testing → 1-4 and 7.
- The spec's rule that "a session is never merely abandoned" is implemented in Task 4 Steps 4 and 6, where the failure branch and each of the four early returns calls `_cancel_session`, and asserted in Task 4 Step 7 test 3.
- Names are consistent across tasks: `feed`/`flush` (Task 1) are called in Tasks 3 and 7; `register`/`set_final`/`progress`/`text` (Task 2) are called in Task 4; `set_partial(text, done, total)` (Task 4) is defined in Task 5 with the same signature in both front-ends.
- Two spec items are deliberately not code tasks: crash-durability behaviour is unchanged by construction (the spool still stores the whole clip, Task 4 Step 6), and the decoder-crash retry already exists in `engine_proc.py` and is inherited.
