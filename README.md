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

That's it. The installer sets up everything: Python environment, CUDA-accelerated PyTorch, Whisper model download (~809 MB), config file, and a Windows Startup shortcut so it launches automatically on boot.

After installation, run `start.bat` or just restart your PC — WhisperType will be waiting in the system tray.

---

## How to Use

| Action | Key |
|--------|-----|
| Start recording | **Double-tap Right Ctrl** (within 400ms) |
| Stop recording | **Right Ctrl** (single tap) |
| Stop recording **and press Enter** in the target app | **Enter** (while recording) |
| Auto-stop | 3 seconds of silence |
| Open history (a recording in progress is still transcribed) | **Space** |
| Resume recording | **Space** (while in history) |
| Minimize to tray (discards a recording in progress) | **Esc** |
| Switch model | Right-click tray icon > Model |
| Show history | Right-click tray icon > Show history |
| Exit | Right-click tray icon > Exit |

1. Double-tap **Right Ctrl** to start recording — a floating overlay appears
2. Speak naturally
3. Tap **Right Ctrl** once to stop (or just pause — silence auto-stops after 3 seconds)
4. The transcribed text is typed into whichever window was active when you **started** recording

Text is injected directly via keystrokes — no clipboard involved, works in any app.

### What happens to a recording in progress

| You press | The audio is |
|---|---|
| **Right Ctrl** | transcribed |
| **Enter** | transcribed, then Enter is sent to the target app |
| 3 s of silence, or the length limit | transcribed |
| **Space** (jump to history) | **transcribed** — glancing at history never costs you a dictation |
| **Esc** (minimize to tray) | discarded |
| Opening the benchmark panel | discarded |
| Tray → **Exit** | discarded — history is in memory, so there is nowhere for the text to survive to |

Esc is the deliberate "forget this one" gesture; Space is not. Every discard is
written to `voice_daemon.log`, so nothing disappears without a trace.

### When Space and Esc are WhisperType keys

Space and Esc are only intercepted while WhisperType actually owns the keyboard:
you are recording, the history or benchmark panel is open, or you have clicked
the overlay. They are **not** intercepted merely because the overlay is on
screen waiting for the queue to drain — otherwise every space you typed into
your editor during a transcription would pop the history panel open.

If the overlay is up but not focused, use **Show history** in the tray menu, or
click the overlay first.

---

## Transcription Queue

You don't have to wait for one transcription to finish before recording the next:

1. Record message A → stops, enters queue
2. While A transcribes, record message B → stops, enters queue
3. A finishes → text typed into its target window
4. B finishes → text typed into its target window

Each recording remembers which window was active when you **started** it, so text always goes to the right place even if you switch windows during transcription.

If that window is gone by the time the text is ready, WhisperType refuses to type into whatever happens to be in front — the transcription stays in history, and the log says why.

---

## Overlay

The floating overlay appears during recording and while transcriptions are queued:

```
+--------------------------------------+
| WhisperType                       x  |  Drag to reposition / x hides it
+--------------------------------------+
| GPU  ||||||||......          67%     |  GPU utilization graph (last 60s)
+--------------------------------------+
| * Recording              0:42       |  Live recording status
| Model: large-v3-turbo               |
| [========...........|..............]  |  Audio level + silence threshold
| Transcribe: R-Ctrl / Enter / 3s ... |  Context-aware keyboard hints
+--------------------------------------+
| TIME      DUR   APP      WINDOW     |  Transcription queue
| 10:04:21  1.2s  Code     server.ts  |  Currently transcribing (gold)
| 10:04:35  0.8s  Chrome   ChatGPT..  |  Waiting (gray)
+--------------------------------------+
```

The overlay shows different states:

- **Recording** — red blinking dot, audio level meter, timer
- **Transcribing** — yellow dot, queue with progress (the **×** on a row cancels that job)
- **History** — green dot, last 50 transcriptions with copy/delete buttons
- **Benchmark** — one row per model, with timings and text previews

The GPU graph collects data in the background from startup, so it's ready instantly when the overlay appears.

### History

Press **Space** to open history — a list of your last 50 transcriptions (8 visible, scroll for the rest). Each entry shows the timestamp, duration, source app, window name, and has:

- **Copy button** — copies the transcription to clipboard (hover to preview the text)
- **Delete button** — removes the entry from history

Press **Space** again to close history and start a new recording.

If you were recording when you pressed Space, **that recording is still transcribed** — it goes into the queue exactly as if you had tapped Right Ctrl. Glancing at your history never costs you a dictation.

Nothing is ever lost to a failed hand-off: if the target window has closed, or the text was cancelled mid-transcription, it still lands in history for you to copy.

