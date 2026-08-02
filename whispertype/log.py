"""Logging.

The daemon runs without a console on both platforms, so stdout/stderr are
redirected into a log file. Unlike the original single-file version this does
NOT truncate on every launch — a daemon that restarts at login would otherwise
destroy the evidence of whatever made it restart. The file is rolled over to
`.1` once it passes MAX_BYTES, keeping at most two generations.
"""
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "voice_daemon.log"
MAX_BYTES = 1_000_000

_log_f = None


def _rotate():
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            LOG_PATH.with_suffix(".log.1").unlink(missing_ok=True)
            LOG_PATH.rename(LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass  # a log we cannot rotate is not worth failing startup over


def init(redirect_std=True):
    """Open the log file and (optionally) point stdout/stderr at it."""
    global _log_f
    if _log_f is not None:
        return
    _rotate()
    _log_f = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    if redirect_std:
        sys.stdout = _log_f
        sys.stderr = _log_f
    print(f"\n{'=' * 60}", flush=True)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def close():
    global _log_f
    if _log_f is not None:
        _log_f.close()
        _log_f = None
