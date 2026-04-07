# WhisperType

**Push-to-talk voice dictation for Windows — powered by OpenAI Whisper, runs locally, types into any app.**

No cloud. No API keys. No subscriptions. Your voice never leaves your machine.

---

## ✨ Features

- 🎙️ **Push-to-talk** — Double-tap Right Ctrl to record, single tap to stop
- 🤫 **Silence detection** — Automatically stops after 3 seconds of silence
- ⌨️ **Types anywhere** — Injects text into whatever window is focused (editors, browsers, chat apps, terminals)
- 🔒 **100% offline** — Whisper runs locally on your GPU/CPU, nothing is sent anywhere
- ⚡ **GPU accelerated** — CUDA support for near-instant transcription
- 🔄 **Model switching** — Switch between Whisper models from the system tray
- 🎨 **Recording overlay** — Floating widget with audio level meter, timer, and status
- 📌 **System tray** — Runs silently in the background, starts with Windows
- 🚫 **No clipboard hijacking** — Uses Win32 SendInput (Unicode), never touches your clipboard
- 🚫 **No console flash** — Direct PCM-to-Whisper pipeline, no ffmpeg subprocess

## 📸 Screenshot

<!-- TODO: Add screenshot of the recording overlay -->

## 📦 Installation

### Prerequisites

- **Python 3.10+** — [Download here](https://python.org). ⚠️ Check **"Add python.exe to PATH"** during installation!
- **NVIDIA GPU recommended** — CUDA-capable GPU for fast transcription. CPU works but is slower.

### One-click install

1. Clone or download this repository
2. Double-click **`install.bat`**
3. Wait for the Whisper model to download (~1.5 GB, one-time only)
4. Done. Run **`start.bat`** to launch.

The installer handles everything automatically:
- Creates a Python virtual environment
- Installs PyTorch with CUDA support (falls back to CPU if no NVIDIA GPU)
- Installs all dependencies
- Downloads the Whisper speech recognition model (~1.5 GB)
- Creates config at `%USERPROFILE%\.whispertype\config.json`
- Adds a Windows Startup shortcut (auto-starts with Windows)

### Manual install

```bash
python -m venv .venv
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -c "import whisper; whisper.load_model('large-v3-turbo', device='cpu')"
mkdir %USERPROFILE%\.whispertype
copy config.template.json %USERPROFILE%\.whispertype\config.json
```

## ⚙️ Configuration

Edit `%USERPROFILE%\.whispertype\config.json`:

| Option | Default | Description |
|--------|---------|-------------|
| `push_to_talk_key` | `"ctrl_r"` | Hotkey for push-to-talk. Options: `ctrl_r`, `ctrl_l`, `shift_r`, `shift_l`, `alt_r`, `alt_l` |
| `language` | `"en"` | Language code for Whisper (e.g. `"en"`, `"hu"`, `"de"`, `"ja"`) |
| `whisper_model` | `"large-v3-turbo"` | Default model to load on startup |
| `models` | `["large-v3-turbo", "large-v3"]` | Models available in the tray menu for switching |
| `sample_rate` | `16000` | Audio sample rate in Hz (16000 is what Whisper expects) |
| `channels` | `1` | Audio channels (1 = mono, which Whisper requires) |
| `chunk_size` | `1024` | Audio buffer size in frames |
| `silence_threshold` | `200` | RMS level below which audio is considered silence (lower = more sensitive) |
| `silence_duration` | `3.0` | Seconds of silence before recording auto-stops |
| `max_recording_time` | `300.0` | Maximum recording duration in seconds (safety limit) |

## 🚀 Usage

1. **Launch** — Run `start.bat` (or it auto-starts with Windows after install)
2. **Record** — Double-tap **Right Ctrl** quickly (within 400ms)
3. **Speak** — A floating overlay appears showing recording status and audio levels
4. **Stop** — Either:
   - Single tap **Right Ctrl** to stop manually
   - Wait 3 seconds of silence for auto-stop
5. **Text appears** — Transcribed text is typed into whatever window has focus

Switch Whisper models anytime from the system tray icon (right-click).

## 🔧 How It Works

```
Microphone → PyAudio (16kHz mono PCM) → Whisper model → Win32 SendInput (Unicode)
```

1. **PyAudio** captures raw PCM audio at 16kHz, mono, 16-bit — exactly what Whisper expects
2. Raw PCM bytes are converted directly to a float32 numpy array — **no ffmpeg subprocess** (avoids the console window flash that plagues most Whisper wrappers on Windows)
3. **OpenAI Whisper** transcribes locally on GPU (CUDA) or CPU
4. **Win32 SendInput** with `KEYEVENTF_UNICODE` types each character as a virtual keystroke — **no clipboard** is used (avoids overwriting whatever you had copied)

A single **PyAudio** instance is created at startup and reused for all recordings — opening/closing PyAudio per recording would create a brief console window flash on Windows.

## 🏗️ Key Design Decisions

| Decision | Why |
|----------|-----|
| **No clipboard** | `SendInput` with Unicode flags types directly. Using clipboard would flash and overwrite user's clipboard contents. |
| **No ffmpeg subprocess** | Whisper's `load_audio()` shells out to ffmpeg, which flashes a CMD window on Windows. We bypass it entirely by feeding raw PCM → numpy → mel spectrogram. |
| **Single PyAudio instance** | Creating/destroying PyAudio per recording causes a brief console flash on Windows. One instance lives for the entire daemon lifetime. |
| **GPU auto-detection** | Uses CUDA if available, gracefully falls back to CPU. No config needed. |
| **Double-tap activation** | Prevents accidental triggers — a single keypress does nothing. Only a quick double-tap within 400ms starts recording. |

## 💻 Requirements

- **Windows 10 or 11**
- **Python 3.10+**
- **NVIDIA GPU recommended** (any CUDA-capable GPU; CPU works but is slower)
- **Microphone**

## 🐛 Debugging

If something isn't working:

1. Run **`start_debug.bat`** to see console output
2. Check `voice_daemon.log` in the project directory
3. Common issues:
   - **No audio**: Check Windows microphone permissions and default input device
   - **Slow transcription**: Install PyTorch with CUDA (the installer tries this automatically)
   - **PyAudio install fails**: You may need to install it from a wheel — see [PyAudio on Windows](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio)

## 📄 License

MIT — see [LICENSE](LICENSE).
