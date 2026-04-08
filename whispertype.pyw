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
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
import queue

# ── Logging ──────────────────────────────────────────────────────────────────
LOG = Path(__file__).parent / "voice_daemon.log"
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
import pynput.keyboard
import ctypes
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

def _gpu_background_collector():
    while True:
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

CONFIG_PATH = Path.home() / ".whispertype" / "config.json"
with open(CONFIG_PATH) as f:
    cfg = json.load(f)

KEY_MAP = {
    "ctrl_r":  pynput.keyboard.Key.ctrl_r,
    "ctrl_l":  pynput.keyboard.Key.ctrl_l,
    "shift_r": pynput.keyboard.Key.shift_r,
    "shift_l": pynput.keyboard.Key.shift_l,
    "alt_r":   pynput.keyboard.Key.alt_r,
    "alt_l":   pynput.keyboard.Key.alt_l,
}
PTT_KEY          = KEY_MAP.get(cfg.get("push_to_talk_key", "ctrl_r"), pynput.keyboard.Key.ctrl_r)
LANGUAGE         = cfg.get("language", "hu")
RATE             = cfg.get("sample_rate", 16000)
CHUNK            = cfg.get("chunk_size", 1024)
SILENCE_THRESH   = cfg.get("silence_threshold", 100)
SILENCE_SECS     = cfg.get("silence_duration", 3.0)
MAX_RECORD_SECS  = cfg.get("max_recording_time", 300.0)
DOUBLE_TAP_MS    = 400

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

# Last used model from config, fallback to large-v3-turbo
current_model_name = [cfg.get("last_model", "large-v3-turbo")]

def save_last_model(name):
    cfg["last_model"] = name
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
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

transcription_queue = queue.Queue()
active_jobs = []
jobs_lock = threading.Lock()
job_counter = [0]


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

        # ── 1. GPU graph section (TOP) ──
        self.gpu_frame = tk.Frame(self.root, bg=self.C["bg"])
        gpu_hdr = tk.Frame(self.gpu_frame, bg=self.C["bg"])
        gpu_hdr.pack(fill="x", padx=14, pady=(8, 2))
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

        self.model_lbl = tk.Label(self.rec_frame, text="", bg=self.C["bg"],
                                  fg=self.C["dim"], font=("Segoe UI", 8))
        self.model_lbl.pack(anchor="w", padx=14)

        self.level_cv = tk.Canvas(self.rec_frame, bg=self.C["bg"], width=cw,
                                  height=24, highlightthickness=0)
        self.level_cv.pack(padx=14, pady=(4, 2))

        self.hint = tk.Label(self.rec_frame, text="", bg=self.C["bg"],
                             fg=self.C["dim"], font=("Segoe UI", 8))
        self.hint.pack(anchor="e", padx=14, pady=(0, 4))

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

        # ── Init state ──
        self.root.geometry(f"{OV_W}x120")
        self.root.update_idletasks()
        self.root.geometry(OFF_SCREEN)

        self._sw = self.root.winfo_screenwidth()
        self._timer_job = None
        self._blink_job = None
        self._gpu_refresh_job = None
        self._blink_on = True
        self._recording = False
        self._visible = False
        self._pos = None

    # ── Layout helpers ──

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
        if _nvml_ok:
            self.gpu_frame.pack(fill="x")
        # Recording section always packed — shows idle state when not recording
        self.rec_frame.pack(fill="x")
        with jobs_lock:
            has_jobs = len(active_jobs) > 0
        if has_jobs:
            self.queue_frame.pack(fill="x")

    def _calc_height(self):
        h = 0
        if _nvml_ok:
            h += 66
        h += 130  # recording section always present
        with jobs_lock:
            n = len(active_jobs)
        if n > 0:
            h += 34 + min(n, MAX_QUEUE_VISIBLE) * 22
            if n > MAX_QUEUE_VISIBLE:
                h += 18
        return max(h, 50)

    def _show_overlay(self):
        self._repack()
        self._rebuild_queue()
        h = self._calc_height()
        pos = self._get_pos()
        self.root.geometry(f"{OV_W}x{h}{pos}")
        self.root.update_idletasks()
        self.root.attributes("-alpha", 0.93)
        self._visible = True
        self._start_gpu_refresh()

    # ── Public methods ──

    def show_recording(self):
        self._recording = True
        self._t0 = time.time()
        self.dot.config(fg=self.C["rec"])
        self.state_lbl.config(text="Recording", fg=self.C["text"])
        self.model_lbl.config(text=f"Model: {current_model_name[0]}")
        self.hint.config(text="R-Ctrl stop \u2022 3s silence auto-stops")
        self._draw_level(0)
        self._show_overlay()
        self._tick()
        self._blink()

    def _show_rec_idle(self):
        self._cancel_timer_blink()
        self.dot.config(fg=self.C["dim"])
        self.state_lbl.config(text="Ready", fg=self.C["dim"])
        self.model_lbl.config(text=f"Model: {current_model_name[0]}")
        self.timer.config(text="")
        self.hint.config(text="Double-tap R-Ctrl to record")
        self._draw_level(0)

    def on_recording_stopped(self):
        self._recording = False
        self._cancel_timer_blink()
        with jobs_lock:
            has_jobs = len(active_jobs) > 0
        if has_jobs:
            self._show_rec_idle()
            self._show_overlay()
        else:
            self.hide()

    def refresh(self):
        if self._recording or self._has_jobs():
            if not self._recording:
                self._show_rec_idle()
            self._show_overlay()
        else:
            self.hide()

    def check_hide(self):
        if not self._has_jobs() and not self._recording:
            self.hide()

    def hide(self):
        self._cancel_timer_blink()
        self._stop_gpu_refresh()
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)
        self._visible = False

    def push_level(self, rms):
        self.root.after(0, lambda: self._draw_level(rms))

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
            color = self.C["trans"] if job.status == JobStatus.TRANSCRIBING else self.C["dim"]
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
        with jobs_lock:
            if job in active_jobs:
                active_jobs.remove(job)
        log(f"Cancelled job {job.job_id}")
        self.refresh()

    def _cancel_timer_blink(self):
        for j in (self._timer_job, self._blink_job):
            if j:
                self.root.after_cancel(j)
        self._timer_job = self._blink_job = None

    def _tick(self):
        e = time.time() - self._t0
        self.timer.config(text=f"{int(e)//60}:{int(e)%60:02d}")
        self._timer_job = self.root.after(500, self._tick)

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

