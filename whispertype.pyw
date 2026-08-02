"""
WhisperType — Push-to-talk voice dictation for Windows.

- Double-tap R-Ctrl: start recording
- Single R-Ctrl during recording: stop recording
- 3s silence also stops recording
- Transcription queue: record next while previous transcribes
- System tray icon with model switching
- Overlay: GPU graph, recording indicator, queue status
"""
import sys
import os
import time
import threading
import tkinter as tk
import ctypes
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
import queue
import gc

# ── Single instance ──────────────────────────────────────────────────────────
# Two daemons both grab the PTT key and both truncate the log. Claim the mutex
# before the log is opened, so a duplicate launch cannot clobber the log of the
# instance that is actually running.
ERROR_ALREADY_EXISTS = 183
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
_instance_mutex = _kernel32.CreateMutexW(None, False, "WhisperType.SingleInstance")
if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
    # .pyw with no console — a message box is the only way to say why nothing
    # happened when the user double-clicks start.bat a second time.
    ctypes.windll.user32.MessageBoxW(
        0, "WhisperType is already running — check the system tray.",
        "WhisperType", 0x40)  # MB_ICONINFORMATION
    sys.exit(0)

# ── Logging ──────────────────────────────────────────────────────────────────
LOG = Path(__file__).parent / "voice_daemon.log"
# Keep one generation: opening "w" outright loses the log of the crash you
# relaunched to diagnose.
try:
    if LOG.exists():
        os.replace(LOG, LOG.with_suffix(".prev.log"))
except OSError:
    pass
_log_f = open(LOG, "w", buffering=1, encoding="utf-8")
sys.stdout = _log_f
sys.stderr = _log_f

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

log("Starting voice daemon...")

sys.path.insert(0, str(Path(__file__).parent))

import json
import pyaudio
import numpy as np
import whisper
import whisper.tokenizer          # for the initial_prompt budget check
import pynput.keyboard
import ctypes.wintypes as wt
from PIL import Image, ImageDraw
import pystray

# ── GPU monitoring (optional) ───────────────────────────────────────────────

_nvml_ok = False
try:
    import pynvml
    pynvml.nvmlInit()
    _gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    _nvml_ok = True
    log(f"NVML initialized: {pynvml.nvmlDeviceGetName(_gpu_handle)}")
except Exception as e:
    log(f"NVML not available (GPU graph disabled): {e}")

# Background GPU history — collected every 1s, always running
gpu_history = []  # list of float [0..1], max 60 entries (last 1 minute)
gpu_history_lock = threading.Lock()

_stopping = [False]   # set on the way out so the collector stops touching NVML

def _gpu_background_collector():
    while not _stopping[0]:
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(_gpu_handle)
            with gpu_history_lock:
                gpu_history.append(util.gpu / 100.0)
                if len(gpu_history) > 60:
                    gpu_history.pop(0)
        except Exception:
            pass
        time.sleep(1.0)

if _nvml_ok:
    threading.Thread(target=_gpu_background_collector, daemon=True).start()
    log("GPU background collector started (1s interval, 60s window)")

# ── Config ───────────────────────────────────────────────────────────────────

CONFIG_PATH   = Path.home() / ".whispertype" / "config.json"
TEMPLATE_PATH = Path(__file__).parent / "config.template.json"

DEFAULT_CFG = {
    "push_to_talk_key":  "ctrl_r",
    "whisper_model":     "large-v3-turbo",
    "language":          "en",
    "sample_rate":       16000,
    "chunk_size":        1024,
    "silence_threshold": 200,
    "silence_duration":  3.0,
    "max_recording_time": 300.0,
}

# Keys the code actually reads. Anything else in config.json is dead weight and
# gets called out at startup rather than silently ignored.
KNOWN_KEYS = set(DEFAULT_CFG) | {"last_model", "fp16"}


# True when config.json exists on disk but could not be read. In that state the
# file still holds the user's edits, so nothing may write over it.
config_unreadable = [False]


def _load_config():
    """User config → bundled template → built-in defaults.

    This is a .pyw with stdout already redirected to the log: an unhandled
    exception here kills the daemon before any UI exists, with nothing on
    screen to explain why.
    """
    def _read(path, label):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f), None
        except FileNotFoundError:
            log(f"No {label} at {path}")
            return None, "missing"
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            log(f"Unusable {label} at {path}: {e}")
            return None, "unreadable"

    def _merge(loaded):
        merged = dict(DEFAULT_CFG)
        merged.update(loaded)
        return merged

    loaded, problem = _read(CONFIG_PATH, "user config")
    if loaded is not None:
        log(f"Config loaded from user config: {CONFIG_PATH}")
        return _merge(loaded)

    tpl, _ = _read(TEMPLATE_PATH, "template")
    if tpl is None:
        if problem == "unreadable":
            config_unreadable[0] = True
        log("No usable config or template — running on built-in defaults.")
        return dict(DEFAULT_CFG)

    if problem == "missing":
        # Seed only when there was genuinely nothing there. A config that exists
        # but fails to parse still contains the user's settings — one stray
        # comma must not cost them their hotkey, language and thresholds.
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(tpl, f, indent=2)
            log(f"Seeded {CONFIG_PATH} from {TEMPLATE_PATH.name}")
        except OSError as e:
            log(f"Could not seed user config: {e}")
    else:
        config_unreadable[0] = True
        log(f"Running on template defaults but LEAVING {CONFIG_PATH} untouched — "
            f"it still holds your settings. Fix the JSON and restart.")

    return _merge(tpl)


cfg = _load_config()

_unknown = sorted(set(cfg) - KNOWN_KEYS)
if _unknown:
    log(f"Config keys that are not read by anything: {', '.join(_unknown)}")

KEY_MAP = {
    "ctrl_r":  pynput.keyboard.Key.ctrl_r,
    "ctrl_l":  pynput.keyboard.Key.ctrl_l,
    "shift_r": pynput.keyboard.Key.shift_r,
    "shift_l": pynput.keyboard.Key.shift_l,
    "alt_r":   pynput.keyboard.Key.alt_r,
    "alt_l":   pynput.keyboard.Key.alt_l,
}
_ptt_name = cfg.get("push_to_talk_key", "ctrl_r")
if _ptt_name not in KEY_MAP:
    log(f"Unknown push_to_talk_key {_ptt_name!r} — falling back to ctrl_r. "
        f"Valid: {', '.join(KEY_MAP)}")
PTT_KEY          = KEY_MAP.get(_ptt_name, pynput.keyboard.Key.ctrl_r)
LANGUAGE         = cfg.get("language", "en")
RATE             = cfg.get("sample_rate", 16000)
CHUNK            = cfg.get("chunk_size", 1024)
SILENCE_THRESH   = cfg.get("silence_threshold", 200)
SILENCE_SECS     = cfg.get("silence_duration", 3.0)
MAX_RECORD_SECS  = cfg.get("max_recording_time", 300.0)
DOUBLE_TAP_MS    = 400

# ── Initial prompt ───────────────────────────────────────────────────────────
# Whisper caps initial_prompt at (n_text_ctx // 2 - 1) tokens — 223 for every
# checkpoint — and drops the overflow off the FRONT without a word. A prompt
# that grew past the cap loses its opening silently, which looks like the bias
# simply not working.
PROMPT_TOKEN_BUDGET = 223
INITIAL_PROMPT_PATH = Path(__file__).parent / "initial_prompt.md"


def _report_prompt_budget(prompt):
    try:
        tok = whisper.tokenizer.get_tokenizer(
            multilingual=True, language=LANGUAGE, task="transcribe")
        toks = tok.encode(" " + prompt.strip())
    except Exception as e:
        log(f"Could not measure initial prompt length: {e}")
        return
    if len(toks) <= PROMPT_TOKEN_BUDGET:
        log(f"Initial prompt: {len(toks)}/{PROMPT_TOKEN_BUDGET} tokens.")
        return
    over = len(toks) - PROMPT_TOKEN_BUDGET
    dropped = tok.decode(toks[:over]).strip()
    log(f"WARNING: initial prompt is {len(toks)} tokens but Whisper only keeps "
        f"the last {PROMPT_TOKEN_BUDGET}. The first {over} tokens are dropped:")
    log(f"  DROPPED -> {dropped[:220]}{'...' if len(dropped) > 220 else ''}")
    log(f"  Shorten {INITIAL_PROMPT_PATH.name} to keep the opening context.")


try:
    INITIAL_PROMPT = INITIAL_PROMPT_PATH.read_text(encoding="utf-8").strip() or None
    if INITIAL_PROMPT:
        log(f"Loaded initial prompt ({len(INITIAL_PROMPT)} chars) from {INITIAL_PROMPT_PATH.name}")
        _report_prompt_budget(INITIAL_PROMPT)
except FileNotFoundError:
    INITIAL_PROMPT = None
    log("No initial_prompt.md — Whisper will run without prompt bias")
except Exception as e:
    INITIAL_PROMPT = None
    log(f"Failed to read {INITIAL_PROMPT_PATH}: {e}")

# All available Whisper models (ordered: best first)
ALL_MODELS = [
    ("large-v3-turbo", "809 MB"),
    ("large-v3",      "1.5 GB"),
    ("large-v2",      "1.5 GB"),
    ("medium",        "769 MB"),
    ("small",         "244 MB"),
    ("base",           "74 MB"),
    ("tiny",           "39 MB"),
]

# Whisper model cache directory
_whisper_cache = Path.home() / ".cache" / "whisper"

def is_model_downloaded(name):
    # Whisper stores models as e.g. large-v3-turbo.pt
    return (_whisper_cache / f"{name}.pt").exists()

# "last_model" is written by the tray switcher; "whisper_model" is what the
# user edits by hand (and what the README documents), so honour it as the seed.
current_model_name = [cfg.get("last_model") or cfg.get("whisper_model") or "large-v3-turbo"]
if current_model_name[0] not in [n for n, _ in ALL_MODELS]:
    log(f"Unknown model {current_model_name[0]!r} in config — falling back to large-v3-turbo")
    current_model_name[0] = "large-v3-turbo"