---

## Benchmark

Right-click the tray icon > **Benchmark next recording**, then dictate as usual. Instead of typing the result, WhisperType runs that one recording through **every downloaded model** and shows a comparison table: load time, transcribe time, and the text each model produced.

Results are also written to `%USERPROFILE%\.whispertype\benchmarks\` as JSON plus an append-only `benchmark_log.txt`.

Use **Download all models** in the tray menu first if you want the comparison to cover everything rather than just what happens to be cached.

This is the quickest way to answer "is `large-v3` actually worth the extra seconds for my voice and my vocabulary" — and, if you are on a pre-Turing GPU, to compare `"fp16": true` against `"fp16": false` (see Configuration).

---

## Vocabulary bias

`initial_prompt.md`, next to the script, is fed to Whisper as its `initial_prompt`. Put your jargon in it — project names, library names, anything Whisper keeps mishearing — and transcription accuracy on those words improves sharply.

**It has a hard budget of 223 tokens.** Whisper truncates anything longer and drops the overflow from the *front*, silently. WhisperType measures the file at startup and logs exactly what would be lost:

```
[10:40:03] WARNING: initial prompt is 323 tokens but Whisper only keeps the last 223.
           The first 100 tokens are dropped:
             DROPPED -> A Helm CRM-en fejlesztek, Next.js és FastAPI stackkel...
```

Check `voice_daemon.log` after editing the file. A comma-separated word list uses the budget far more efficiently than prose.

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
| `whisper_model` | `"large-v3-turbo"` | Model to start with |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, `es`, `ja`, etc.) |
| `silence_threshold` | `200` | Audio level below this = silence (0-32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop |
| `max_recording_time` | `300.0` | Maximum recording length in seconds |
| `sample_rate` | `16000` | Capture rate. Whisper expects 16 kHz — leave it alone |
| `chunk_size` | `1024` | PyAudio frames per read |
| `fp16` | `true` on GPU | Half-precision inference. Forced off on CPU |
| `last_model` | *(written by the app)* | Model chosen from the tray menu; overrides `whisper_model` |

Unrecognised keys are listed in `voice_daemon.log` at startup, so a typo shows up instead of silently doing nothing.

**About `fp16`:** half precision is the right default on Turing and newer (RTX 20xx+). Pascal cards (GTX 10xx, `sm_61`) run fp16 math at 1/64 rate, so fp32 can be faster there. The log prints your GPU's compute capability at startup; use Benchmark mode to compare.

If `config.json` is missing or corrupt, WhisperType falls back to `config.template.json` and then to built-in defaults, rather than dying invisibly.

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
4. Downloads the default Whisper model (~809 MB, one-time)
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

Run `start_debug.bat` to see console output. Check `voice_daemon.log` for errors — the previous run is kept as `voice_daemon.prev.log`, so relaunching no longer destroys the log of the crash you are chasing.

| Problem | Solution |
|---------|----------|
| "Python not found" | Install Python 3.10+ and check "Add to PATH" |
| Nothing happens on launch | It is probably already running — check the tray. A second instance refuses to start and says so |
| No CUDA / slow | Run `python -c "import torch; print(torch.cuda.is_available())"` — if `False`, reinstall PyTorch: `pip install torch --index-url https://download.pytorch.org/whl/cu124` |
| No microphone | Check Windows Sound Settings, make sure a mic is set as default input |
| Text not appearing | Check the log. If it says "could not activate target window", the target closed while transcribing — the text is in history. Some apps also block simulated keystrokes; try Notepad to verify |
| Text goes to the wrong window | Fixed — the target is captured when you *start* recording and re-captured when you leave the history panel |
| Vocabulary bias not working | `initial_prompt.md` is probably over the 223-token budget; the log says exactly what got dropped |
| `FutureWarning: pynvml is deprecated` | Both `pynvml` and `nvidia-ml-py` are installed. `pip uninstall -y pynvml && pip install --force-reinstall nvidia-ml-py` |
| Console window flash | Use `start_silent.vbs` instead of `start.bat` (the installer's startup shortcut already does this) |

---

## Files

```
WhisperType/
  whispertype.pyw       Main application (runs as background daemon)
  install.bat           One-click installer
  start.bat             Launch script (no console window)
  start_silent.vbs      Launch script (no console at all, used by startup shortcut)
  start_debug.bat       Launch with console output for debugging
  config.template.json  Default configuration template
  initial_prompt.md     Vocabulary bias fed to Whisper (223-token budget)
  requirements.txt      Python dependencies
  voice_daemon.log      Runtime log (created on each launch)
  voice_daemon.prev.log Previous run's log
```

---

## License

MIT
