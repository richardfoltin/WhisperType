"""Microphone capture.

Windows keeps PyAudio (unchanged behaviour); macOS uses sounddevice, whose
wheel ships its own libportaudio.dylib so no Homebrew/PortAudio build is
needed. Both produce identical output: raw 16-bit mono PCM at cfg.rate.
"""
import locale
import sys
import threading
import time

import numpy as np

from .log import log

IS_MAC = sys.platform == "darwin"


class _PyAudioBackend:
    def __init__(self):
        import pyaudio
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        try:
            log(f"PyAudio initialized: {self._pa.get_default_input_device_info()['name']}")
        except Exception as e:
            log(f"PyAudio: no default input device ({e})")

    def open(self, rate, chunk, device=None):
        stream = self._pa.open(format=self._pyaudio.paInt16, channels=1,
                               rate=rate, input=True, frames_per_buffer=chunk,
                               input_device_index=device)
        return _PyAudioStream(stream, chunk)

    def resolve_device(self, spec):
        """spec may be None (system default), an int index, or a name substring.

        Windows machines routinely expose the same physical microphone several
        times over different host APIs, and the system default is not always the
        one that carries signal — so "just use the default" needs an override.
        """
        if spec is None:
            return None
        if isinstance(spec, int):
            return spec
        needle = str(spec).lower()
        for idx in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(idx)
            except Exception:
                continue
            if info.get("maxInputChannels", 0) > 0 and needle in str(info.get("name", "")).lower():
                return idx
        log(f"Input device matching {spec!r} not found — using system default")
        return None


class _PyAudioStream:
    def __init__(self, stream, chunk):
        self._s = stream
        self._chunk = chunk

    def read(self):
        return self._s.read(self._chunk, exception_on_overflow=False)

    def close(self):
        self._s.stop_stream()
        self._s.close()


class _SoundDeviceBackend:
    def __init__(self):
        import sounddevice as sd
        self._sd = sd
        try:
            default_in = sd.query_devices(kind="input")
            log(f"sounddevice initialized: {default_in['name']} "
                f"({default_in['default_samplerate']:.0f} Hz native)")
        except Exception as e:
            log(f"sounddevice: no default input device ({e})")

    def resolve_device(self, spec):
        """spec may be None (system default), an int index, or a name substring."""
        if spec is None:
            return None
        if isinstance(spec, int):
            return spec
        needle = str(spec).lower()
        for idx, dev in enumerate(self._sd.query_devices()):
            if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
                log(f"Using input device #{idx}: {dev['name']}")
                return idx
        log(f"Input device matching {spec!r} not found — using system default")
        return None

    def open(self, rate, chunk, device=None):
        stream = self._sd.RawInputStream(
            samplerate=rate, blocksize=chunk, device=device,
            channels=1, dtype="int16")
        stream.start()  # sounddevice streams do not auto-start
        return _SoundDeviceStream(stream, chunk)


class _SoundDeviceStream:
    def __init__(self, stream, chunk):
        self._s = stream
        self._chunk = chunk

    def read(self):
        # RawInputStream.read returns (cffi_buffer, overflowed)
        buf, _overflowed = self._s.read(self._chunk)
        return bytes(buf)

    def close(self):
        self._s.stop()
        self._s.close()


_backend = None
_device_cache = {}
_device_list = None

#: PortAudio is not thread-safe, and this module is reached from three threads:
#: the recorder, the tray (which lists devices for its Microphone submenu) and
#: the Tk thread (the settings window). Two of them constructing the backend at
#: once initialised PortAudio twice and took the whole process down natively —
#: no traceback, no log line, just a daemon that was there a second ago.
#: Everything that calls into PortAudio holds this; only stream.read() runs
#: outside it, which is what keeps a recording in progress from blocking the
#: menu.
_pa_lock = threading.RLock()


def backend():
    with _pa_lock:
        global _backend
        if _backend is None:
            _backend = _SoundDeviceBackend() if IS_MAC else _PyAudioBackend()
        return _backend


def reset_device_cache():
    """Called when the configured device changes, so the next recording opens
    the new one rather than the cached lookup."""
    global _device_list
    with _pa_lock:
        _device_cache.clear()
        _device_list = None