def save_last_model(name):
    if config_unreadable[0]:
        # cfg is template defaults, not what the user wrote. Persisting it would
        # finish the job of destroying a config we already failed to parse.
        log(f"Not persisting last_model={name}: {CONFIG_PATH} could not be read "
            f"and overwriting it would discard your settings.")
        return
    cfg["last_model"] = name
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)   # atomic — a crash mid-write kept a valid file
    except OSError as e:
        log(f"Failed to save config: {e}")


# ── Queue data structures ───────────────────────────────────────────────────

class JobStatus(Enum):
    WAITING = "waiting"
    TRANSCRIBING = "transcribing"

@dataclass
class TranscriptionJob:
    job_id: int
    audio_bytes: bytes
    target_hwnd: int
    window_name: str
    app_name: str = ""
    audio_duration: float = 0.0
    status: JobStatus = JobStatus.WAITING
    created_at: float = field(default_factory=time.time)
    send_enter: bool = False
    # Set by the overlay's X button. Removing the job from active_jobs only
    # hides the row — the worker still holds it in the queue, so it needs a
    # flag to actually skip the work.
    cancelled: bool = False

@dataclass
class BenchmarkJob:
    job_id: int
    audio_bytes: bytes
    audio_duration: float
    target_hwnd: int
    window_name: str
    app_name: str = ""
    created_at: float = field(default_factory=time.time)
    # Each item: {"model", "status" (waiting|downloading|running|done|error),
    #             "transcribe_secs", "load_secs", "text", "error"}
    results: list = field(default_factory=list)

transcription_queue = queue.Queue()
active_jobs = []
jobs_lock = threading.Lock()
job_counter = [0]

# History: last 50 completed transcriptions
transcription_history = []   # list of dicts: {ts, dur, app, window, text}
history_lock = threading.Lock()
MAX_HISTORY = 50
VISIBLE_HISTORY = 8   # max visible rows before scrollbar appears
HISTORY_ITEM_H = 22   # pixels per history row

# Benchmark state
benchmark_next = [False]              # armed via tray menu toggle
current_benchmark_job = [None]        # BenchmarkJob being processed (for live overlay)
last_benchmark_job = [None]           # last finished BenchmarkJob (for "Open last benchmark")
BENCHMARK_DIR = Path.home() / ".whispertype" / "benchmarks"


# ── Tray icon ────────────────────────────────────────────────────────────────