wmodel = [None]

def load_model(name):
    log(f"Loading {name} on {DEVICE}...")
    m = whisper.load_model(name, device=DEVICE)
    log(f"{name} ready on {DEVICE}.")
    return m

def transcribe(audio_bytes):
    audio_np = np.frombuffer(audio_bytes, np.int16).astype(np.float32) / 32768.0
    audio_padded = whisper.pad_or_trim(audio_np)
    mel = whisper.log_mel_spectrogram(audio_padded, n_mels=128).to(DEVICE)
    options = whisper.DecodingOptions(language=LANGUAGE, fp16=(DEVICE == "cuda"))
    result = whisper.decode(wmodel[0], mel, options)
    return result.text.strip()


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

def get_foreground_window():
    return ctypes.windll.user32.GetForegroundWindow()

def set_foreground_window(hwnd):
    ctypes.windll.user32.SetForegroundWindow(hwnd)

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

def auto_type(text, target_hwnd=None):
    if target_hwnd:
        try:
            set_foreground_window(target_hwnd)
        except Exception:
            pass
    time.sleep(0.2)
    events = []
    for ch in text:
        code = ord(ch)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki.wVk = 0
            inp.ki.wScan = code
            inp.ki.dwFlags = flags
            inp.ki.time = 0
            inp.ki.dwExtraInfo = _extra
            events.append(inp)
    n = len(events)
    ctypes.windll.user32.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))


# ── Transcription worker (queue consumer) ───────────────────────────────────

