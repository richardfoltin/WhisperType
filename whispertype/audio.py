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
        return None  # Windows: always the system default, as before


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


def backend():
    global _backend
    if _backend is None:
        _backend = _SoundDeviceBackend() if IS_MAC else _PyAudioBackend()
    return _backend


def record_until_stop(cfg, stop_event, level_callback=None):
    """Record until stopped, silent for cfg.silence_duration, or timed out.

    Returns raw 16-bit mono PCM bytes, or None if nothing was captured.
    """
    dev = backend().resolve_device(cfg.input_device)
    stream = backend().open(cfg.rate, cfg.chunk, device=dev)
    frames = []
    silence_since = None
    start = time.time()
    threshold = cfg.silence_threshold
    max_secs = cfg.max_recording_time
    silence_secs = cfg.silence_duration
    try:
        while not stop_event.is_set():
            data = stream.read()
            frames.append(data)
            arr = np.frombuffer(data, np.int16).astype(np.float64)
            rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0
            if level_callback:
                level_callback(rms)
            if rms < threshold:
                if silence_since is None:
                    silence_since = time.time()
                elif time.time() - silence_since > silence_secs:
                    break
            else:
                silence_since = None
            if time.time() - start > max_secs:
                break
    finally:
        try:
            stream.close()
        except Exception as e:
            log(f"Error closing audio stream: {e}")
    return b"".join(frames) if frames else None
