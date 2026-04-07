"""
WhisperType — Push-to-talk voice dictation for Windows.

- Double-tap R-Ctrl: start recording
- Single R-Ctrl during recording: stop recording
- 3s silence also stops recording
- System tray icon with model switching
- Recording overlay with audio levels + timer
"""
import sys
import os
import time
import threading
import tkinter as tk
from pathlib import Path

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
import tempfile
import wave
import ctypes
import ctypes.wintypes as wt
from PIL import Image, ImageDraw
import pystray

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
MODELS           = cfg.get("models", ["large-v3-turbo", "large-v3"])
DOUBLE_TAP_MS    = 400   # max ms between two taps

current_model_idx = [0]


# ── Tray icon ────────────────────────────────────────────────────────────────

def make_tray_icon(state="idle"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg, fg = {"idle": ("#1e293b", "#6ee7b7"),
              "recording": ("#1e293b", "#f87171"),
              "transcribing": ("#1e293b", "#fbbf24")}.get(state, ("#1e293b", "#6ee7b7"))
    d.ellipse([2, 2, 62, 62], fill=bg)
    d.rounded_rectangle([22, 10, 42, 38], radius=10, fill=fg)
    d.arc([14, 26, 50, 50], 0, 180, fill=fg, width=4)
    d.line([32, 50, 32, 58], fill=fg, width=4)
    d.line([24, 58, 40, 58], fill=fg, width=4)
    return img


# ── Overlay (NO withdraw/deiconify — alpha-only visibility) ──────────────────

OFF_SCREEN = "-9999+-9999"

class RecordingOverlay:
    C = {"bg": "#0f172a", "rec": "#f87171", "trans": "#fbbf24", "text": "#f1f5f9",
         "dim": "#64748b", "bar_bg": "#1e293b",
         "bar_lo": "#6ee7b7", "bar_mid": "#fbbf24", "bar_hi": "#f87171"}

    def __init__(self):
        self.root = tk.Tk()
        # Prevent ANY flash: set alpha=0 first, overrideredirect, then geometry
        self.root.attributes("-alpha", 0.0)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.title("VR")
        self.root.configure(bg=self.C["bg"])
        self.root.resizable(False, False)
        self.root.geometry(OFF_SCREEN)   # start off-screen

        W = 320
        hdr = tk.Frame(self.root, bg=self.C["bg"])
        hdr.pack(fill="x", padx=14, pady=(12, 4))
        self.dot = tk.Label(hdr, text="●", bg=self.C["bg"], fg=self.C["rec"], font=("Segoe UI", 11))
        self.dot.pack(side="left")
        self.state = tk.Label(hdr, text="Recording", bg=self.C["bg"], fg=self.C["text"], font=("Segoe UI", 11, "bold"))
        self.state.pack(side="left", padx=(4, 0))
        self.timer = tk.Label(hdr, text="0:00", bg=self.C["bg"], fg=self.C["dim"], font=("Consolas", 11))
        self.timer.pack(side="right")

        self.model_lbl = tk.Label(self.root, text="", bg=self.C["bg"], fg=self.C["dim"], font=("Segoe UI", 8))
        self.model_lbl.pack(anchor="w", padx=14)

        self.cv = tk.Canvas(self.root, bg=self.C["bg"], width=W - 28, height=24, highlightthickness=0)
        self.cv.pack(padx=14, pady=(6, 2))

        self.hint = tk.Label(self.root, text="", bg=self.C["bg"], fg=self.C["dim"], font=("Segoe UI", 8))
        self.hint.pack(anchor="e", padx=14, pady=(0, 10))

        self.root.geometry(f"{W}x120")
        self.root.update_idletasks()  # force geometry computation
        self.root.geometry(OFF_SCREEN)  # back off-screen

        # Drag
        hdr.bind("<Button-1>",  lambda e: setattr(self, '_d', (e.x, e.y)))
        hdr.bind("<B1-Motion>", self._drag)

        self._sw = self.root.winfo_screenwidth()
        self._timer_job = None
        self._blink_job = None
        self._blink_on = True

    def _drag(self, e):
        dx, dy = self._d
        x = self.root.winfo_x() + e.x - dx
        y = self.root.winfo_y() + e.y - dy
        self.root.geometry(f"+{x}+{y}")

    def show_recording(self):
        self._t0 = time.time()
        self.dot.config(fg=self.C["rec"])
        self.state.config(text="Recording", fg=self.C["text"])
        self.model_lbl.config(text=f"Model: {MODELS[current_model_idx[0]]}")
        self.hint.config(text="R-Ctrl to stop • 3s silence auto-stops")
        self._draw_bar(0)
        # Move on-screen THEN fade in (no flash because alpha is still 0)
        self.root.geometry(f"+{self._sw // 2 - 160}+20")
        self.root.update_idletasks()
        self.root.attributes("-alpha", 0.93)
        self._tick()
        self._blink()

    def show_transcribing(self):
        self._cancel_jobs()
        self.dot.config(fg=self.C["trans"])
        self.state.config(text="Transcribing...", fg=self.C["trans"])
        self.timer.config(text="")
        self.hint.config(text="")
        self._draw_bar(0)

    def hide(self):
        self._cancel_jobs()
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)

    def push_level(self, rms):
        self.root.after(0, lambda: self._draw_bar(rms))

    def _cancel_jobs(self):
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

    def _draw_bar(self, rms, mx=4000):
        c = self.cv; c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, W, H, fill=self.C["bar_bg"], outline="")
        r = min(rms / mx, 1.0)
        fw = int(W * r)
        clr = self.C["bar_lo"] if r < 0.4 else (self.C["bar_mid"] if r < 0.75 else self.C["bar_hi"])
        if fw > 0:
            c.create_rectangle(0, 0, fw, H, fill=clr, outline="")
        # Silence threshold line
        sx = int(W * min(SILENCE_THRESH / mx, 1.0))
        c.create_line(sx, 0, sx, H, fill="#ef4444", width=2)
        for p in (0.25, 0.5, 0.75):
            c.create_line(int(W * p), 0, int(W * p), H, fill="#0f172a")

    def run(self):
        self.root.mainloop()


