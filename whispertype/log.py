"""Logging — mirrors the original single-file behaviour.

The daemon runs without a console on both platforms, so stdout/stderr are
redirected into a log file next to the package. The file is truncated on every
launch, exactly like the original whispertype.pyw did.
"""
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "voice_daemon.log"

_log_f = None


def init(redirect_std=True):
    """Open the log file and (optionally) point stdout/stderr at it."""
    global _log_f
    if _log_f is not None:
        return
    _log_f = open(LOG_PATH, "w", buffering=1, encoding="utf-8")
    if redirect_std:
        sys.stdout = _log_f
        sys.stderr = _log_f


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def close():
    global _log_f
    if _log_f is not None:
        _log_f.close()
        _log_f = None
