"""Configuration loading.

Same file location as the Windows original (~/.whispertype/config.json), but it
is now created from the built-in defaults when missing instead of crashing —
the Windows installer used to be the only thing that ever created it.
"""
import json
import sys
from pathlib import Path

from .log import log

CONFIG_DIR = Path.home() / ".whispertype"
CONFIG_PATH = CONFIG_DIR / "config.json"

IS_MAC = sys.platform == "darwin"

# Right Ctrl does not exist on Apple keyboards, so macOS defaults to Right
# Command. Everything else is identical across platforms.
DEFAULTS = {
    "push_to_talk_key": "cmd_r" if IS_MAC else "ctrl_r",
    "language": "en",
    "sample_rate": 16000,
    "channels": 1,
    "chunk_size": 1024,
    "silence_threshold": 200,
    "silence_duration": 3.0,
    "max_recording_time": 300.0,
    "last_model": "large-v3-turbo",
    #: Capture device: null = system default, an int index, or a name substring.
    "input_device": None,
    #: Release the model after this many idle minutes (0 disables). Reloading
    #: from the local cache measures about a second.
    "idle_unload_minutes": 10,
    #: A clip whose level never stayed above silence_threshold for this long is
    #: discarded rather than transcribed — otherwise Whisper invents a sentence
    #: for silence and types it wherever the user is working.
    "min_speech_seconds": 0.25,
    #: "auto" follows the system appearance; "dark" / "light" pin it.
    "theme": "auto",
}


class Config:
    def __init__(self, data, path=CONFIG_PATH):
        self._data = data
        self._path = path

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            log(f"Failed to save config: {e}")

    # ── Typed accessors used across the app ──

    @property
    def language(self):
        return self.get("language", DEFAULTS["language"])

    @property
    def rate(self):
        return int(self.get("sample_rate", DEFAULTS["sample_rate"]))

    @property
    def chunk(self):
        return int(self.get("chunk_size", DEFAULTS["chunk_size"]))

    @property
    def silence_threshold(self):
        return float(self.get("silence_threshold", DEFAULTS["silence_threshold"]))

    @property
    def silence_duration(self):
        return float(self.get("silence_duration", DEFAULTS["silence_duration"]))

    @property
    def max_recording_time(self):
        return float(self.get("max_recording_time", DEFAULTS["max_recording_time"]))

    @property
    def ptt_key_name(self):
        return self.get("push_to_talk_key", DEFAULTS["push_to_talk_key"])

    @property
    def input_device(self):
        """Optional substring/index selecting the capture device (macOS: pin to
        the built-in mic so opening the stream does not drag Bluetooth
        headphones into their low-quality HFP profile)."""
        return self.get("input_device", None)

    @property
    def idle_unload_seconds(self):
        try:
            minutes = float(self.get("idle_unload_minutes",
                                     DEFAULTS["idle_unload_minutes"]))
        except (TypeError, ValueError):
            minutes = DEFAULTS["idle_unload_minutes"]
        return max(0.0, minutes) * 60.0

    @property
    def min_speech_seconds(self):
        try:
            return max(0.0, float(self.get("min_speech_seconds",
                                           DEFAULTS["min_speech_seconds"])))
        except (TypeError, ValueError):
            return DEFAULTS["min_speech_seconds"]

    @property
    def theme(self):
        value = str(self.get("theme", "auto")).lower()
        return value if value in ("auto", "dark", "light") else "auto"


def load():
    """Load config, creating it from DEFAULTS on first run."""
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
        log(f"Created default config at {CONFIG_PATH}")
        return Config(dict(DEFAULTS))

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Config unreadable ({e}) — falling back to defaults")
        return Config(dict(DEFAULTS))

    # Fill in keys added after the user's config was written
    merged = dict(DEFAULTS)
    merged.update(data)
    return Config(merged)
