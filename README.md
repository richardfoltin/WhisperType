# WhisperType

**Push-to-talk voice dictation for Windows and macOS — powered by OpenAI Whisper, runs 100% locally on your GPU.**

No cloud. No API keys. No subscriptions. Your voice never leaves your machine.

| | Windows | macOS (Apple Silicon) |
|---|---|---|
| Engine | openai-whisper on CUDA | mlx-whisper on Metal |
| Hotkey | Right Ctrl (double-tap) | Right Command (double-tap) |
| Lives in | System tray | Menu bar |
| Autostart | Startup shortcut | LaunchAgent |

---

## Quick Start

### Windows

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
install.bat
```

That's it. The installer sets up everything: Python environment, CUDA-accelerated PyTorch, Whisper model download (~1.5 GB), config file, and a Windows Startup shortcut so it launches automatically on boot.

After installation, run `start.bat` or just restart your PC — WhisperType will be waiting in the system tray.

### macOS

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
./install_mac.sh
```

The installer creates a virtualenv in `~/.whispertype/venv`, installs the MLX stack, downloads the model, builds a small `WhisperType.app` into `~/Applications`, and registers a LaunchAgent so it starts at login.

**Then grant three permissions** in System Settings ▸ Privacy & Security — WhisperType cannot work without them, and macOS gives no error when they are missing:

| Permission | Why |
|---|---|
| Microphone | recording (prompted automatically) |
| Accessibility | typing the transcript into other apps |
| Input Monitoring | the push-to-talk hotkey |

Missing grants are written to `voice_daemon.log` on every startup.

To update, pull and re-run the installer — it is safe to run any time:

```bash
git pull && ./install_mac.sh
```

To remove everything it created:

```bash
./uninstall_mac.sh
```

---

## How to Use

| Action | Key |
|--------|-----|
| Start recording | **Double-tap Right Ctrl** (within 400ms) |
| Stop recording | **Right Ctrl** (single tap) |
| Auto-stop | 3 seconds of silence |
| Open history | **Space** (while recording), or the menu bar |
| Leave history | **Space** |
| Hide overlay | **Esc** |
| Switch model | Tray / menu bar ▸ Model |
| Exit | Tray / menu bar ▸ Exit |

Space and Esc are only intercepted while you are actually recording or in history — they behave normally in your other apps the rest of the time, including while a transcription is still running.

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

## The menu bar (macOS)

Everything you need day to day is in the menu-bar menu, so you never have to
edit JSON for the common cases:

| Item | Notes |
|---|---|
| **History…** | Opens the history list without starting a recording |
| **Pause dictation** | Ignores the hotkey until you switch it back; the icon shows a struck-through mic |
| **Model** | Runtime switch. `↓` means not downloaded yet |
| **Language** | Takes effect on the very next dictation, no restart |
| **Microphone** | Same. Pin the built-in mic here so recording does not drop AirPods into call quality |
| **Open Log** | Opens `voice_daemon.log` in Console |
| **Restart WhisperType** | Needed only after changing the hotkey |

The icon colour is the app's status at a glance: green ready, red recording,
amber transcribing, grey downloading or loading, **red exclamation mark** when
something failed, struck-through when paused.

## When something goes wrong

A transcript that cannot be delivered is never silently dropped. The overlay
stays on screen with the reason, the menu-bar icon turns into a red exclamation
mark, and the text is kept in history so you can copy it. Press **Esc** to
dismiss. This covers: the target app quit, a password field took secure input,
the Accessibility permission was revoked, the microphone was unavailable, the
model failed to load, or transcription itself threw.

History is written to `~/.whispertype/history.json`, so it survives quitting and
logging out — which matters precisely because it is where undeliverable text
ends up.

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

Edit `%USERPROFILE%\.whispertype\config.json` (Windows) or `~/.whispertype/config.json` (macOS):

