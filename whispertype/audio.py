"""Microphone capture.

Windows keeps PyAudio (unchanged behaviour); macOS uses sounddevice, whose
wheel ships its own libportaudio.dylib so no Homebrew/PortAudio build is
needed. Both produce identical output: raw 16-bit mono PCM at cfg.rate.
"""
import sys
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


def backend():
    global _backend
    if _backend is None:
        _backend = _SoundDeviceBackend() if IS_MAC else _PyAudioBackend()
    return _backend


def resolve_device(spec):
    """Cached device lookup — record_until_stop runs this on every recording."""
    key = repr(spec)
    if key not in _device_cache:
        idx = backend().resolve_device(spec)
        _device_cache[key] = idx
        if idx is not None:
            log(f"Capture device resolved: {spec!r} -> index {idx}")
    return _device_cache[key]


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
    stream = backend().open(cfg.rate, cfg.chunk, device=dev)
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
        stream.close()
    log(f"Audio device warm ({time.time() - t0:.2f}s) | "
        f"idle noise floor RMS {floor:.1f}, silence_threshold {cfg.silence_threshold:.0f}")


def record_until_stop(cfg, stop_event, level_callback=None, on_first_chunk=None):
    """Record until stopped, silent for cfg.silence_duration, or timed out.

    `on_first_chunk` fires once, when audio is genuinely flowing, so the UI can
    start its timer from that instant rather than from the keypress.
    """
    dev = resolve_device(cfg.input_device)
    stream = backend().open(cfg.rate, cfg.chunk, device=dev)
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
            stream.close()
        except Exception as e:
            log(f"Error closing audio stream: {e}")

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