def resolve_device(spec):
    """Cached device lookup — record_until_stop runs this on every recording."""
    with _pa_lock:
        key = repr(spec)
        if key not in _device_cache:
            idx = backend().resolve_device(spec)
            _device_cache[key] = idx
            if idx is not None:
                log(f"Capture device resolved: {spec!r} -> index {idx}")
        return _device_cache[key]


def _clean_name(name):
    """Repair a capture device's name for display.

    PortAudio writes UTF-8 into the MME device names, and PyAudio decodes them
    with the process's ANSI code page — so on a Hungarian Windows "hangleképző"
    arrives as "hanglekĂ©pzĹ‘". Re-encoding with that code page and decoding as
    UTF-8 undoes it, and the round trip is self-checking: a name that was never
    mangled cannot survive it, so it raises and the original is kept.

    Some Bluetooth device names also carry an embedded newline, which turns one
    menu item into three.
    """
    name = str(name)
    try:
        repaired = name.encode(locale.getpreferredencoding(False)).decode("utf-8")
    except (UnicodeError, LookupError):
        repaired = name
    return " ".join(repaired.split())


def open_stream(cfg, device):
    """The one place a capture stream is opened, so the open is serialised
    against every other PortAudio call."""
    with _pa_lock:
        return backend().open(cfg.rate, cfg.chunk, device=device)


def list_input_devices():
    """[(index, name)] for every usable capture device, for the settings UI.

    Windows routinely exposes the same physical microphone several times over
    different host APIs, so the index matters as much as the name.

    Cached: the tray menu is rebuilt on every state change, and re-enumerating
    the sound hardware each time is both slow and one more chance to be inside
    PortAudio when something else needs to be.
    """
    global _device_list
    with _pa_lock:
        if _device_list is not None:
            return list(_device_list)
        out = []
        try:
            b = backend()
            if IS_MAC:
                for idx, dev in enumerate(b._sd.query_devices()):
                    if dev["max_input_channels"] > 0:
                        out.append((idx, _clean_name(dev["name"])))
            else:
                for idx in range(b._pa.get_device_count()):
                    info = b._pa.get_device_info_by_index(idx)
                    if info.get("maxInputChannels", 0) > 0:
                        out.append((idx, _clean_name(
                            info.get("name", f"device {idx}"))))
        except Exception as e:
            log(f"Could not list input devices: {e}")
            return out          # not cached: a failed probe should be retried
        _device_list = out
        return list(out)


#: Whisper decodes in windows of this many seconds, padding the last one out
#: with silence to fill it.
WINDOW_SECONDS = 30.0

#: A last window holding less than this much real audio is mostly padding, and
#: padding is what Whisper answers with an invented stock phrase. Measured on
#: three real dictations: every segment that ended 0.1-1.3 s past a window
#: boundary came back with a "Köszönöm" or a garbled half-word on the end,
#: while none that ended more than 5 s past one did.
MIN_WINDOW_TAIL = 5.0


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

    On top of those three, no cut may leave a SLIVER. Whisper pads its last
    window out to 30 seconds, so a segment of 30.7 s is decoded as one full
    window plus a second one holding 0.7 s of speech and 29.3 s of silence —
    and it answers that second call with a stock phrase it made up. The floor
    stops a whole segment being mostly padding; this stops a segment's tail
    being mostly padding, which is the same failure one level down.
    """

    def __init__(self, rate, chunk, *, threshold, min_seconds, max_seconds,
                 cut_silence, window_seconds=WINDOW_SECONDS,
                 min_window_tail=MIN_WINDOW_TAIL):
        self._chunk_seconds = chunk / float(rate)
        self._threshold = threshold
        # Taken as arguments rather than read off the module so a probe can
        # scale the whole rule down to numbers a person can check by hand.
        self._window = window_seconds
        self._min_tail = min_window_tail
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
            if self._sliver(n):
                # Wait rather than cut. Almost always this is a few hundred
                # milliseconds further into the same pause, which is still a
                # genuine pause; a short pause is skipped for the next one.
                return None
            return self._close(n)
        if n >= self._max_chunks:
            return self._close(self._safe_ceiling())
        return None

    def _sliver(self, chunks):
        """Would cutting here leave a last decode window that is mostly
        padding?"""
        tail = (chunks * self._chunk_seconds) % self._window
        return 0 < tail < self._min_tail

    def _safe_ceiling(self):
        """Where to cut when the ceiling is reached and no pause was usable.

        Normally the quietest chunk seen. When that would leave a sliver there
        is no quieter option left to look for, so the cut falls back to a whole
        number of decode windows — an arbitrary offset, but one the decoder
        handles as a full window rather than as padding.
        """
        upto = self._best_idx
        if not self._sliver(upto):
            return upto
        windows = int(upto * self._chunk_seconds // self._window)
        # Rounded, not truncated: a whole number of windows divided by a chunk
        # length that is not exact in binary lands a hair under the boundary,
        # and truncating there would move the cut a chunk earlier every time.
        boundary = round(windows * self._window / self._chunk_seconds)
        return max(self._min_chunks, min(upto, boundary))

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

        The sliver rule does not apply here and cannot: capture has ended, so
        there is no "wait for the next pause" left to take, and splitting the
        tail would only move the sliver rather than remove it. A tail that
        happens to land just past a window boundary is therefore still exposed
        to the stock phrase — one window per dictation at worst, against one
        per segment before the rule existed.
        """
        if not self._frames:
            return None
        if self._cut_made and not self._speech:
            self._reset()
            return None
        segment = b"".join(self._frames)
        self._reset()
        return segment


