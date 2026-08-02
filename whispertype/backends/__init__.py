"""Platform backends — picked once at import time."""
import sys


def create_backend():
    if sys.platform == "darwin":
        from .macos import MacBackend
        return MacBackend()
    if sys.platform.startswith("win"):
        from .windows import WindowsBackend
        return WindowsBackend()
    raise RuntimeError(f"WhisperType has no backend for {sys.platform!r}")