# ── Audio (single PyAudio instance — no per-recording window flash) ──────────

_pa = pyaudio.PyAudio()   # create ONCE at startup
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
        # NOT calling _pa.terminate() — reuse for next recording
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
    # Convert raw PCM bytes to float32 numpy array directly —
    # bypasses whisper.load_audio() which spawns ffmpeg (causes CMD flash on Windows)
    audio_np = np.frombuffer(audio_bytes, np.int16).astype(np.float32) / 32768.0
    # Whisper expects 16kHz mono float32 — which is exactly what we record
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

def auto_type(text):
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


# ── PTT: double-tap to start, single-tap to stop ────────────────────────────

recording    = [False]
stop_event   = [threading.Event()]
generation   = [0]
last_tap     = [0.0]
overlay      = None


def on_press(key):
    if key != PTT_KEY or wmodel[0] is None:
        return
    now = time.time()

    if recording[0]:
        # Single press during recording → stop
        recording[0] = False
        stop_event[0].set()
        log("Recording stopped by keypress")
    else:
        # Double-tap detection
        if (now - last_tap[0]) * 1000 < DOUBLE_TAP_MS:
            recording[0] = True
            stop_event[0].set()           # cancel any stale transcribe
            stop_event[0] = threading.Event()
            generation[0] += 1
            overlay.root.after(0, overlay.show_recording)
            threading.Thread(target=_record_and_transcribe,
                             args=(generation[0],), daemon=True).start()
            log("Recording started (double-tap)")
        last_tap[0] = now


def on_release(key):
    pass   # no action on release — double-tap model


def _record_and_transcribe(gen):
    try:
        audio = record_until_stop(stop_event[0], level_callback=overlay.push_level)
        if not audio or gen != generation[0]:
            overlay.root.after(0, overlay.hide)
            return

        overlay.root.after(0, overlay.show_transcribing)
        text = transcribe(audio)

        if gen != generation[0]:
            overlay.root.after(0, overlay.hide)
            return

        log(f"Transcribed: {text}")
        overlay.root.after(0, overlay.hide)
        if text:
            auto_type(text)
    except Exception as e:
        log(f"Error: {e}")
        overlay.root.after(0, overlay.hide)


# ── System tray ──────────────────────────────────────────────────────────────

tray_icon = [None]

def build_tray_menu():
    items = []
    for i, name in enumerate(MODELS):
        short = name.replace("large-v3-turbo", "turbo ⚡").replace("large-v3", "large 🎯")
        label = f"✓ {short}" if i == current_model_idx[0] else f"   {short}"
        idx = i
        def make_act(idx):
            def act(icon, item):
                if wmodel[0] and not recording[0]:
                    threading.Thread(target=lambda: switch_model(idx), daemon=True).start()
            return act
        items.append(pystray.MenuItem(label, make_act(idx)))

    return pystray.Menu(
        pystray.MenuItem("WhisperType", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Model", pystray.Menu(*items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_tray_exit),
    )

def switch_model(idx):
    current_model_idx[0] = idx
    name = MODELS[idx]
    log(f"Switching to {name}...")
    wmodel[0] = load_model(name)
    refresh_tray()
    log(f"Switched to {name}.")

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
    wmodel[0] = load_model(MODELS[current_model_idx[0]])
    refresh_tray()
    log("Ready.")

overlay = RecordingOverlay()

threading.Thread(target=init_model, daemon=True).start()
threading.Thread(target=run_tray, daemon=True).start()

listener = pynput.keyboard.Listener(on_press=on_press, on_release=on_release)
listener.start()

log(f"PTT={cfg.get('push_to_talk_key')} (double-tap) | Models={MODELS}")
overlay.run()

listener.stop()
log("Daemon stopped.")
_log_f.close()
