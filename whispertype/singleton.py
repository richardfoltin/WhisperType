"""Single-instance guard.

Two daemons both grab the push-to-talk key, so a double-tap starts two
recordings, two overlays fight over the same screen position and both write to
the same log. The second copy has to refuse to start.

Claimed before anything else in main(), including before the log is opened, so
a duplicate launch cannot disturb the instance that is actually running.
"""
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

_MUTEX_NAME = "WhisperType.SingleInstance"
_LOCK_PATH = Path.home() / ".whispertype" / "whispertype.lock"

#: Kept at module scope for the process lifetime — releasing the handle or
#: closing the file descriptor would release the lock.
_handle = None


def _claim_windows():
    global _handle
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _claim_posix():
    global _handle
    import fcntl

    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _handle = open(_LOCK_PATH, "w")
        fcntl.flock(_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def claim():
    """True if we are the only instance; False if one is already running."""
    try:
        return _claim_windows() if IS_WINDOWS else _claim_posix()
    except Exception:
        # A guard that cannot be established must not stop the app from running.
        return True


def notify_already_running():
    """Tell the user why nothing happened — there is no console to print to."""
    message = "WhisperType is already running — check the tray / menu bar."
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "WhisperType", 0x40)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)