def make_tray_icon(state="idle"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    colors = {"idle": ("#1e293b", "#6ee7b7"),
              "recording": ("#1e293b", "#f87171"),
              "transcribing": ("#1e293b", "#fbbf24"),
              "downloading": ("#1e293b", "#64748b")}
    bg, fg = colors.get(state, colors["idle"])
    d.ellipse([2, 2, 62, 62], fill=bg)
    if state == "downloading":
        # Down arrow icon
        d.line([32, 12, 32, 44], fill=fg, width=4)
        d.polygon([(20, 36), (44, 36), (32, 52)], fill=fg)
        d.line([18, 56, 46, 56], fill=fg, width=3)
    else:
        # Microphone icon
        d.rounded_rectangle([22, 10, 42, 38], radius=10, fill=fg)
        d.arc([14, 26, 50, 50], 0, 180, fill=fg, width=4)
        d.line([32, 50, 32, 58], fill=fg, width=4)
        d.line([24, 58, 40, 58], fill=fg, width=4)
    return img


# ── Overlay ─────────────────────────────────────────────────────────────────

OFF_SCREEN = "-9999+-9999"
OV_W = 380
MAX_QUEUE_VISIBLE = 5

class RecordingOverlay:
    C = {"bg": "#0f172a", "rec": "#f87171", "trans": "#fbbf24", "text": "#f1f5f9",
         "dim": "#64748b", "bar_bg": "#1e293b",
         "bar_lo": "#6ee7b7", "bar_mid": "#fbbf24", "bar_hi": "#f87171",
         "gpu_fill": "#1a3a2a", "sep": "#334155"}

    def __init__(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0.0)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.title("VR")
        self.root.configure(bg=self.C["bg"])
        self.root.resizable(False, False)
        self.root.geometry(OFF_SCREEN)

        cw = OV_W - 28  # content width (padded)

        # ── 0. Title bar with exit button ──
        self.title_bar = tk.Frame(self.root, bg=self.C["bg"])
        self.title_bar.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(self.title_bar, text="WhisperType", bg=self.C["bg"],
                 fg=self.C["dim"], font=("Segoe UI", 8)).pack(side="left")
        exit_btn = tk.Label(self.title_bar, text="\u00d7", bg=self.C["bg"],
                            fg=self.C["dim"], font=("Segoe UI", 11), cursor="hand2")
        exit_btn.pack(side="right")
        exit_btn.bind("<Button-1>", lambda e: self.hide())
        # Drag on title bar
        self.title_bar.bind("<Button-1>", lambda e: setattr(self, '_d', (e.x, e.y)))
        self.title_bar.bind("<B1-Motion>", self._drag)

        # ── 1. GPU graph section (TOP) ──
        self.gpu_frame = tk.Frame(self.root, bg=self.C["bg"])
        gpu_hdr = tk.Frame(self.gpu_frame, bg=self.C["bg"])
        gpu_hdr.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(gpu_hdr, text="GPU", bg=self.C["bg"], fg=self.C["dim"],
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        self.gpu_pct = tk.Label(gpu_hdr, text="--%", bg=self.C["bg"],
                                fg=self.C["bar_lo"], font=("Consolas", 9, "bold"))
        self.gpu_pct.pack(side="right")
        self.gpu_cv = tk.Canvas(self.gpu_frame, bg=self.C["bar_bg"],
                                width=cw, height=40, highlightthickness=0)
        self.gpu_cv.pack(padx=14, pady=(0, 4))

        # ── 2. Recording section (MIDDLE) ──
        self.rec_frame = tk.Frame(self.root, bg=self.C["bg"])
        self.rec_sep = tk.Frame(self.rec_frame, bg=self.C["sep"], height=1)
        self.rec_sep.pack(fill="x", padx=14, pady=(2, 6))

        hdr = tk.Frame(self.rec_frame, bg=self.C["bg"])
        hdr.pack(fill="x", padx=14, pady=(0, 4))
        self.dot = tk.Label(hdr, text="\u25cf", bg=self.C["bg"], fg=self.C["rec"],
                            font=("Segoe UI", 11))
        self.dot.pack(side="left")
        self.state_lbl = tk.Label(hdr, text="Recording", bg=self.C["bg"],
                                  fg=self.C["text"], font=("Segoe UI", 11, "bold"))
        self.state_lbl.pack(side="left", padx=(4, 0))
        self.timer = tk.Label(hdr, text="0:00", bg=self.C["bg"], fg=self.C["dim"],
                              font=("Consolas", 11))
        self.timer.pack(side="right")

        info_row = tk.Frame(self.rec_frame, bg=self.C["bg"])
        info_row.pack(fill="x", padx=14)
        self.model_lbl = tk.Label(info_row, text="", bg=self.C["bg"],
                                  fg=self.C["dim"], font=("Segoe UI", 8))
        self.model_lbl.pack(side="left")
        self.target_lbl = tk.Label(info_row, text="", bg=self.C["bg"],
                                   fg=self.C["dim"], font=("Segoe UI", 8))
        self.target_lbl.pack(side="right")

        self.level_cv = tk.Canvas(self.rec_frame, bg=self.C["bg"], width=cw,
                                  height=24, highlightthickness=0)
        self.level_cv.pack(padx=14, pady=(4, 2))

        # Hint label — inside rec_frame, below level bar
        self.hint = tk.Label(self.rec_frame, text="", bg=self.C["bg"],
                             fg=self.C["dim"], font=("Segoe UI", 8))
        self.hint.pack(fill="x", padx=14, pady=(2, 4))

        # Drag support on header
        hdr.bind("<Button-1>", lambda e: setattr(self, '_d', (e.x, e.y)))
        hdr.bind("<B1-Motion>", self._drag)

        # ── 3. Queue section (BOTTOM) ──
        self.queue_frame = tk.Frame(self.root, bg=self.C["bg"])
        self.queue_sep = tk.Frame(self.queue_frame, bg=self.C["sep"], height=1)
        self.queue_sep.pack(fill="x", padx=14, pady=(2, 4))
        # Queue header row using grid
        self.queue_hdr = tk.Frame(self.queue_frame, bg=self.C["bg"])
        self.queue_hdr.pack(fill="x", padx=14)
        _qf = ("Consolas", 8)
        _qbg = self.C["bg"]
        _qdim = self.C["dim"]
        for i, (txt, w) in enumerate([("TIME", 9), ("DUR", 5), ("APP", 8), ("WINDOW", 20), ("", 2)]):
            tk.Label(self.queue_hdr, text=txt, bg=_qbg, fg=_qdim, font=_qf,
                     width=w, anchor="w").grid(row=0, column=i, sticky="w")
        self.queue_items_frame = tk.Frame(self.queue_frame, bg=self.C["bg"])
        self.queue_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.queue_item_labels = []

        # ── 4. History section ──
        self.history_frame = tk.Frame(self.root, bg=self.C["bg"])
        tk.Frame(self.history_frame, bg=self.C["sep"], height=1).pack(fill="x", padx=14, pady=(2, 4))
        history_hdr_row = tk.Frame(self.history_frame, bg=self.C["bg"])
        history_hdr_row.pack(fill="x", padx=14)
        history_hdr_row.columnconfigure(3, weight=1)
        _qf = ("Consolas", 8)
        for i, (txt, w) in enumerate([("TIME", 9), ("DUR", 5), ("APP", 8)]):
            tk.Label(history_hdr_row, text=txt, bg=self.C["bg"], fg=self.C["dim"],
                     font=_qf, width=w, anchor="w").grid(row=0, column=i, sticky="w")
        tk.Label(history_hdr_row, text="WINDOW", bg=self.C["bg"], fg=self.C["dim"],
                 font=_qf, anchor="w").grid(row=0, column=3, sticky="we")
        # Scrollable history container (Canvas + Scrollbar)
        history_scroll_container = tk.Frame(self.history_frame, bg=self.C["bg"])
        history_scroll_container.pack(fill="x", padx=14, pady=(0, 6))
        self._history_canvas = tk.Canvas(
            history_scroll_container, bg=self.C["bg"], highlightthickness=0,
            height=VISIBLE_HISTORY * HISTORY_ITEM_H)
        self._history_scrollbar = tk.Scrollbar(
            history_scroll_container, orient="vertical",
            command=self._history_canvas.yview)
        self.history_items_frame = tk.Frame(self._history_canvas, bg=self.C["bg"])
        self.history_items_frame.bind(
            "<Configure>",
            lambda e: self._history_canvas.configure(
                scrollregion=self._history_canvas.bbox("all")))
        self._history_canvas_win = self._history_canvas.create_window(
            (0, 0), window=self.history_items_frame, anchor="nw")
        self._history_canvas.configure(yscrollcommand=self._history_scrollbar.set)
        self._history_canvas.pack(side="left", fill="x", expand=True)
        # Scrollbar packed/forgotten dynamically in _rebuild_history
        # Keep inner frame width in sync with canvas
        self._history_canvas.bind("<Configure>", lambda e: self._history_canvas.itemconfig(
            self._history_canvas_win, width=e.width))

        def _on_history_mousewheel(event):
            self._history_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._on_history_mousewheel = _on_history_mousewheel
        self._history_canvas.bind("<MouseWheel>", _on_history_mousewheel)

        self.history_item_widgets = []

        # ── 5. Benchmark section ──
        self.benchmark_frame = tk.Frame(self.root, bg=self.C["bg"])
        tk.Frame(self.benchmark_frame, bg=self.C["sep"], height=1).pack(fill="x", padx=14, pady=(2, 4))
        bench_hdr_row = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        bench_hdr_row.pack(fill="x", padx=14)
        bench_hdr_row.columnconfigure(2, weight=1)
        _bf = ("Consolas", 8)
        tk.Label(bench_hdr_row, text="BENCHMARK", bg=self.C["bg"], fg=self.C["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.bench_title_lbl = tk.Label(bench_hdr_row, text="", bg=self.C["bg"],
                                        fg=self.C["dim"], font=_bf, anchor="w")
        self.bench_title_lbl.grid(row=0, column=2, sticky="we", padx=(8, 0))
        bench_close = tk.Label(bench_hdr_row, text="\u00d7", bg=self.C["bg"],
                               fg="#ef4444", font=("Segoe UI", 11, "bold"), cursor="hand2")
        bench_close.grid(row=0, column=3, sticky="e", padx=(4, 0))
        bench_close.bind("<Button-1>", lambda e: self._close_benchmark())
        # Column headers
        bench_cols = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        bench_cols.pack(fill="x", padx=14, pady=(2, 0))
        bench_cols.columnconfigure(2, weight=1)
        for i, (txt, w, col) in enumerate([("MODEL", 16, 0), ("TIME", 8, 1)]):
            tk.Label(bench_cols, text=txt, bg=self.C["bg"], fg=self.C["dim"],
                     font=_bf, width=w, anchor="w").grid(row=0, column=col, sticky="w")
        tk.Label(bench_cols, text="TEXT", bg=self.C["bg"], fg=self.C["dim"],
                 font=_bf, anchor="w").grid(row=0, column=2, sticky="we", padx=(4, 0))
        # Items frame (not scrollable — only 7 fixed rows)
        self.benchmark_items_frame = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        self.benchmark_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.benchmark_item_widgets = []
        self._benchmark_view_job = None  # which BenchmarkJob is being displayed

        # ── Shared tooltip widget ──
        self._tooltip = None
        self._tooltip_after = None

        # ── Init state ──
        self.root.geometry(f"{OV_W}x120")
        self.root.update_idletasks()
        self.root.geometry(OFF_SCREEN)

        self._sw = self.root.winfo_screenwidth()
        self._timer_job = None
        self._blink_job = None
        self._level_job = None
        self._gpu_refresh_job = None
        self._blink_on = True
        self._recording = False
        self._visible = False
        self._history_mode = False
        self._benchmark_mode = False
        self._pos = None
        self._level = 0.0

        # Win32 handle of our own window, as a plain int so the keyboard and
        # audio threads can read it without touching Tcl.
        #
        # NOT winfo_id(): on Windows Tk nests a "TkChild" inside a "TkTopLevel"
        # wrapper, winfo_id() returns the child, and GetForegroundWindow()
        # reports the wrapper — so comparing a foreground HWND against
        # winfo_id() never matches, and the overlay never gets skipped.
        self.hwnd = self._read_hwnd()

    # ── Layout helpers ──

    def _read_hwnd(self):
        """Wrapper HWND. Re-read on every show: Tk recreates the wrapper for
        some wm-attribute changes, and a stale handle would silently target
        a destroyed window."""
        try:
            return int(self.root.wm_frame(), 16)
        except Exception as e:
            log(f"Could not read overlay HWND: {e}")
            return 0

    def has_focus(self):
        return bool(self.hwnd
                    and ctypes.windll.user32.GetForegroundWindow() == self.hwnd)

    def _drag(self, e):
        dx, dy = self._d
        x = self.root.winfo_x() + e.x - dx
        y = self.root.winfo_y() + e.y - dy
        self._pos = (x, y)
        self.root.geometry(f"+{x}+{y}")

    def _get_pos(self):
        if self._pos:
            return f"+{self._pos[0]}+{self._pos[1]}"
        return f"+{self._sw // 2 - OV_W // 2}+20"

    def _repack(self):
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.history_frame.pack_forget()
        self.benchmark_frame.pack_forget()
        if _nvml_ok:
            self.gpu_frame.pack(fill="x")
        self.rec_frame.pack(fill="x")
        if self._benchmark_mode:
            self.benchmark_frame.pack(fill="x")
        elif self._history_mode:
            self.history_frame.pack(fill="x")
        else:
            with jobs_lock:
                has_jobs = len(active_jobs) > 0
            if has_jobs:
                self.queue_frame.pack(fill="x")
        self._update_state_display()

    def _update_state_display(self):
        """Update header (dot, state_lbl) and hint text based on current state."""
        if self._recording:
            if benchmark_next[0]:
                self.dot.config(fg=self.C["bar_mid"])
                self.state_lbl.config(text="Recording (benchmark)", fg=self.C["bar_mid"])
            else:
                self.dot.config(fg=self.C["rec"])
                self.state_lbl.config(text="Recording", fg=self.C["text"])
            # Spell out which keys keep the audio and which throw it away \u2014
            # Space used to silently discard, and that must not be guesswork.
            self.hint.config(text="Transcribe: R-Ctrl / Enter\u21b5 / 3s / Space (history)  |  Esc: discard")
        elif self._benchmark_mode:
            running = current_benchmark_job[0] is not None
            self.dot.config(fg=self.C["bar_mid"] if running else self.C["bar_lo"])
            self.state_lbl.config(
                text="Benchmarking..." if running else "Benchmark results",
                fg=self.C["bar_mid"] if running else self.C["bar_lo"],
            )
            self.timer.config(text="")
            self.hint.config(text="Close: \u00d7  |  Hide: Esc")
        elif self._history_mode:
            self.dot.config(fg=self.C["bar_lo"])
            self.state_lbl.config(text="History", fg=self.C["bar_lo"])
            self.timer.config(text="")
            self.hint.config(text="Record: Space  |  Hide: Esc")
        elif self._has_jobs():
            self.dot.config(fg=self.C["trans"])
            self.state_lbl.config(text="Transcribing", fg=self.C["trans"])
            self.timer.config(text="")
            self.hint.config(text="Record: Double R-Ctrl  |  History: Space  |  Hide: Esc")
        else:
            self.dot.config(fg=self.C["dim"])
            self.state_lbl.config(text="Ready", fg=self.C["dim"])
            self.timer.config(text="")
            self.hint.config(text="Record: Double R-Ctrl  |  History: Space  |  Hide: Esc")

    def _calc_height(self):
        h = 20  # title bar
        if _nvml_ok:
            h += 62
        h += 130  # recording section (includes hint inside rec_frame)
        if self._benchmark_mode:
            # header (sep + BENCHMARK row + COLS row) + one row per model.
            # A benchmark only covers *downloaded* models, so sizing off
            # ALL_MODELS leaves dead space when some are missing.
            job = self._benchmark_view_job
            rows = len(job.results) if job and job.results else len(ALL_MODELS)
            h += 48 + rows * HISTORY_ITEM_H
        elif self._history_mode:
            with history_lock:
                n = len(transcription_history)
            visible = min(n, VISIBLE_HISTORY) if n > 0 else 1
            h += 34 + visible * HISTORY_ITEM_H
        else:
            with jobs_lock:
                n = len(active_jobs)
            if n > 0:
                h += 34 + min(n, MAX_QUEUE_VISIBLE) * 22
                if n > MAX_QUEUE_VISIBLE:
                    h += 18
        return max(h, 60)

    def _show_overlay(self):
        self._repack()
        self._rebuild_queue()
        h = self._calc_height()
        pos = self._get_pos()
        self.root.geometry(f"{OV_W}x{h}{pos}")
        self.root.update_idletasks()
        self.root.attributes("-alpha", 0.93)
        self._visible = True
        self.hwnd = self._read_hwnd()
        self._start_gpu_refresh()

    # ── Public methods ──

    def show_recording(self, target_name=""):
        # Defensive: a leaked after-job from a previous recording would double
        # the blink/level rate and never stop on its own.
        self._cancel_timer_blink()
        self._recording = True
        self._history_mode = False
        self._benchmark_mode = False
        self._t0 = time.time()
        self.model_lbl.config(text=f"Model: {current_model_name[0]}")
        self.target_lbl.config(text=f"\u2192 {target_name}" if target_name else "")
        self._draw_level(0)
        self._show_overlay()
        self._tick()
        self._blink()
        self._level_tick()

    def _show_rec_idle(self):
        self._cancel_timer_blink()
        self.model_lbl.config(text=f"Model: {current_model_name[0]}")
        self.target_lbl.config(text="")
        self._draw_level(0)

    def on_recording_stopped(self):
        self._recording = False
        self._cancel_timer_blink()
        # A recording stopped by Space/Esc still enqueues, and this runs after
        # the panel the user asked for is already open. Never pull it away.
        if self._history_mode or self._benchmark_mode:
            self._show_overlay()
            return
        with jobs_lock:
            has_jobs = len(active_jobs) > 0
        if has_jobs:
            self._show_rec_idle()
            self._show_overlay()
        else:
            self.hide()

    def refresh(self):
        if (self._recording or self._has_jobs()
                or self._history_mode or self._benchmark_mode):
            if not self._recording:
                self._show_rec_idle()
            self._show_overlay()
        else:
            self.hide()

    def check_hide(self):
        if (not self._has_jobs() and not self._recording
                and not self._history_mode and not self._benchmark_mode):
            self.hide()

    def hide(self):
        self._cancel_timer_blink()
        self._stop_gpu_refresh()
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.history_frame.pack_forget()
        self.benchmark_frame.pack_forget()
        self._history_mode = False
        self._benchmark_mode = False
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)
        self._visible = False

    def push_level(self, rms):
        """Called from the audio thread ~16x/s. Store only — Tcl is not
        thread-safe, so the redraw happens in _level_tick() on the main loop."""
        self._level = rms

    # ── Internal ──

    def _has_jobs(self):
        with jobs_lock:
            return len(active_jobs) > 0

    def _rebuild_queue(self):
        for w in self.queue_item_labels:
            w.destroy()
        self.queue_item_labels = []

        with jobs_lock:
            jobs = list(active_jobs)
        if not jobs:
            return

        _qf = ("Consolas", 8)
        _qbg = self.C["bg"]

        for idx, job in enumerate(jobs[:MAX_QUEUE_VISIBLE]):
            if job.cancelled:
                color = self.C["sep"]   # cancelled but still finishing on the GPU
            elif job.status == JobStatus.TRANSCRIBING:
                color = self.C["trans"]
            else:
                color = self.C["dim"]
            ts = time.strftime("%H:%M:%S", time.localtime(job.created_at))
            dur = f"{job.audio_duration:.1f}s"
            app = (job.app_name[:8] if job.app_name else "?")
            win = job.window_name

            row = tk.Frame(self.queue_items_frame, bg=_qbg)
            row.pack(fill="x")
            row.columnconfigure(4, weight=1)
            self.queue_item_labels.append(row)

            for i, (txt, w) in enumerate([(ts, 9), (dur, 5), (app, 8), (win, 20)]):
                tk.Label(row, text=txt, bg=_qbg, fg=color, font=_qf,
                         width=w, anchor="w").grid(row=0, column=i, sticky="w")

            # X button to cancel job — right-aligned
            # Already-cancelled jobs keep their row until the worker retires
            # them, but offer no button.
            if not job.cancelled:
                xbtn = tk.Label(row, text="\u00d7", bg=_qbg, fg="#ef4444",
                                font=("Consolas", 9, "bold"), cursor="hand2")
                xbtn.grid(row=0, column=4, sticky="e", padx=(0, 2))
                xbtn.bind("<Button-1>", lambda e, j=job: self._cancel_job(j))

        if len(jobs) > MAX_QUEUE_VISIBLE:
            extra = tk.Label(self.queue_items_frame,
                             text=f"  +{len(jobs) - MAX_QUEUE_VISIBLE} more\u2026",
                             bg=_qbg, fg=self.C["dim"], font=_qf)
            extra.pack(anchor="w")
            self.queue_item_labels.append(extra)

    def _cancel_job(self, job):
        # The job is already sitting in transcription_queue; dropping it from
        # active_jobs only removes the row. Without the flag the worker would
        # still transcribe it and type the text into the target window.
        job.cancelled = True
        with jobs_lock:
            # Only drop a job that has not started. A TRANSCRIBING job is still
            # holding a model on the GPU, and active_jobs is what switch_model()
            # and _do_exit() consult to decide whether work is in flight —
            # removing it early would let a model switch run concurrently with
            # whisper.transcribe(). The worker's finally block removes it.
            if job.status == JobStatus.WAITING and job in active_jobs:
                active_jobs.remove(job)
        log(f"Cancelled job {job.job_id}")
        self.refresh()

    def toggle_history(self):
        if self._benchmark_mode:
            return  # the benchmark panel owns the overlay; Space must not fight it
        self._history_mode = not self._history_mode
        if self._history_mode:
            # Entering history while recording: stop, but keep the audio —
            # it goes through the queue like any other dictation.
            if self._recording:
                self._recording = False
                _stop_current_recording()
                self._cancel_timer_blink()
                log("Recording stopped by Space (history) — still transcribing")
            self._rebuild_history()
            # Smooth transition: repack + resize without alpha flicker
            self._repack()
            h = self._calc_height()
            pos = self._get_pos()
            self.root.geometry(f"{OV_W}x{h}{pos}")
            self.root.update_idletasks()
            self.hwnd = self._read_hwnd()
        else:
            # Exiting history auto-starts a recording, so the target window has
            # to be captured here. Without it the job inherits target_hwnd_pre
            # from the *previous* recording and the text lands in the wrong app.
            if not _can_record():
                # on_press enforces the same precondition; without it here the
                # user gets a full recording UI whose audio is thrown away by
                # the worker with only a log line to show for it.
                self._history_mode = True
                log("Cannot start recording: no model loaded / model switch in progress")
                return
            hwnd = get_real_target_window()
            wname = get_window_text(hwnd)
            _start_recording(hwnd, wname)
            log(f"Recording started (exited history), target: {wname}")
            self.show_recording(wname)

    def _rebuild_history(self):
        for w in self.history_item_widgets:
            w.destroy()
        self.history_item_widgets = []

        with history_lock:
            items = list(reversed(transcription_history))  # newest first

        if not items:
            lbl = tk.Label(self.history_items_frame, text="  No transcriptions yet",
                           bg=self.C["bg"], fg=self.C["dim"], font=("Segoe UI", 8))
            lbl.pack(anchor="w")
            lbl.bind("<MouseWheel>", self._on_history_mousewheel)
            self.history_item_widgets.append(lbl)
            self._history_scrollbar.pack_forget()
            self._history_canvas.configure(height=HISTORY_ITEM_H)
            return

        _qf = ("Consolas", 8)
        _qbg = self.C["bg"]

        for idx, entry in enumerate(items):
            row = tk.Frame(self.history_items_frame, bg=_qbg)
            row.pack(fill="x")
            row.columnconfigure(3, weight=1)  # WINDOW stretches
            self.history_item_widgets.append(row)

            for i, (txt, w) in enumerate([
                (entry["ts"], 9), (entry["dur"], 5), (entry["app"], 8)
            ]):
                tk.Label(row, text=txt, bg=_qbg, fg=self.C["dim"],
                         font=_qf, width=w, anchor="w").grid(row=0, column=i, sticky="w")

            # WINDOW — no fixed width, fills remaining space
            tk.Label(row, text=entry["window"], bg=_qbg, fg=self.C["dim"],
                     font=_qf, anchor="w").grid(row=0, column=3, sticky="we")

            # Buttons frame (copy + delete) — vertically centered
            btn_frame = tk.Frame(row, bg=_qbg)
            btn_frame.grid(row=0, column=4, sticky="e", padx=(4, 0))

            # Copy icon button
            copy_btn = tk.Label(btn_frame, text="\U0001f4cb", bg=_qbg,
                                fg=self.C["bar_lo"], font=("Segoe UI", 11), cursor="hand2")
            copy_btn.pack(side="left", padx=(0, 6))
            copy_btn.bind("<Button-1>", lambda e, t=entry["text"]: self._copy_text(t))
            # Tooltip on hover: show text preview
            preview = entry["text"][:500] + ("\u2026" if len(entry["text"]) > 500 else "")
            copy_btn.bind("<Enter>", lambda e, p=preview: self._show_tooltip(e, p))
            copy_btn.bind("<Leave>", lambda e: self._hide_tooltip())

            # Delete button
            del_btn = tk.Label(btn_frame, text="\u00d7", bg=_qbg, fg="#ef4444",
                               font=("Segoe UI", 11, "bold"), cursor="hand2")
            del_btn.pack(side="left")
            # idx in reversed list → real index is len-1-idx
            real_idx = len(items) - 1 - idx
            del_btn.bind("<Button-1>", lambda e, ri=real_idx: self._delete_history(ri))

            # Mousewheel bindings for scrollable history
            row.bind("<MouseWheel>", self._on_history_mousewheel)
            for child in row.winfo_children():
                child.bind("<MouseWheel>", self._on_history_mousewheel)
                for grandchild in child.winfo_children():
                    grandchild.bind("<MouseWheel>", self._on_history_mousewheel)

        # Show/hide scrollbar based on item count
        if len(items) > VISIBLE_HISTORY:
            self._history_scrollbar.pack(side="right", fill="y")
            self._history_canvas.configure(height=VISIBLE_HISTORY * HISTORY_ITEM_H)
        else:
            self._history_scrollbar.pack_forget()
            self._history_canvas.configure(height=len(items) * HISTORY_ITEM_H)

    # ── Benchmark panel ──

    def show_benchmark(self, job):
        self._benchmark_view_job = job
        self._benchmark_mode = True
        self._history_mode = False
        # If recording, cancel it — benchmark panel takes over
        if self._recording:
            self._recording = False
            _stop_current_recording(discard=True)
            self._cancel_timer_blink()
            log("Recording discarded — benchmark panel took over the overlay")
        self._rebuild_benchmark()
        self._show_overlay()

    def refresh_benchmark(self):
        # Prefer currently running job, fall back to last finished, else current view
        job = current_benchmark_job[0] or last_benchmark_job[0] or self._benchmark_view_job
        if job is None:
            return
        self._benchmark_view_job = job
        if not self._benchmark_mode:
            return
        self._rebuild_benchmark()
        self._update_state_display()

    def _close_benchmark(self):
        self._benchmark_mode = False
        self._benchmark_view_job = None
        # If a benchmark is still running, just hide the panel — worker keeps going
        self.hide()

    def _rebuild_benchmark(self):
        for w in self.benchmark_item_widgets:
            w.destroy()
        self.benchmark_item_widgets = []

        job = self._benchmark_view_job
        if job is None:
            return

        created = time.strftime("%H:%M:%S", time.localtime(job.created_at))
        self.bench_title_lbl.config(
            text=f"{created}  \u2022  {job.audio_duration:.1f}s  \u2022  {job.app_name or '?'}"
        )

        _f = ("Consolas", 8)
        _bg = self.C["bg"]

        status_icons = {
            "waiting": ("\u25cb", self.C["dim"]),        # ○
            "downloading": ("\u2193", self.C["bar_mid"]),  # ↓
            "loading": ("\u21bb", self.C["bar_mid"]),    # ↻
            "running": ("\u25b6", self.C["trans"]),      # ▶
            "done": ("\u2713", self.C["bar_lo"]),        # ✓
            "error": ("\u00d7", "#ef4444"),              # ×
        }

        for r in job.results:
            row = tk.Frame(self.benchmark_items_frame, bg=_bg)
            row.pack(fill="x")
            row.columnconfigure(2, weight=1)
            self.benchmark_item_widgets.append(row)

            icon, icon_col = status_icons.get(r["status"], ("?", self.C["dim"]))
            name_txt = f"{icon} {r['model']}"
            tk.Label(row, text=name_txt, bg=_bg, fg=icon_col, font=_f,
                     width=18, anchor="w").grid(row=0, column=0, sticky="w")

            if r["transcribe_secs"] is not None:
                time_txt = f"{r['transcribe_secs']:.2f}s"
                time_fg = self.C["bar_lo"]
            elif r["status"] in ("running", "loading", "downloading"):
                time_txt = "\u2026"
                time_fg = self.C["bar_mid"]
            elif r["status"] == "error":
                time_txt = "ERR"
                time_fg = "#ef4444"
            else:
                time_txt = "\u2014"
                time_fg = self.C["dim"]
            tk.Label(row, text=time_txt, bg=_bg, fg=time_fg, font=_f,
                     width=8, anchor="w").grid(row=0, column=1, sticky="w")

            if r["status"] == "done" and r["text"] is not None:
                preview = r["text"][:40] + ("\u2026" if len(r["text"]) > 40 else "")
                preview_fg = self.C["text"]
            elif r["status"] == "error":
                preview = (r["error"] or "error")[:40]
                preview_fg = "#ef4444"
            elif r["status"] == "downloading":
                preview = "downloading..."
                preview_fg = self.C["bar_mid"]
            elif r["status"] == "loading":
                preview = "loading model..."
                preview_fg = self.C["bar_mid"]
            elif r["status"] == "running":
                preview = "transcribing..."
                preview_fg = self.C["bar_mid"]
            else:
                preview = ""
                preview_fg = self.C["dim"]

            preview_lbl = tk.Label(row, text=preview, bg=_bg, fg=preview_fg,
                                   font=_f, anchor="w")
            preview_lbl.grid(row=0, column=2, sticky="we", padx=(4, 0))

            if r["status"] == "done" and r["text"]:
                full = r["text"]
                tip = full[:500] + ("\u2026" if len(full) > 500 else "")
                preview_lbl.bind("<Enter>", lambda e, p=tip: self._show_tooltip(e, p))
                preview_lbl.bind("<Leave>", lambda e: self._hide_tooltip())
                copy_btn = tk.Label(row, text="\U0001f4cb", bg=_bg,
                                    fg=self.C["bar_lo"], font=("Segoe UI", 10), cursor="hand2")
                copy_btn.grid(row=0, column=3, sticky="e", padx=(4, 0))
                copy_btn.bind("<Button-1>", lambda e, t=full: self._copy_text(t))

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keep clipboard after window close

    def _delete_history(self, idx):
        with history_lock:
            if 0 <= idx < len(transcription_history):
                transcription_history.pop(idx)
        self._rebuild_history()
        self._show_overlay()

    def _show_tooltip(self, event, text):
        self._hide_tooltip()
        x = event.widget.winfo_rootx()
        self._tooltip = tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg="#1e293b")
        lbl = tk.Label(tw, text=text, bg="#1e293b", fg="#f1f5f9",
                       font=("Segoe UI", 10), wraplength=360, justify="left",
                       padx=8, pady=4)
        lbl.pack()
        tw.update_idletasks()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Try to position above the widget
        widget_y = event.widget.winfo_rooty()
        y = widget_y - tw_h - 4
        if y < 0:
            # Falls off top — position below the widget instead
            y = widget_y + event.widget.winfo_height() + 4
        if y + tw_h > sh:
            y = sh - tw_h - 4
        # Horizontal clamping
        if x + tw_w > sw:
            x = sw - tw_w - 4
        if x < 0:
            x = 4
        tw.geometry(f"+{x}+{y}")

    def _hide_tooltip(self):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _cancel_timer_blink(self):
        for j in (self._timer_job, self._blink_job, self._level_job):
            if j:
                self.root.after_cancel(j)
        self._timer_job = self._blink_job = self._level_job = None
        self._level = 0.0

    def _tick(self):
        e = time.time() - self._t0
        self.timer.config(text=f"{int(e)//60}:{int(e)%60:02d}")
        self._timer_job = self.root.after(500, self._tick)

    def _level_tick(self):
        self._draw_level(self._level)
        self._level_job = self.root.after(60, self._level_tick)

    def _blink(self):
        self._blink_on = not self._blink_on
        self.dot.config(fg=self.C["rec"] if self._blink_on else self.C["bg"])
        self._blink_job = self.root.after(500, self._blink)

    def _draw_level(self, rms, mx=4000):
        c = self.level_cv; c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, W, H, fill=self.C["bar_bg"], outline="")
        r = min(rms / mx, 1.0)
        fw = int(W * r)
        clr = self.C["bar_lo"] if r < 0.4 else (self.C["bar_mid"] if r < 0.75 else self.C["bar_hi"])
        if fw > 0:
            c.create_rectangle(0, 0, fw, H, fill=clr, outline="")
        sx = int(W * min(SILENCE_THRESH / mx, 1.0))
        c.create_line(sx, 0, sx, H, fill="#ef4444", width=2)
        for p in (0.25, 0.5, 0.75):
            c.create_line(int(W * p), 0, int(W * p), H, fill="#0f172a")

    # ── GPU graph (reads from global gpu_history) ──

    def _start_gpu_refresh(self):
        if not _nvml_ok or self._gpu_refresh_job:
            return
        self._refresh_gpu()

    def _stop_gpu_refresh(self):
        if self._gpu_refresh_job:
            self.root.after_cancel(self._gpu_refresh_job)
            self._gpu_refresh_job = None

    def _refresh_gpu(self):
        with gpu_history_lock:
            history = list(gpu_history)
        if history:
            pct = int(history[-1] * 100)
            color = self.C["bar_lo"] if pct < 50 else (self.C["bar_mid"] if pct < 80 else self.C["bar_hi"])
            self.gpu_pct.config(text=f"{pct}%", fg=color)
        self._draw_gpu_graph(history)
        self._gpu_refresh_job = self.root.after(1000, self._refresh_gpu)

    def _draw_gpu_graph(self, history):
        c = self.gpu_cv
        c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, W, H, fill=self.C["bar_bg"], outline="")

        # Reference lines (draw first, behind the graph)
        for p in (0.25, 0.5, 0.75):
            y = int(H * (1.0 - p))
            c.create_line(0, y, W, y, fill="#1e293b")

        n = len(history)
        if n < 2:
            return

        # Right-aligned: if < 60 samples, don't stretch to full width
        graph_w = int(W * (n / 60.0))
        x_off = W - graph_w
        step = graph_w / max(n - 1, 1)

        points = []
        for i, val in enumerate(history):
            x = x_off + int(i * step)
            y = int(H * (1.0 - val))
            points.append((x, y))

        # Fill area under curve
        last_x = x_off + int((n - 1) * step)
        fill_pts = [(x_off, H)] + points + [(last_x, H)]
        c.create_polygon(fill_pts, fill=self.C["gpu_fill"], outline="")

        # Line segments
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            c.create_line(x1, y1, x2, y2, fill=self.C["bar_lo"], width=2)

    def run(self):
        self.root.mainloop()


