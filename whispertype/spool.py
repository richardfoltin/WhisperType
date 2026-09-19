"""Crash-durable audio spool.

A recording exists only in RAM between the moment capture ends and the moment
its transcript reaches history. On 2026-08-18 a fast-fail inside nvcuda64.dll
(0xc0000409, stack-cookie check) killed the process inside exactly that window
and took 141 seconds of dictation with it. Nothing at the Python level can
defend against a native fast-fail: no exception is raised, no `except` sees it,
no `finally` runs, no atexit handler fires. The only thing that survives is
what is already on disk.

So a clip goes to disk before it is queued, and is deleted once its transcript
is in history. Whatever is still here at startup is, by definition, audio the
previous run never finished with.

The spool sits next to history.json under the home directory — the same SSD,
so writing a 4.5 MB clip costs about a frame and never waits on a platter.
"""
import json
import os
import time
import wave
from pathlib import Path

from .log import log

SPOOL_DIR = Path.home() / ".whispertype" / "spool"

#: How many times a recovered clip may be handed to the engine before it is
#: left alone. The crash this spool exists for is a *decode* crash, so the
#: clip that caused it can reproduce it — and a recovery that runs on every
#: launch would turn one lost recording into a daemon that cannot start. After
#: this many tries the WAV stays on disk and the log says where it is.
MAX_ATTEMPTS = 2


class SpooledClip:
    """One spooled recording: the WAV plus whatever metadata survived."""

    def __init__(self, wav, meta):
        self.wav = Path(wav)
        self.meta = meta or {}

    @property
    def attempts(self):
        return int(self.meta.get("attempts", 0))

    @property
    def duration(self):
        return float(self.meta.get("duration", 0.0))

    @property
    def window_name(self):
        return self.meta.get("window_name", "?")

    @property
    def app_name(self):
        return self.meta.get("app_name", "?")

    @property
    def created_at(self):
        return float(self.meta.get("created_at", self.wav.stat().st_mtime))

    def audio_bytes(self):
        """The raw 16-bit PCM back out of the WAV container."""
        with wave.open(str(self.wav), "rb") as w:
            return w.readframes(w.getnframes())

    def note_attempt(self):
        """Record that this clip is about to be decoded — on disk, before the
        decode, so a process death during it still counts."""
        self.meta["attempts"] = self.attempts + 1
        _write_meta(_meta_path(self.wav), self.meta)


def _meta_path(wav):
    return Path(wav).with_suffix(".json")


def _write_meta(path, meta):
    try:
        tmp = path.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log(f"Spool: could not write {path.name} ({e})")


def save(job_id, audio_bytes, rate, meta=None):
    """Write a clip to the spool and return its path, or None.

    Written to a `.part` name and renamed into place, so a half-written file
    can never be mistaken for a recoverable one. The WAV lands before its
    sidecar: audio with unknown metadata is recoverable, metadata without
    audio is not.

    Never raises. A spool that cannot be written is a degraded safety net;
    a dictation that fails because of it is a lost recording, which is the
    exact thing this module exists to prevent.
    """
    if not audio_bytes:
        return None
    try:
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        wav = SPOOL_DIR / f"{stamp}-{job_id:04d}.wav"
        part = wav.with_suffix(".wav.part")

        with wave.open(str(part), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)          # 16-bit, as captured
            w.setframerate(int(rate))
            w.writeframes(audio_bytes)
        # writeframes alone leaves the bytes in the OS cache, which a fast-fail
        # takes down with the process. Force them out before the rename.
        with open(part, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(part, wav)

        full = {"job_id": job_id, "rate": int(rate), "attempts": 0,
                "created_at": time.time(),
                "bytes": len(audio_bytes)}
        full.update(meta or {})
        _write_meta(_meta_path(wav), full)
        return wav
    except Exception as e:
        log(f"Spool: could not save the recording ({e}) — "
            f"this clip is only in memory")
        return None


def discard(wav):
    """Drop a clip whose transcript is safely in history."""
    if not wav:
        return
    for path in (Path(wav), _meta_path(wav)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Spool: could not delete {path.name} ({e})")


def orphans():
    """Every clip the previous run never finished with, oldest first.

    Also sweeps `.part` files: those are recordings interrupted mid-write, and
    there is nothing in them worth keeping.
    """
    if not SPOOL_DIR.is_dir():
        return []
    for part in SPOOL_DIR.glob("*.part"):
        try:
            part.unlink()
        except Exception:
            pass
    clips = []
    for wav in sorted(SPOOL_DIR.glob("*.wav")):
        meta = {}
        try:
            meta_file = _meta_path(wav)
            if meta_file.exists():
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Spool: unreadable metadata for {wav.name} ({e}) — "
                f"recovering the audio anyway")
        clips.append(SpooledClip(wav, meta))
    return clips