class Capture:
    """Result of one recording."""

    __slots__ = ("data", "speech_seconds", "duration", "stop_reason", "peak_rms")

    def __init__(self, data, speech_seconds, duration=0.0,
                 stop_reason="stopped", peak_rms=0.0):
        self.data = data
        self.speech_seconds = speech_seconds
        self.duration = duration
        #: "stopped" (key/UI), "silence", or "limit" — logged so a recording
        #: that ended on its own is never a mystery.
        self.stop_reason = stop_reason
        self.peak_rms = peak_rms


def warm_up(cfg):
    """Open and immediately close a capture stream.

    Opening a CoreAudio input stream is not free — the device has to be
    configured and, at 16 kHz on a 48 kHz mic, a sample-rate converter set up.
    Doing that lazily inside the recorder thread meant the overlay said
    "Recording" seconds before any audio was actually flowing, so the first
    sentence of each session was clipped. Paying the cost at startup also moves
    the microphone permission prompt to launch time instead of mid-dictation.
    """
    t0 = time.time()
    dev = resolve_device(cfg.input_device)
    stream = open_stream(cfg, dev)
    # Sample the idle noise floor while we are here. It is the number you need
    # to pick silence_threshold sensibly, and a mic whose floor sits above the
    # threshold can never auto-stop while one that returns digital zero gives
    # pauses no margin at all.
    floor = 0.0
    try:
        deadline = time.time() + 0.3
        while time.time() < deadline:
            arr = np.frombuffer(stream.read(), np.int16).astype(np.float64)
            if arr.size:
                floor = max(floor, float(np.sqrt(np.mean(arr ** 2))))
    except Exception as e:
        log(f"Idle level probe failed: {e}")
    finally:
        with _pa_lock:
            stream.close()
    log(f"Audio device warm ({time.time() - t0:.2f}s) | "
        f"idle noise floor RMS {floor:.1f}, silence_threshold {cfg.silence_threshold:.0f}")
    return floor


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
                # silence_duration <= 0 disables the auto-stop entirely, for
                # people who always end a dictation with the key.
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

    duration = time.time() - start
    log(f"Capture: {duration:.1f}s, speech {speech_seconds:.1f}s, "
        f"peak RMS {peak_rms:.0f} (threshold {threshold:.0f}), ended by {reason}")
    if reason == "silence":
        # The single most confusing failure this app has: a recording that ends
        # itself mid-sentence looks like a crash. Name the cause every time.
        if peak_rms < threshold:
            log(f"  Nothing ever exceeded the threshold. Either the microphone "
                f"is not the one you are speaking into (set \"input_device\"), "
                f"or silence_threshold ({threshold:.0f}) is above your voice.")
        else:
            log(f"  Auto-stopped after {silence_secs:.1f}s below the threshold. "
                f"Raise \"silence_duration\" if you pause longer than that while "
                f"thinking, or set it to 0 and always stop with the key.")
    return Capture(b"".join(frames) if frames else None, speech_seconds,
                   duration=duration, stop_reason=reason, peak_rms=peak_rms)