# ── Audio (single PyAudio instance) ─────────────────────────────────────────

_pa = pyaudio.PyAudio()
log(f"PyAudio initialized: {_pa.get_default_input_device_info()['name']}")

def record_until_stop(stop_event, level_callback=None):
    stream = _pa.open(format=pyaudio.paInt16, channels=1,
                      rate=RATE, input=True, frames_per_buffer=CHUNK)
    frames = []
    silence_since = None
    start = time.time()
    try:
        while not stop_event.is_set():
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
            arr = np.frombuffer(data, np.int16).astype(np.float64)
            rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0
            if level_callback:
                level_callback(rms)
            if rms < SILENCE_THRESH:
                if silence_since is None:
                    silence_since = time.time()
                elif time.time() - silence_since > SILENCE_SECS:
                    break
            else:
                silence_since = None
            if time.time() - start > MAX_RECORD_SECS:
                break
    finally:
        stream.stop_stream()
        stream.close()
    return b"".join(frames) if frames else None


# ── Whisper ──────────────────────────────────────────────────────────────────

import torch
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
log(f"Torch device: {DEVICE}")

# fp16 is the right default on Turing and newer (sm_70+, tensor cores). Pascal
# (sm_61, e.g. GTX 10xx) runs fp16 math at 1/64 rate, where fp32 can win — so
# make it overridable and log the capability needed to decide.
FP16 = bool(cfg.get("fp16", DEVICE == "cuda"))
if DEVICE == "cuda":
    _props = torch.cuda.get_device_properties(0)
    log(f"GPU: {_props.name} (sm_{_props.major}{_props.minor}, "
        f"{_props.total_memory / 1024**3:.1f} GB) | fp16={FP16}")
    if FP16 and _props.major < 7:
        log(f'Note: sm_{_props.major}{_props.minor} has no tensor cores. '
            f'Try "fp16": false in config.json and compare with the benchmark.')