| Key | Default | Description |
|-----|---------|-------------|
| `push_to_talk_key` | `"ctrl_r"` / `"cmd_r"` | Hotkey. Options: `ctrl_r`, `ctrl_l`, `shift_r`, `shift_l`, `alt_r`, `alt_l`, `cmd_r`, `cmd_l`. macOS defaults to `cmd_r` because Apple keyboards have no right Ctrl. |
| `input_device` | *(system default)* | macOS only. Device index, or a substring of its name — e.g. `"MacBook Air Microphone"` to stop recording from switching AirPods into low-quality call mode. |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, `es`, `ja`, etc.) |
| `silence_threshold` | `200` | Audio level below this = silence (0-32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop |
| `max_recording_time` | `300.0` | Maximum recording length in seconds |
| `min_speech_seconds` | `0.25` | A clip with less speech than this is discarded instead of transcribed. Whisper reliably invents a sentence for silence, and an accidental double-tap would otherwise type it into your document. |
| `idle_unload_minutes` | `10` | Release the model after this many idle minutes (`0` = keep it resident). Measured: 1799 MB → 260 MB, and reloading from the local cache takes about a second. |
| `theme` | `"auto"` | Overlay appearance: `auto` follows the system, or pin it with `dark` / `light`. |

Everything except `push_to_talk_key` takes effect on the next dictation — `language` and `input_device` are re-read per job, so the menu-bar submenus change them with no restart at all. The hotkey is read once at startup, so changing it needs:

```bash
launchctl kickstart -k gui/$UID/com.whispertype.agent
```

---

## Requirements

**Windows**

- Windows 10 or 11
- Python 3.10+ — [python.org](https://python.org) (check "Add to PATH" during install)
- NVIDIA GPU recommended — any CUDA-capable GPU; CPU works but is much slower
- Microphone

**macOS**

- macOS 13+ on Apple Silicon (M1 or newer)
- Python 3.11+ from [python.org](https://python.org) — the system `python3` is 3.9 and too old for PyObjC. `install_mac.sh` downloads the installer for you if it is missing.
- Microphone

Intel Macs are not supported: the transcription backend is MLX, which is Apple-Silicon only.

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

### macOS

| Problem | Solution |
|---------|----------|
| Hotkey does nothing | Input Monitoring is not granted. System Settings ▸ Privacy & Security ▸ Input Monitoring ▸ enable WhisperType. |
| Overlay appears but no text is typed | Accessibility is not granted. macOS drops synthetic keystrokes silently — there is no error. Check `voice_daemon.log`. |
| Nothing typed into a password field | Expected. macOS secure input blocks synthetic keystrokes; the transcript stays in history so you can copy it. |
| Permissions keep resetting | Re-run `./install_mac.sh`; the bundle is ad-hoc signed with a fixed identifier so grants survive rebuilds. |
| Music quality drops when recording | Your AirPods are the input device. Set `input_device` in the config to the built-in mic. |
| Stop it running at login | `launchctl bootout gui/$UID/com.whispertype.agent && rm ~/Library/LaunchAgents/com.whispertype.agent.plist` |

---

## Files

```
WhisperType/
  whispertype/              The application
    app.py                  State machine, hotkeys, recording, queue worker
    config.py  jobs.py       Config, transcription queue + history
    audio.py                 Capture (PyAudio on Windows, sounddevice on macOS)
    transcribe.py            Whisper backends (CUDA / MLX)
    backends/windows.py      Win32 SendInput, HWND targeting, NVML
    backends/macos.py        CGEvent typing, NSWorkspace/AX targeting
    backends/mac_gpu.py      Apple GPU utilisation via IOKit
    ui/tk_ui.py              Windows overlay + tray (tkinter, pystray)
    ui/appkit_ui.py          macOS overlay + menu bar (NSPanel, WKWebView)
    ui/overlay.html          macOS overlay markup
  whispertype.pyw           Windows launcher (pythonw, no console)
  main.py                   macOS launcher (used by the .app bundle)
  install.bat               Windows installer
  install_mac.sh            macOS installer (also the updater)
  uninstall_mac.sh          macOS uninstaller
  setup_mac.py              py2app bundle definition
  requirements.txt          Windows dependencies
  requirements-mac.txt      macOS dependencies
  voice_daemon.log          Runtime log (rolls over at 1 MB, keeps one backup)
```

State lives in `~/.whispertype/`: `config.json`, `history.json` and the venv.

The overlay is rebuilt from scratch on macOS rather than shared. Tk cannot be used there: its `XMapWindow` calls `[NSApp activateIgnoringOtherApps:]` on every window map, so the overlay would steal focus from the very window the transcript is about to be typed into.

---

## License

MIT
