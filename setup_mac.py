"""py2app bundle definition for the macOS build.

Built in ALIAS mode (`python setup_mac.py py2app -A`): the produced
WhisperType.app is a few hundred kilobytes and references this source tree and
the venv instead of copying the ~2 GB MLX/PyObjC stack into itself.

The point of the bundle is not distribution — it is identity. Launched as a
bare `venv/bin/python3 main.py`, macOS attaches the Accessibility and Input
Monitoring grants to the interpreter, which means they appear in System
Settings as an anonymous "python3" and silently break whenever the venv is
rebuilt. py2app's launcher is a real Mach-O that loads libpython in-process, so
the grants attach to WhisperType itself.
"""
import sys
from pathlib import Path

from setuptools import setup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from whispertype import __version__  # noqa: E402  single source of truth

APP = ["main.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "WhisperType",
        "CFBundleDisplayName": "WhisperType",
        "CFBundleIdentifier": "com.whispertype.app",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "13.0",
        # Menu-bar only: no Dock icon, no app menu, never becomes frontmost.
        "LSUIElement": True,
        "NSMicrophoneUsageDescription":
            "WhisperType records your voice so it can be transcribed locally "
            "on this Mac.",
    },
}

setup(
    name="WhisperType",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