def _transcription_worker():
    while True:
        job = transcription_queue.get()
        try:
            job.status = JobStatus.TRANSCRIBING
            overlay.root.after(0, overlay.refresh)

            if tray_icon[0]:
                tray_icon[0].icon = make_tray_icon("transcribing")

            text = transcribe(job.audio_bytes)
            log(f"Transcribed (job {job.job_id}, target={job.window_name}): {text}")

            if text:
                auto_type(text, target_hwnd=job.target_hwnd)
        except Exception as e:
            log(f"Transcription error (job {job.job_id}): {e}")
        finally:
            with jobs_lock:
                if job in active_jobs:
                    active_jobs.remove(job)
            transcription_queue.task_done()
            overlay.root.after(0, overlay.refresh)
            overlay.root.after(100, overlay.check_hide)

            if transcription_queue.qsize() == 0 and not recording[0]:
                if tray_icon[0]:
                    tray_icon[0].icon = make_tray_icon("idle")


# ── PTT: double-tap to start, single-tap to stop ────────────────────────────

recording      = [False]
model_switching = [False]
stop_event     = [threading.Event()]
last_tap       = [0.0]
overlay        = None


def on_press(key):
    if key != PTT_KEY or wmodel[0] is None or model_switching[0]:
        return
    now = time.time()

    if recording[0]:
        recording[0] = False
        stop_event[0].set()
        log("Recording stopped by keypress")
    else:
        if (now - last_tap[0]) * 1000 < DOUBLE_TAP_MS:
            recording[0] = True
            stop_event[0] = threading.Event()
            overlay.root.after(0, overlay.show_recording)
            threading.Thread(target=_record_and_enqueue, daemon=True).start()
            log("Recording started (double-tap)")
        last_tap[0] = now


def on_release(key):
    pass


def _record_and_enqueue():
    try:
        audio = record_until_stop(stop_event[0], level_callback=overlay.push_level)
        target_hwnd = get_foreground_window()
        window_name = get_window_text(target_hwnd)
        recording[0] = False

        if not audio:
            overlay.root.after(0, overlay.on_recording_stopped)
            return

        duration = len(audio) / (RATE * 2)  # 16-bit PCM = 2 bytes per sample
        app_name = get_process_name(target_hwnd)
        job_counter[0] += 1
        job = TranscriptionJob(
            job_id=job_counter[0],
            audio_bytes=audio,
            target_hwnd=target_hwnd,
            window_name=window_name,
            app_name=app_name,
            audio_duration=duration,
        )

        with jobs_lock:
            active_jobs.append(job)

        # Schedule overlay update BEFORE putting in queue to avoid race
        # (worker might finish and clear job before mainloop processes on_recording_stopped)
        overlay.root.after(0, overlay.on_recording_stopped)
        transcription_queue.put(job)
        log(f"Enqueued job {job.job_id} for '{window_name}' ({duration:.1f}s)")

        if tray_icon[0]:
            tray_icon[0].icon = make_tray_icon("transcribing")
    except Exception as e:
        log(f"Recording error: {e}")
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
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_tray_exit),
    )

def switch_model(name):
    if transcription_queue.qsize() > 0:
        log("Cannot switch model while queue has items")
        return
    model_switching[0] = True
    current_model_name[0] = name
    save_last_model(name)
    try:
        need_download = not is_model_downloaded(name)
        if need_download:
            log(f"Downloading {name}...")
            if tray_icon[0]:
                tray_icon[0].icon = make_tray_icon("downloading")
        log(f"Loading {name} on {DEVICE}...")
        wmodel[0] = whisper.load_model(name, device=DEVICE)
        log(f"Switched to {name}.")
    except Exception as e:
        log(f"Failed to load {name}: {e}")
    finally:
        model_switching[0] = False
        refresh_tray()  # rebuilds menu (removes ↓ from newly downloaded) + resets icon

def refresh_tray():
    if tray_icon[0]:
        tray_icon[0].menu = build_tray_menu()
        tray_icon[0].icon = make_tray_icon("idle")

def on_tray_exit(icon, item):
    icon.stop()
    overlay.root.after(0, overlay.root.destroy)

def run_tray():
    icon = pystray.Icon("WhisperType", make_tray_icon("idle"),
                        "WhisperType", menu=build_tray_menu())
    tray_icon[0] = icon
    icon.run()


# ── Start ────────────────────────────────────────────────────────────────────

def init_model():
    name = current_model_name[0]
    log(f"Loading {name} on {DEVICE}...")
    wmodel[0] = whisper.load_model(name, device=DEVICE)
    log(f"{name} ready on {DEVICE}.")
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
log("Daemon stopped.")
_log_f.close()
