# WhisperType

**Push-to-talk voice dictation for Windows — powered by OpenAI Whisper, runs 100% locally on your GPU.**

No cloud. No API keys. No subscriptions. Your voice never leaves your machine.

---

## How It Works

1. **Double-tap Right Ctrl** to start recording
2. **Speak** — you'll see a live recording overlay with audio levels
3. **Tap Right Ctrl once** to stop (or wait 3 seconds of silence)
4. The transcribed text is **typed into whichever window was active** when you stopped recording

While one recording is being transcribed, you can already start recording the next one. WhisperType queues them and processes them in order, each going to the correct target window.

---

## Features

| Feature | Description |
|---------|-------------|
| **Push-to-talk** | Double-tap Right Ctrl to record, single tap to stop |
| **Silence detection** | Stops automatically after 3 seconds of silence |
| **Types anywhere** | Injects text via Win32 SendInput — works in any app, no clipboard |
| **Transcription queue** | Record your next message while the previous one transcribes |
| **Window targeting** | Text goes to the window that was focused when you stopped recording |
| **GPU accelerated** | CUDA-powered Whisper for near-instant transcription |
| **GPU monitor** | Real-time GPU utilization graph (Task Manager style, last 60 seconds) |
| **Model switching** | Switch Whisper models from the system tray without restarting |
| **100% offline** | Everything runs locally — nothing is sent anywhere |
| **Auto-start** | Starts with Windows via Startup folder shortcut |

---

## Overlay

The floating overlay appears during recording and while transcriptions are queued:

```
+──────────────────────────────────+
| GPU  ▓▓▓▓▓▒▒░░           67%   |   GPU utilization graph (last 60s)
+──────────────────────────────────+
| ● Recording          0:42      |   Live recording indicator
| Model: large-v3-turbo          |
| [████████░░░░░░░░░|░░░░░░░░░░] |   Audio level meter + silence line
| R-Ctrl stop · 3s silence       |
+──────────────────────────────────+
| Transcribe queue                |
| ▶ 10:04:21  1.2s  Notepad++..  |   Currently transcribing (gold)
| ○ 10:04:35  0.8s  VS Code...   |   Waiting (gray)
+──────────────────────────────────+
```

- **GPU graph** collects data in the background, so it's ready instantly when the overlay appears
- **Queue items** show timestamp, audio duration, and target window name
- The overlay disappears automatically when all transcriptions are done and no recording is active
- Drag the overlay by its header to reposition

---

## Installation

### Prerequisites

