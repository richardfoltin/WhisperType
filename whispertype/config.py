"""Configuration loading.

Same file location as the Windows original (~/.whispertype/config.json), but it
is now created from the built-in defaults when missing instead of crashing —
the Windows installer used to be the only thing that ever created it.
"""
import json
import os
import sys
from pathlib import Path

from .log import log

CONFIG_DIR = Path.home() / ".whispertype"
CONFIG_PATH = CONFIG_DIR / "config.json"

#: Preferred home for the OpenAI key: a file of its own, so the key never has
#: to sit in a config people paste into issues, and never near the repo.
API_KEY_PATH = CONFIG_DIR / "openai_api_key"

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
    #: The GPU sparkline is developer telemetry sitting above "am I
    #: recording?". Off by default; the utilisation still shows as a chip
    #: while a transcription is running.
    "show_gpu_graph": False,
    #: "local" runs Whisper on this machine; "openai" sends the audio to
    #: OpenAI's hosted transcription API instead.
    "stt_engine": "local",
    "openai_model": "gpt-4o-transcribe",
    "openai_endpoint": "https://api.openai.com/v1",
    #: Leave null and put the key in ~/.whispertype/openai_api_key (or the
    #: OPENAI_API_KEY environment variable) instead — see the api_key property.
    "openai_api_key": None,
}


class Config:
    def __init__(self, data, path=None, writable=True):
        self._data = data
        # Resolved at call time, not bound as a default argument: a default
        # would capture CONFIG_PATH at import and keep writing to the real
        # config even when a caller (or a test) points the module elsewhere.
        self._path = Path(path) if path else CONFIG_PATH
        #: False when the file on disk exists but could not be parsed. The
        #: in-memory config is then defaults, not the user's settings, and
        #: writing it back would finish destroying a file that one stray comma
        #: made unreadable.
        self._writable = writable

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        if not self._writable:
            log(f"Not saving config: {self._path} could not be read, and these "
                f"are defaults — overwriting it would discard your settings.")
            return
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            # Atomic: a crash mid-write leaves the previous file intact rather
            # than a truncated one that fails to parse on the next launch.
            os.replace(tmp, self._path)
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

    # ── OpenAI API mode ──

    @property
    def stt_engine(self):
        value = str(self.get("stt_engine", "local")).lower()
        return value if value in ("local", "openai") else "local"

    @property
    def openai_model(self):
        return self.get("openai_model", DEFAULTS["openai_model"])

    @property
    def openai_endpoint(self):
        return str(self.get("openai_endpoint",
                            DEFAULTS["openai_endpoint"])).rstrip("/")

    @property
    def openai_api_key(self):
        """Resolved from, in order: the environment, a key file, the config.

        The file and the environment come first so the key never has to live in
        config.json — that file gets pasted into bug reports, and this one does
        not. Nothing here is ever logged.
        """
        env = os.environ.get("OPENAI_API_KEY", "").strip()
        if env:
            return env
        try:
            if API_KEY_PATH.exists():
                key = API_KEY_PATH.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except OSError as e:
            log(f"Could not read {API_KEY_PATH}: {e}")
        key = self.get("openai_api_key") or ""
        return key.strip() or None

    @property
    def api_key_source(self):
        """Where the key came from — for the UI, without revealing the key."""
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "environment"
        try:
            if API_KEY_PATH.exists() and API_KEY_PATH.read_text(
                    encoding="utf-8").strip():
                return "key file"
        except OSError:
            pass
        if (self.get("openai_api_key") or "").strip():
            return "config.json"
        return None

    def write_api_key(self, key):
        """Store the key in its own file, never in config.json."""
        try:
            API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = API_KEY_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(key.strip())
            os.replace(tmp, API_KEY_PATH)
            try:
                os.chmod(API_KEY_PATH, 0o600)   # no-op on Windows, right on macOS
            except OSError:
                pass
            log(f"OpenAI API key written to {API_KEY_PATH}")
            return True
        except OSError as e:
            log(f"Could not write the API key: {e}")
            return False

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
        log(f"Config unreadable ({e}) — running on defaults, leaving the file alone")
        log(f"Fix the JSON in {CONFIG_PATH} and restart to get your settings back.")
        return Config(dict(DEFAULTS), writable=False)

    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        # A typo'd key otherwise looks exactly like a setting that does nothing.
        log(f"Config keys that are not read by anything: {', '.join(unknown)}")

    # Fill in keys added after the user's config was written
    merged = dict(DEFAULTS)
    merged.update(data)
    return Config(merged)