else:
    FP16 = False  # whisper refuses fp16 on CPU

wmodel = [None]


def _free_cuda():
    """Release Whisper's intermediate tensors and PyTorch's caching allocator.
    Without this, days of dictation accumulate GBs of allocator fragmentation."""
    gc.collect()
    if DEVICE == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass


def load_model(name):
    log(f"Loading {name} on {DEVICE}...")
    t0 = time.perf_counter()
    m = whisper.load_model(name, device=DEVICE)
    log(f"{name} ready on {DEVICE} ({time.perf_counter() - t0:.1f}s).")
    return m


def transcribe(audio_bytes):
    # Bind the model once: a tray-driven switch could rebind wmodel[0] while
    # this is running, and the run must finish on the model it started with.
    model = wmodel[0]
    if model is None:
        raise RuntimeError("no model loaded")
    # Convert raw PCM to float32 numpy — bypasses whisper.load_audio() / ffmpeg
    audio_np = np.frombuffer(audio_bytes, np.int16).astype(np.float32) / 32768.0
    # Use whisper.transcribe() with numpy array (no ffmpeg, handles any length)
    # condition_on_previous_text=False prevents hallucination loops on long audio
    result = whisper.transcribe(model, audio_np, language=LANGUAGE,
                                initial_prompt=INITIAL_PROMPT,
                                condition_on_previous_text=False,
                                compression_ratio_threshold=2.4,
                                logprob_threshold=-1.0,
                                no_speech_threshold=0.6,
                                fp16=FP16)
    text = result["text"].strip()
    del result, audio_np, model
    _free_cuda()
    return text


