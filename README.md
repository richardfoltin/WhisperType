# WhisperType

**Push-to-talk voice dictation for Windows — powered by OpenAI Whisper, runs 100% locally on your GPU.**

No cloud. No API keys. No subscriptions. Your voice never leaves your machine.

---

## Quick Start

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
install.bat
```

That's it. The installer sets up everything: Python environment, CUDA-accelerated PyTorch, Whisper model download (~1.5 GB), config file, and a Windows Startup shortcut so it launches automatically on boot.

After installation, run `start.bat` or just restart your PC — WhisperType will be waiting in the system tray.

---

## How to Use

| Action | Key |
|--------|-----|
| Start recording | **Double-tap Right Ctrl** (within 400ms) |
| Stop recording | **Right Ctrl** (single tap) |
| Auto-stop | 3 seconds of silence |
| Open history | **Space** (while overlay is visible) |
| Resume recording | **Space** (while in history) |
| Quit | **Esc** (while overlay is visible) |
| Switch model | Right-click tray icon > Model |
| Exit | Right-click tray icon > Exit |

1. Double-tap **Right Ctrl** to start recording — a floating overlay appears
2. Speak naturally
3. Tap **Right Ctrl** once to stop (or just pause — silence auto-stops after 3 seconds)
4. The transcribed text is typed into whichever window was active when you stopped recording

Text is injected directly via keystrokes — no clipboard involved, works in any app.

---

## Transcription Queue

You don't have to wait for one transcription to finish before recording the next:

1. Record message A → stops, enters queue
2. While A transcribes, record message B → stops, enters queue
3. A finishes → text typed into its target window
4. B finishes → text typed into its target window

Each recording remembers which window was active when you stopped, so text always goes to the right place even if you switch windows during transcription.

---

## Overlay

The floating overlay appears during recording and while transcriptions are queued:

```
+--------------------------------------+
| WhisperType                       x  |  Drag to reposition / x to quit
+--------------------------------------+
| GPU  ||||||||......          67%     |  GPU utilization graph (last 60s)
+--------------------------------------+
| * Recording              0:42       |  Live recording status
| Model: large-v3-turbo               |
| [========...........|..............]  |  Audio level + silence threshold
| Transcribe: R-Ctrl / 3s | Space ... |  Context-aware keyboard hints
+--------------------------------------+
| TIME      DUR   APP      WINDOW     |  Transcription queue
| 10:04:21  1.2s  Code     server.ts  |  Currently transcribing (gold)
| 10:04:35  0.8s  Chrome   ChatGPT..  |  Waiting (gray)
+--------------------------------------+
```

The overlay shows different states:

- **Recording** — red blinking dot, audio level meter, timer
- **Transcribing** — yellow dot, queue with progress
- **History** — green dot, last 10 transcriptions with copy/delete buttons

The GPU graph collects data in the background from startup, so it's ready instantly when the overlay appears.

### History

Press **Space** while the overlay is visible to open history — a list of your last 10 transcriptions. Each entry shows the timestamp, duration, source app, window name, and has:

- **Copy button** — copies the transcription to clipboard (hover to preview the text)
- **Delete button** — removes the entry from history

Press **Space** again to close history and start a new recording. If you were recording when you pressed Space, that recording is discarded.

---

## Model Switching

Right-click the tray icon and go to **Model** to see all available Whisper models:

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| `large-v3-turbo` | ~809 MB | Fast | Very good |
| `large-v3` | ~1.5 GB | Slow | Best |
| `large-v2` | ~1.5 GB | Slow | Best |
| `medium` | ~769 MB | Medium | Good |
| `small` | ~244 MB | Fast | Decent |
| `base` | ~74 MB | Very fast | Basic |
| `tiny` | ~39 MB | Instant | Basic |

- A checkmark shows the active model
- Models not yet downloaded show a down arrow (click to download)
- The tray icon changes during download
- Your last selected model is remembered across restarts

For daily use, **`large-v3-turbo`** is the best balance of speed and accuracy.

---

## Configuration

Edit `%USERPROFILE%\.whispertype\config.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `push_to_talk_key` | `"ctrl_r"` | Hotkey. Options: `ctrl_r`, `ctrl_l`, `shift_r`, `shift_l`, `alt_r`, `alt_l` |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, `es`, `ja`, etc.) |
| `silence_threshold` | `200` | Audio level below this = silence (0-32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop |
| `max_recording_time` | `300.0` | Maximum recording length in seconds |

---

## Requirements

- **Windows 10 or 11**
- **Python 3.10+** — [python.org](https://python.org) (check "Add to PATH" during install)
- **NVIDIA GPU recommended** — any CUDA-capable GPU; CPU works but is much slower
- **Microphone**

---

## Installation

### One-click (recommended)

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
install.bat
```

The installer:
1. Creates a Python virtual environment
2. Installs PyTorch with CUDA support (falls back to CPU if needed)
3. Installs all dependencies
4. Downloads the default Whisper model (~1.5 GB, one-time)
5. Creates your config file at `%USERPROFILE%\.whispertype\config.json`
6. Adds a Windows Startup shortcut (auto-start on boot)

### Manual

```bash
python -m venv .venv
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -c "import whisper; whisper.load_model('large-v3-turbo', device='cpu')"
mkdir %USERPROFILE%\.whispertype
copy config.template.json %USERPROFILE%\.whispertype\config.json
```

---

## Troubleshooting

Run `start_debug.bat` to see console output. Check `voice_daemon.log` for errors.

| Problem | Solution |
|---------|----------|
| "Python not found" | Install Python 3.10+ and check "Add to PATH" |
| No CUDA / slow | Run `python -c "import torch; print(torch.cuda.is_available())"` — if `False`, reinstall PyTorch: `pip install torch --index-url https://download.pytorch.org/whl/cu124` |
| No microphone | Check Windows Sound Settings, make sure a mic is set as default input |
| Text not appearing | Some apps block simulated keystrokes. Try Notepad first to verify it works |
| Console window flash | Use `start_silent.vbs` instead of `start.bat` (the installer's startup shortcut already does this) |

---

## Files

```
WhisperType/
  whispertype.pyw      Main application (runs as background daemon)
  install.bat          One-click installer
  start.bat            Launch script (with console window)
  start_silent.vbs     Launch script (no console window, used by startup shortcut)
  start_debug.bat      Launch with console output for debugging
  config.template.json Default configuration template
  requirements.txt     Python dependencies
  voice_daemon.log     Runtime log (created on each launch)
```

---

## License

MIT