- **Windows 10 or 11**
- **Python 3.10+** — [Download here](https://python.org). Check **"Add python.exe to PATH"** during installation!
- **NVIDIA GPU recommended** — any CUDA-capable GPU. CPU works but is significantly slower.
- **Microphone**

### One-click install

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
install.bat
```

The installer handles everything:
1. Creates a Python virtual environment
2. Installs PyTorch with CUDA support (falls back to CPU)
3. Installs all dependencies
4. Downloads the Whisper model (~1.5 GB, one-time)
5. Creates your config file
6. Adds a Windows Startup shortcut

### Manual install

```bash
python -m venv .venv
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -c "import whisper; whisper.load_model('large-v3-turbo', device='cpu')"
mkdir %USERPROFILE%\.whispertype
copy config.template.json %USERPROFILE%\.whispertype\config.json
```

---

## Usage

| Action | How |
|--------|-----|
| Start recording | Double-tap **Right Ctrl** (within 400ms) |
| Stop recording | Single **Right Ctrl** during recording |
| Auto-stop | 3 seconds of silence (configurable) |
| Switch model | Right-click tray icon > Model |
| Exit | Right-click tray icon > Exit |
| Launch | Double-click `start.bat` or let it auto-start with Windows |
| Debug | Run `start_debug.bat` to see console output |

### Transcription Queue

You don't have to wait for one transcription to finish before recording the next:

1. Record message A (stops, enters queue)
2. While A transcribes, record message B (stops, enters queue)
3. A finishes, text is typed into its target window
4. B finishes, text is typed into its target window

Each recording remembers which window was active when you stopped, so text always goes to the right place — even if you switch windows during transcription.

---

## Configuration

Edit `%USERPROFILE%\.whispertype\config.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `push_to_talk_key` | `"ctrl_r"` | Hotkey. Options: `ctrl_r`, `ctrl_l`, `shift_r`, `shift_l`, `alt_r`, `alt_l` |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, etc.) |
| `models` | `["large-v3-turbo", "large-v3"]` | Available models (switchable from tray) |
| `silence_threshold` | `200` | Audio level below this = silence (0-32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop |
| `max_recording_time` | `300.0` | Maximum recording length in seconds |
| `sample_rate` | `16000` | Audio sample rate in Hz (Whisper expects 16000) |
| `chunk_size` | `1024` | Audio buffer size. Don't change unless you have issues |

### Available Whisper Models

| Model | Size | Speed | Accuracy | GPU VRAM |
|-------|------|-------|----------|----------|
| `large-v3-turbo` | ~800 MB | Fast | Very good | ~2 GB |
| `large-v3` | ~1.5 GB | Slow | Best | ~5 GB |

For daily use, **`large-v3-turbo`** is recommended — it's much faster with minimal quality loss.

---

## Design Decisions

| Decision | Why |
|----------|-----|
| **No ffmpeg subprocess** | Whisper's `load_audio()` shells out to ffmpeg, which flashes a CMD window on Windows. We bypass it entirely: raw PCM bytes > numpy > mel spectrogram > `whisper.decode()`. |
| **No clipboard** | Uses Win32 `SendInput` with `KEYEVENTF_UNICODE` to simulate keystrokes. Never touches your clipboard. |
| **Single PyAudio instance** | Creating/destroying PyAudio per recording flashes a console window on Windows. One instance lives for the entire process. |
| **Background GPU collector** | GPU utilization is sampled every second in a background thread, so the graph has data ready the moment the overlay appears. |
| **Double-tap activation** | Prevents accidental triggers — a single keypress does nothing. Two taps within 400ms required. |
| **Window capture at stop** | The target window HWND is captured when recording stops, not when transcription finishes. This lets you switch windows freely during transcription. |

---

## Requirements

- **Windows 10 or 11**
- **Python 3.10+**
- **NVIDIA GPU recommended** (any CUDA-capable GPU; CPU works but is slower)
- **Microphone**

### Python Dependencies

See [requirements.txt](requirements.txt):
- `openai-whisper` — speech recognition
- `torch` — GPU acceleration (CUDA)
- `pyaudio` — microphone recording
- `pynput` — keyboard listener
- `pystray` / `Pillow` — system tray icon
- `nvidia-ml-py` — GPU monitoring (optional)

---

## Debugging

If something isn't working:

1. Run `start_debug.bat` — this opens a console window with live output
2. Check `voice_daemon.log` in the WhisperType directory
3. Common issues:
   - **"Python not found"** — Install Python 3.10+ and check "Add to PATH"
   - **No CUDA** — Run `python -c "import torch; print(torch.cuda.is_available())"`. If `False`, reinstall PyTorch with CUDA: `pip install torch --index-url https://download.pytorch.org/whl/cu124`
   - **No microphone** — Check Windows sound settings, make sure a mic is set as default input
   - **Text not appearing** — Some apps block SendInput. Try a different app (Notepad) to verify it works

---

## Files

```
WhisperType/
  whispertype.pyw      Main application (runs as background daemon)
  install.bat          One-click installer
  start.bat            Launch script
  start_debug.bat      Launch with console output for debugging
  config.template.json Default configuration template
  requirements.txt     Python dependencies
  voice_daemon.log     Runtime log (created on launch)
```

---

## License

MIT