# ── Benchmark ────────────────────────────────────────────────────────────────

def _save_benchmark(job):
    try:
        BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(job.created_at))
        data = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.created_at)),
            "audio_duration_secs": round(job.audio_duration, 2),
            "app": job.app_name,
            "window": job.window_name,
            "device": DEVICE,
            "results": job.results,
        }
        json_path = BENCHMARK_DIR / f"benchmark_{ts}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        with open(BENCHMARK_DIR / "benchmark_log.txt", "a", encoding="utf-8") as f:
            f.write(
                f"\n=== {data['created_at']}  audio={data['audio_duration_secs']}s  "
                f"app={job.app_name}  window={job.window_name}  device={DEVICE} ===\n"
            )
            for r in job.results:
                t = f"{r['transcribe_secs']:.2f}s" if r['transcribe_secs'] is not None else "—"
                txt = r['text'] if r['text'] is not None else (r['error'] or "")
                f.write(f"  [{r['model']:<16}] {t:>7}  {txt}\n")
        log(f"Benchmark saved: {json_path.name}")
    except Exception as e:
        log(f"Failed to save benchmark: {e}")


def _run_benchmark(job):
    log(f"Benchmark job {job.job_id} started ({job.audio_duration:.1f}s audio, {len(job.results)} models)")
    original_model = current_model_name[0]
    # Lock out dictation model switching while benchmark runs
    model_switching[0] = True
    current_benchmark_job[0] = job

    # Drop the live dictation model first. Keeping it resident means every
    # benchmarked model shares VRAM with it — large-v3 alongside large-v3-turbo
    # is ~4.7 GB, which is a real risk on an 8 GB card.
    wmodel[0] = None
    _free_cuda()

    audio_np = np.frombuffer(job.audio_bytes, np.int16).astype(np.float32) / 32768.0

    def _refresh():
        if overlay:
            overlay.root.after(0, overlay.refresh_benchmark)

    _refresh()

    for i, r in enumerate(job.results):
        name = r["model"]
        m = None
        try:
            job.results[i]["status"] = "loading"
            _refresh()

            t0 = time.perf_counter()
            m = whisper.load_model(name, device=DEVICE)
            load_secs = time.perf_counter() - t0
            job.results[i]["load_secs"] = load_secs

            job.results[i]["status"] = "running"
            _refresh()

            t0 = time.perf_counter()
            result = whisper.transcribe(m, audio_np, language=LANGUAGE,
                                        initial_prompt=INITIAL_PROMPT,
                                        condition_on_previous_text=False,
                                        compression_ratio_threshold=2.4,
                                        logprob_threshold=-1.0,
                                        no_speech_threshold=0.6,
                                        fp16=FP16)
            transcribe_secs = time.perf_counter() - t0

            job.results[i]["transcribe_secs"] = transcribe_secs
            job.results[i]["text"] = result["text"].strip()
            job.results[i]["status"] = "done"
            log(f"Benchmark [{name}] {transcribe_secs:.2f}s: {job.results[i]['text'][:60]}")
        except Exception as e:
            job.results[i]["status"] = "error"
            job.results[i]["error"] = str(e)
            log(f"Benchmark [{name}] ERROR: {e}")
        finally:
            # Also runs when load or transcribe raised — otherwise a partially
            # loaded model would stay pinned in VRAM for the rest of the run.
            m = None
            _free_cuda()
        _refresh()

    # Persist result + restore original model
    _save_benchmark(job)
    last_benchmark_job[0] = job
    current_benchmark_job[0] = None

    try:
        log(f"Restoring original model: {original_model}")
        wmodel[0] = load_model(original_model)
    except Exception as e:
        log(f"CRITICAL: could not restore {original_model}: {e} — "
            f"dictation stays disabled until the model is switched or the app restarts")

    model_switching[0] = False
    refresh_tray()
    _refresh()
    log(f"Benchmark job {job.job_id} complete")


_downloading_all = [False]


def _ensure_all_models_downloaded():
    """Background: pre-download any missing Whisper models onto CPU (cache only).

    Wired to the tray's "Download all models" item — the benchmark only covers
    models that are already on disk, so this is what makes it comprehensive.
    """
    if _downloading_all[0]:
        log("Model pre-download already running.")
        return
    missing = [n for n, _ in ALL_MODELS if not is_model_downloaded(n)]
    if not missing:
        log("All Whisper models already downloaded.")
        return
    _downloading_all[0] = True
    try:
        log(f"Pre-downloading {len(missing)} missing model(s): {', '.join(missing)}")
        if tray_icon[0]:
            tray_icon[0].icon = make_tray_icon("downloading")
        for name in missing:
            try:
                log(f"Downloading {name}...")
                # Load on CPU so GPU memory isn't touched; only the cached .pt
                # file matters. Release the weights immediately.
                m = whisper.load_model(name, device="cpu")
                del m
                gc.collect()
                log(f"Downloaded {name}.")
                # Refresh tray so the ↓ arrow disappears
                if tray_icon[0]:
                    try:
                        tray_icon[0].menu = build_tray_menu()
                    except Exception:
                        pass
            except Exception as e:
                log(f"Failed to pre-download {name}: {e}")
        log("Background model download finished.")
    finally:
        _downloading_all[0] = False
        refresh_tray()


# ── Auto-type via Win32 SendInput ────────────────────────────────────────────

INPUT_KEYBOARD    = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP   = 0x0002

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", wt.DWORD), ("_u", _INPUT_UNION)]

_extra = ctypes.pointer(ctypes.c_ulong(0))

# Separate handle so ctypes preserves GetLastError for the SendInput checks.
_user32_le = ctypes.WinDLL("user32", use_last_error=True)

def get_foreground_window():
    return ctypes.windll.user32.GetForegroundWindow()

def get_real_target_window():
    """Get the foreground window, skipping our own overlay.

    Returns 0 when the overlay is in front and nothing behind it qualifies —
    callers must treat that as "no target" rather than typing blind.
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    ov = overlay.hwnd if overlay else 0
    if ov and hwnd == ov:
        # Overlay is focused (history mode, or the user dragged it) — walk the
        # Z-order for the window underneath.
        GW_HWNDNEXT = 2
        candidate = user32.GetWindow(hwnd, GW_HWNDNEXT)
        while candidate:
            # Skip our own windows, child windows, invisible and untitled ones
            if (candidate != ov
                    and user32.GetParent(candidate) == 0
                    and user32.IsWindowVisible(candidate)
                    and user32.GetWindowTextLengthW(candidate) > 0):
                return candidate
            candidate = user32.GetWindow(candidate, GW_HWNDNEXT)
        log("get_real_target_window: overlay is in front and no window behind it")
        return 0
    return hwnd

def set_foreground_window(hwnd):
    """Activate target window with retry. Returns True if the window is foreground."""
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        log(f"set_foreground_window: HWND {hwnd} is no longer valid")
        return False
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    elif user32.GetForegroundWindow() == hwnd:
        # Already there — skip the synthetic Alt below, which can pop the
        # target app's menu bar as a side effect.
        return True
    # Press and release Alt to allow SetForegroundWindow from background
    alt_inp = INPUT()
    alt_inp.type = INPUT_KEYBOARD
    alt_inp.ki.wVk = 0x12  # VK_MENU (Alt)
    alt_inp.ki.dwFlags = 0
    alt_up = INPUT()
    alt_up.type = INPUT_KEYBOARD
    alt_up.ki.wVk = 0x12
    alt_up.ki.dwFlags = KEYEVENTF_KEYUP
    arr = (INPUT * 2)(alt_inp, alt_up)
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))
    # Try SetForegroundWindow, then poll to confirm it worked
    for attempt in range(2):
        user32.SetForegroundWindow(hwnd)
        for _ in range(10):  # poll up to ~500ms
            time.sleep(0.05)
            if user32.GetForegroundWindow() == hwnd:
                return True
        # Second attempt: try BringWindowToTop as fallback
        if attempt == 0:
            user32.BringWindowToTop(hwnd)
            log(f"set_foreground_window: retry with BringWindowToTop for HWND {hwnd}")
    log(f"set_foreground_window: FAILED to activate HWND {hwnd} after retries")
    return False

def get_window_text(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
    title = buf.value.strip()
    return (title[:20] + "\u2026") if len(title) > 20 else (title or "(untitled)")

def get_process_name(hwnd):
    try:
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid.value)
        if h:
            buf = ctypes.create_unicode_buffer(260)
            size = wt.DWORD(260)
            ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            ctypes.windll.kernel32.CloseHandle(h)
            name = buf.value.strip()
            if name:
                return Path(name).stem  # e.g. "Code" from "C:\...\Code.exe"
    except Exception:
        pass
    return "?"

# One SendInput call per ~200 characters. A 300 s dictation is tens of
# thousands of INPUT structs, and several apps drop the tail of a single huge
# batch rather than queueing it.
TYPE_CHUNK_CHARS = 200


def _send_inputs(events):
    """Send one batch and report short writes instead of losing them quietly."""
    n = len(events)
    if n == 0:
        return 0
    sent = _user32_le.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))
    if sent != n:
        log(f"SendInput: {sent}/{n} events accepted (GetLastError={ctypes.get_last_error()})")
    return sent


def auto_type(text, target_hwnd=None):
    if not target_hwnd:
        log("auto_type: no target window — text kept in history only")
        return
    if overlay and target_hwnd == overlay.hwnd:
        log("auto_type: target is our own overlay — text kept in history only")
        return
    try:
        activated = set_foreground_window(target_hwnd)
    except Exception as e:
        log(f"auto_type: activation raised {e}")
        activated = False
    if not activated:
        log("auto_type: skipped — could not activate target window (text in history)")
        return

    # Iterate UTF-16 code units, not characters: wScan is a WORD, so a
    # non-BMP character (emoji) must go out as its two surrogate halves.
    sent = expected = 0
    for start in range(0, len(text), TYPE_CHUNK_CHARS):
        units = text[start:start + TYPE_CHUNK_CHARS].encode("utf-16-le")
        events = []
        for i in range(0, len(units), 2):
            code = units[i] | (units[i + 1] << 8)
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = INPUT()
                inp.type = INPUT_KEYBOARD
                inp.ki.wVk = 0
                inp.ki.wScan = code
                inp.ki.dwFlags = flags
                inp.ki.time = 0
                inp.ki.dwExtraInfo = _extra
                events.append(inp)
        expected += len(events)
        sent += _send_inputs(events)
        if start + TYPE_CHUNK_CHARS < len(text):
            time.sleep(0.005)  # let the target's message pump drain
    if sent != expected:
        log(f"auto_type: only {sent}/{expected} keystroke events delivered")

def send_enter_key():
    """Send a single Enter keypress via SendInput."""
    VK_RETURN = 0x0D
    events = []
    for flags in (0, KEYEVENTF_KEYUP):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = VK_RETURN
        inp.ki.wScan = 0
        inp.ki.dwFlags = flags
        inp.ki.time = 0
        inp.ki.dwExtraInfo = _extra
        events.append(inp)
    _send_inputs(events)


# ── Transcription worker (queue consumer) ───────────────────────────────────

def _transcription_worker():
    while True:
        job = transcription_queue.get()

        if isinstance(job, BenchmarkJob):
            try:
                if tray_icon[0]:
                    tray_icon[0].icon = make_tray_icon("transcribing")
                overlay.root.after(0, lambda j=job: overlay.show_benchmark(j))
                _run_benchmark(job)
            except Exception as e:
                import traceback
                log(f"Benchmark worker error (job {job.job_id}): {e}")
                log(traceback.format_exc())
            finally:
                transcription_queue.task_done()
                overlay.root.after(0, overlay.refresh_benchmark)
                if tray_icon[0]:
                    tray_icon[0].icon = make_tray_icon(_tray_state())
            continue

        try:
            if job.cancelled:
                log(f"Job {job.job_id} was cancelled — skipped")
            else:
                job.status = JobStatus.TRANSCRIBING
                overlay.root.after(0, overlay.refresh)

                if tray_icon[0]:
                    tray_icon[0].icon = make_tray_icon("transcribing")

                text = transcribe(job.audio_bytes)
                log(f"Transcribed (job {job.job_id}, target={job.window_name}): {text}")

                if text:
                    with history_lock:
                        transcription_history.append({
                            "ts": time.strftime("%H:%M:%S", time.localtime(job.created_at)),
                            "dur": f"{job.audio_duration:.1f}s",
                            "app": job.app_name[:8] if job.app_name else "?",
                            "window": job.window_name,
                            "text": text,
                        })
                        if len(transcription_history) > MAX_HISTORY:
                            transcription_history.pop(0)
                    # Always keep the text in history; only the typing is
                    # suppressed on shutdown or a mid-flight cancel.
                    if shutting_down[0]:
                        log(f"Shutdown: saved to history but skipped auto_type for job {job.job_id}")
                    elif job.cancelled:
                        log(f"Job {job.job_id} cancelled mid-transcription — kept in history, not typed")
                    else:
                        auto_type(text, target_hwnd=job.target_hwnd)
                        if job.send_enter:
                            time.sleep(0.05)
                            send_enter_key()
                            log(f"Sent Enter after transcription (job {job.job_id})")
        except Exception as e:
            import traceback
            log(f"Transcription error (job {job.job_id}): {e}")
            log(traceback.format_exc())
        finally:
            with jobs_lock:
                if job in active_jobs:
                    active_jobs.remove(job)
            transcription_queue.task_done()
            overlay.root.after(0, overlay.refresh)
            overlay.root.after(100, overlay.check_hide)
            if tray_icon[0]:
                tray_icon[0].icon = make_tray_icon(_tray_state())


# ── PTT: double-tap to start, single-tap to stop ────────────────────────────

recording        = [False]
model_switching  = [False]
stop_event       = [threading.Event()]
last_tap       = [0.0]
enter_stop     = [False]   # True when recording was stopped via Enter key
target_hwnd_pre  = [None]   # HWND captured before overlay steals focus
target_wname_pre = [""]     # window title captured before recording
overlay        = None

# Every recording gets a generation number. The audio thread captures its own
# and refuses to touch shared state once a newer recording has replaced it.
#
# This matters because stopping a recording is not instantaneous: the thread is
# blocked in stream.read() for up to a chunk period (~64 ms) plus PyAudio's
# stop/close, so "discard this and start a new one" — which is exactly what
# Space-Space does — leaves the old thread running while the new one begins.
# With plain shared flags the dying thread would clear recording[0] on the live
# recording and enqueue the audio the user just threw away.
rec_gen      = [0]      # generation of the most recently started recording
discard_gen  = [-1]     # generation the UI asked to throw away
rec_thread   = [None]   # the live recording thread, for shutdown to join


def _can_record():
    """Preconditions for starting a recording at all."""
    return wmodel[0] is not None and not model_switching[0]


def _start_recording(target_hwnd, window_name):
    """Begin a recording. Caller is responsible for the overlay update."""
    rec_gen[0] += 1
    gen = rec_gen[0]
    recording[0] = True
    enter_stop[0] = False
    target_hwnd_pre[0] = target_hwnd
    target_wname_pre[0] = window_name
    ev = threading.Event()
    stop_event[0] = ev
    t = threading.Thread(target=_record_and_enqueue,
                         args=(gen, ev, target_hwnd, window_name), daemon=True)
    rec_thread[0] = t
    t.start()
    return gen


def _stop_current_recording(discard=False):
    """Stop the live recording.

    discard=False keeps the audio: it goes through the queue exactly as if the
    push-to-talk key had ended it. That is what Space does — glancing at the
    history panel must not cost you a dictation you already spoke.

    discard=True throws the audio away. Reserved for the deliberate "forget
    this" gestures: Esc, opening the benchmark panel, and shutdown.
    """
    if not recording[0]:
        return
    if discard:
        discard_gen[0] = rec_gen[0]
    recording[0] = False
    stop_event[0].set()


def _whispertype_owns_keys():
    """Should Space/Esc be treated as WhisperType commands right now?

    The pynput listener is global and does not suppress, so "overlay is
    visible" is far too wide a net: the overlay also stays up while the queue
    drains, and a space typed into your editor then opened the history panel
    (and the next one started a recording). Restrict it to states the user
    deliberately put WhisperType into.
    """
    if not overlay or not overlay._visible:
        return False
    return (recording[0]
            or overlay._history_mode
            or overlay._benchmark_mode
            or overlay.has_focus())


def on_press(key):
    # Space toggles history view
    if key == pynput.keyboard.Key.space and _whispertype_owns_keys():
        overlay.root.after(0, overlay.toggle_history)
        return

    # Escape: minimize to tray. Unlike Space this only ever hides, so it stays
    # available whenever the overlay is on screen — including while the queue
    # drains, which is exactly where the overlay's own hint promises "Hide: Esc".
    if key == pynput.keyboard.Key.esc and overlay and overlay._visible:
        if recording[0]:
            log("Recording discarded by Esc")
        _stop_current_recording(discard=True)
        overlay.root.after(0, overlay.hide)
        return

    # Enter key: stop recording + flag to send Enter after transcription
    if key == pynput.keyboard.Key.enter and recording[0]:
        recording[0] = False
        enter_stop[0] = True
        stop_event[0].set()
        log("Recording stopped by Enter (will send Enter after transcription)")
        return

    if key != PTT_KEY or not _can_record():
        return
    now = time.time()

    if recording[0]:
        recording[0] = False
        stop_event[0].set()
        log("Recording stopped by keypress")
    else:
        if (now - last_tap[0]) * 1000 < DOUBLE_TAP_MS:
            # Capture target window, skipping overlay if it has focus (e.g. history mode)
            hwnd = get_real_target_window()
            wname = get_window_text(hwnd)
            _start_recording(hwnd, wname)
            overlay.root.after(0, lambda: overlay.show_recording(wname))
            log(f"Recording started (double-tap), target: {wname}")
        last_tap[0] = now


def on_release(key):
    pass


def _record_and_enqueue(gen, stop_ev, target_hwnd, window_name):
    try:
        audio = record_until_stop(stop_ev, level_callback=overlay.push_level)

        # A newer recording may have started while we were draining out of
        # stream.read() (Space-Space is fast enough to do exactly that). When
        # that happens this thread no longer owns recording[]/enter_stop[] or
        # the overlay — but it still owns its audio, and that audio must be
        # transcribed like any other. Being superseded is not a reason to
        # throw away something the user already said.
        superseded = gen != rec_gen[0]
        discarded = gen == discard_gen[0]

        if not superseded:
            recording[0] = False

        if not audio or discarded:
            if not superseded:
                enter_stop[0] = False
                if not overlay._history_mode:
                    overlay.root.after(0, overlay.on_recording_stopped)
            log("Recording discarded" if audio else "No audio captured")
            return

        duration = len(audio) / (RATE * 2)  # 16-bit PCM = 2 bytes per sample
        app_name = get_process_name(target_hwnd)
        job_counter[0] += 1

        if superseded:
            # The Enter flag and the benchmark arming belong to whichever
            # recording is live now, not to this one.
            _send_enter = False
            log(f"Recording {gen} was superseded by {rec_gen[0]} — "
                f"transcribing its audio anyway")
        else:
            _send_enter = enter_stop[0]
            enter_stop[0] = False

        if benchmark_next[0] and not superseded:
            benchmark_next[0] = False
            refresh_tray()  # updates the checkbox state
            downloaded_models = [m for m, _ in ALL_MODELS if is_model_downloaded(m)]
            if not downloaded_models:
                log("Benchmark skipped: no models downloaded")
                overlay.root.after(0, overlay.on_recording_stopped)
                return
            job = BenchmarkJob(
                job_id=job_counter[0],
                audio_bytes=audio,
                audio_duration=duration,
                target_hwnd=target_hwnd,
                window_name=window_name,
                app_name=app_name,
                results=[{"model": m, "status": "waiting",
                          "transcribe_secs": None, "load_secs": None,
                          "text": None, "error": None}
                         for m in downloaded_models],
            )
            overlay.root.after(0, overlay.on_recording_stopped)
            transcription_queue.put(job)
            log(f"Enqueued BENCHMARK job {job.job_id} ({duration:.1f}s, {len(downloaded_models)} downloaded models)")
            if tray_icon[0]:
                tray_icon[0].icon = make_tray_icon("transcribing")
            return

        job = TranscriptionJob(
            job_id=job_counter[0],
            audio_bytes=audio,
            target_hwnd=target_hwnd,
            window_name=window_name,
            app_name=app_name,
            audio_duration=duration,
            send_enter=_send_enter,
        )

        with jobs_lock:
            active_jobs.append(job)

        # Schedule overlay update BEFORE putting in queue to avoid race
        # (worker might finish and clear job before mainloop processes
        # on_recording_stopped). Skipped when superseded: a newer recording is
        # live and on_recording_stopped would cancel its timers.
        if superseded:
            overlay.root.after(0, overlay.refresh)
        else:
            overlay.root.after(0, overlay.on_recording_stopped)
        transcription_queue.put(job)
        log(f"Enqueued job {job.job_id} for '{window_name}' ({duration:.1f}s)")

        if tray_icon[0]:
            # _tray_state() so a recording that is still live keeps the
            # recording icon instead of being overwritten by this older job.
            tray_icon[0].icon = make_tray_icon(_tray_state())
    except Exception as e:
        import traceback
        log(f"Recording error: {e}")
        log(traceback.format_exc())
        if gen == rec_gen[0]:
            recording[0] = False
            overlay.root.after(0, overlay.on_recording_stopped)


# ── System tray ──────────────────────────────────────────────────────────────

tray_icon = [None]

def build_tray_menu():
    items = []
    for name, size in ALL_MODELS:
        downloaded = is_model_downloaded(name)

        # \t right-aligns size in Windows native menus
        if downloaded:
            label = f"{name}\t{size}"
        else:
            label = f"\u2193 {name}\t{size}"

        def make_act(n):
            def act(icon, item):
                if not recording[0]:
                    threading.Thread(target=lambda: switch_model(n), daemon=True).start()
            return act

        def make_checked(n):
            return lambda item: n == current_model_name[0]

        items.append(pystray.MenuItem(label, make_act(name), checked=make_checked(name)))

    return pystray.Menu(
        pystray.MenuItem("WhisperType", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Model", pystray.Menu(*items)),
        pystray.MenuItem(
            "Download all models",
            on_download_all,
            enabled=lambda item: (not _downloading_all[0]
                                  and any(not is_model_downloaded(n) for n, _ in ALL_MODELS)),
        ),
        pystray.Menu.SEPARATOR,
        # Space only reaches the overlay when WhisperType already owns the
        # keyboard, so history needs a route that always works.
        pystray.MenuItem("Show history", on_show_history),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Benchmark next recording",
            on_toggle_benchmark,
            checked=lambda item: benchmark_next[0],
        ),
        pystray.MenuItem(
            "Open last benchmark",
            on_open_last_benchmark,
            enabled=lambda item: last_benchmark_job[0] is not None,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_tray_exit),
    )


def on_toggle_benchmark(icon, item):
    benchmark_next[0] = not benchmark_next[0]
    log(f"Benchmark mode {'ARMED' if benchmark_next[0] else 'disarmed'}")
    refresh_tray()


def on_open_last_benchmark(icon, item):
    job = last_benchmark_job[0]
    if job and overlay:
        overlay.root.after(0, lambda: overlay.show_benchmark(job))


def on_download_all(icon, item):
    threading.Thread(target=_ensure_all_models_downloaded, daemon=True).start()


def on_show_history(icon, item):
    if not overlay:
        return
    def _open():
        # toggle_history() refuses to run while the benchmark panel owns the
        # overlay, and _repack() draws benchmark ahead of history — so the
        # panel has to be dismissed first or this item silently does nothing.
        overlay._benchmark_mode = False
        overlay._benchmark_view_job = None
        if not overlay._history_mode:
            overlay.toggle_history()
        overlay._show_overlay()
    overlay.root.after(0, _open)


def switch_model(name):
    # qsize() alone is not enough: the job being transcribed right now has
    # already been get()'d off the queue, so it is invisible here.
    with jobs_lock:
        busy = len(active_jobs) > 0
    if busy or transcription_queue.qsize() > 0 or current_benchmark_job[0] is not None:
        log("Cannot switch model while transcriptions are in flight")
        return
    if model_switching[0]:
        log("Model switch already in progress")
        return
    model_switching[0] = True
    previous = current_model_name[0]
    try:
        if not is_model_downloaded(name):
            log(f"Downloading {name}...")
            if tray_icon[0]:
                tray_icon[0].icon = make_tray_icon("downloading")
        wmodel[0] = load_model(name)
        # Only record the switch once the model actually loaded — otherwise a
        # failed download would persist a broken model into config.json.
        current_model_name[0] = name
        save_last_model(name)
        log(f"Switched to {name}.")
    except Exception as e:
        log(f"Failed to load {name}: {e} — staying on {previous}")
    finally:
        model_switching[0] = False
        refresh_tray()  # rebuilds menu (removes ↓ from newly downloaded) + resets icon


def _tray_state():
    """Icon state that matches what the daemon is actually doing."""
    if model_switching[0] or _downloading_all[0]:
        return "downloading" if _downloading_all[0] else "transcribing"
    if recording[0]:
        return "recording"
    with jobs_lock:
        busy = len(active_jobs) > 0
    return "transcribing" if (busy or transcription_queue.qsize() > 0) else "idle"


def refresh_tray():
    if tray_icon[0]:
        tray_icon[0].menu = build_tray_menu()
        tray_icon[0].icon = make_tray_icon(_tray_state())

shutting_down = [False]

def _do_exit():
    shutting_down[0] = True
    # Shutdown is the one case where the audio really is dropped: history is
    # in-memory and dies with the process, and blocking the quit on a
    # transcription that then types into a window you just closed is worse.
    if recording[0]:
        log("Exit during recording — audio discarded (nowhere for the text to go)")
    _stop_current_recording(discard=True)
    if tray_icon[0]:
        tray_icon[0].stop()
    # Let active transcriptions finish (save to history, skip auto_type)
    def _wait_and_destroy():
        transcription_queue.join()  # wait for worker to finish current jobs
        overlay.root.after(0, overlay.root.destroy)
    with jobs_lock:
        busy = any(j.status == JobStatus.TRANSCRIBING for j in active_jobs)
    if transcription_queue.qsize() > 0 or busy:
        overlay.root.after(0, overlay.hide)
        threading.Thread(target=_wait_and_destroy, daemon=True).start()
    else:
        overlay.root.after(0, overlay.root.destroy)

def on_tray_exit(icon, item):
    _do_exit()

def run_tray():
    icon = pystray.Icon("WhisperType", make_tray_icon("idle"),
                        "WhisperType", menu=build_tray_menu())
    tray_icon[0] = icon
    icon.run()


# ── Start ────────────────────────────────────────────────────────────────────

def init_model():
    name = current_model_name[0]
    try:
        wmodel[0] = load_model(name)
    except Exception as e:
        log(f"CRITICAL: could not load {name}: {e}")
        log("Dictation is disabled — pick another model from the tray menu.")
        refresh_tray()
        return
    refresh_tray()
    log("Ready.")

overlay = RecordingOverlay()

threading.Thread(target=init_model, daemon=True).start()
threading.Thread(target=run_tray, daemon=True).start()
threading.Thread(target=_transcription_worker, daemon=True).start()

listener = pynput.keyboard.Listener(on_press=on_press, on_release=on_release)
listener.start()

log(f"PTT={cfg.get('push_to_talk_key')} (double-tap) | Model={current_model_name[0]}")
overlay.run()

listener.stop()

# The recorder thread can still be blocked in stream.read() for up to a chunk
# period plus PyAudio's stop/close. Pa_Terminate() frees the stream objects, so
# tearing PyAudio down underneath a live read is a use-after-free.
_t = rec_thread[0]
if _t is not None and _t.is_alive():
    _t.join(timeout=2.0)

if _t is not None and _t.is_alive():
    log("Recorder thread still running — skipping PyAudio teardown to avoid a crash on exit")
else:
    try:
        _pa.terminate()
    except Exception as e:
        log(f"PyAudio terminate failed: {e}")

_stopping[0] = True   # tell the GPU collector to stop calling into NVML
if _nvml_ok:
    time.sleep(0.05)
    try:
        pynvml.nvmlShutdown()
    except Exception as e:
        log(f"NVML shutdown failed: {e}")
log("Daemon stopped.")
_log_f.close()
