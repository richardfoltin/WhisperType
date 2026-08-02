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

That's it. The installer sets up everything: Python environment, CUDA-accelerated PyTorch, Whisper model download (~809 MB), config file, and a Windows Startup shortcut so it launches automatically on boot.

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
| Stop recording **and press Enter** in the target app | **Enter** (while recording) |
| Auto-stop | `silence_duration` seconds of silence (default 3, `0` disables it) |
| Open history | **Space** (while recording), or the tray / menu bar |
| Leave history | **Space** |
| Hide overlay (discards a recording in progress) | **Esc** |
| Switch model | Tray / menu bar ▸ Model |
| Exit | Tray / menu bar ▸ Exit |

Space and Esc are only intercepted while you are actually recording or in history — they behave normally in your other apps the rest of the time, including while a transcription is still running.

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

## Benchmark (Windows)

Right-click the tray icon > **Benchmark next recording**, then dictate as usual. Instead of typing the result, WhisperType runs that one clip through **every downloaded model** and shows a comparison table: load time, transcribe time, and the text each model produced.

The models are loaded one at a time — the dictation model is released first and each benchmarked model is freed before the next is loaded, so a full run does not need seven models' worth of VRAM. Your original model is restored at the end.

Results are also written to `~/.whispertype/benchmarks/` as JSON plus an append-only `benchmark_log.txt`.

Use **Download all models** in the tray menu first if you want the comparison to cover everything rather than just what happens to be cached.

This is the quickest way to answer "is `large-v3` actually worth the extra seconds for my voice and my vocabulary" — and, if you are on a pre-Turing GPU, to compare `"fp16": true` against `"fp16": false` (see Configuration). The benchmark decodes with exactly the same options as normal dictation, so what it measures is what you get.

The results panel is Windows-only: the macOS overlay is an HTML page with no table for per-model rows, so the menu items are not offered there.

---

## OpenAI API mode

WhisperType normally runs Whisper on your own machine. The tray / menu bar ▸ **Engine** switches it to OpenAI's hosted transcription API instead — useful on a machine with no usable GPU, or when you want a model you cannot run locally.

| | Local | OpenAI API |
|---|---|---|
| Where the audio goes | nowhere | uploaded to OpenAI |
| Cost | none | per minute of audio |
| First-use wait | model download + load | none |
| Works offline | yes | no |
| Models | `large-v3-turbo` … `tiny` | `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1`, … |

The model list is fetched live from `/v1/models`, so new models appear without an update here; a built-in list is used if the request fails. At the time of writing that yields:

| Model | Notes |
|---|---|
| `gpt-4o-transcribe` | the default — best general accuracy |
| `gpt-4o-mini-transcribe` | cheaper and faster, slightly less accurate |
| `gpt-4o-transcribe-diarize` | adds speaker labels; pointless for single-speaker dictation |
| `whisper-1` | the original hosted Whisper; cheapest, oldest |

The `gpt-realtime*` family is **not** offered. Those are websocket session models — `POST /v1/audio/transcriptions` answers `404 Invalid URL` for them — and a menu entry that silently transcribed with something else would be worse than no entry.

### The API key

Set it from **Engine ▸ Set API key…**, or place it yourself. WhisperType looks in this order:

1. the `OPENAI_API_KEY` environment variable
2. `~/.whispertype/openai_api_key` — a file of its own, mode `600`
3. `"openai_api_key"` in `config.json`

The first two are preferred, and the menu writes to (2). Keeping the key out of `config.json` matters because that file is the one people paste into bug reports. **None of these locations is inside the repository**, so the key cannot reach git. It is never written to `voice_daemon.log` either — API errors are logged without the request headers.

Switching engines does not lose anything: each engine remembers its own model, and if the switch fails WhisperType falls back to the engine that was working and says why.

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
| `input_device` | *(system default)* | Device index, or a substring of its name. On macOS, e.g. `"MacBook Air Microphone"` to stop recording from switching AirPods into low-quality call mode. On Windows the same physical mic is often exposed several times over different host APIs and the system default is not always the one carrying signal — the startup log prints the idle noise floor so you can tell. |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, `es`, `ja`, etc.) |
| `silence_threshold` | `200` | Audio level below this = silence (0-32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop. `0` disables it — end every dictation with the key instead |
| `max_recording_time` | `300.0` | Maximum recording length in seconds |
| `sample_rate` | `16000` | Capture rate. Whisper expects 16 kHz — leave it alone |
| `chunk_size` | `1024` | Frames per read |
| `fp16` | `true` on GPU | Half-precision inference (Windows/CUDA). Forced off on CPU |
| `min_speech_seconds` | `0.25` | A clip with less speech than this is discarded instead of transcribed. Whisper reliably invents a sentence for silence, and an accidental double-tap would otherwise type it into your document. |
| `idle_unload_minutes` | `10` | Release the model after this many idle minutes (`0` = keep it resident). Measured: 1799 MB → 260 MB, and reloading from the local cache takes about a second. |
| `theme` | `"auto"` | Overlay appearance: `auto` follows the system, or pin it with `dark` / `light`. |
| `stt_engine` | `"local"` | `local` runs Whisper here; `openai` uses the hosted API (see above) |
| `openai_model` | `"gpt-4o-transcribe"` | Model used in API mode |
| `openai_endpoint` | `https://api.openai.com/v1` | Override for an Azure/compatible endpoint |
| `openai_api_key` | `null` | Last-resort key location — prefer the env var or the key file |
| `last_model` | *(written by the app)* | Model chosen from the tray / menu bar |

Unrecognised keys are listed in `voice_daemon.log` at startup, so a typo shows up instead of silently doing nothing.

**If recordings cut themselves off mid-sentence**, it is the silence auto-stop firing on a thinking pause. Every capture now logs why it ended:

```
Capture: 7.2s, speech 4.1s, peak RMS 4820 (threshold 200), ended by silence
  Auto-stopped after 3.0s below the threshold. Raise "silence_duration" if you
  pause longer than that while thinking, or set it to 0 and always stop with the key.
```

Microphones with aggressive noise suppression output *exact* digital zero between words, so a pause has no room tone to keep the counter from advancing — the startup log prints the idle noise floor so you can see whether that is your situation. If the peak never exceeds the threshold at all, the log says so too, and the cause is the wrong `input_device` or a `silence_threshold` set above your voice.

**About `fp16`:** half precision is the right default on Turing and newer (RTX 20xx+). Pascal cards (GTX 10xx, `sm_61`) run fp16 math at 1/64 rate, so fp32 can be faster there. The log prints your GPU's compute capability at startup.

If `config.json` is missing it is created from the defaults. If it exists but cannot be parsed, WhisperType runs on defaults and **leaves your file alone** — one stray comma must not cost you your settings, so nothing writes over a config it could not read.

Everything except `push_to_talk_key` takes effect on the next dictation — `language` and `input_device` are re-read per job, so the menu-bar submenus change them with no restart at all. The hotkey is read once at startup, so changing it needs (macOS):

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
    singleton.py             Single-instance guard (mutex / lock file)
  whispertype.pyw           Windows launcher (pythonw, no console)
  main.py                   macOS launcher (used by the .app bundle)
  start.bat                 Windows launch script
  start_silent.vbs          Windows launch script (no console at all)
  start_debug.bat           Windows launch with console output
  install.bat               Windows installer
  install_mac.sh            macOS installer (also the updater)
  uninstall_mac.sh          macOS uninstaller
  setup_mac.py              py2app bundle definition
  config.template.json      Default configuration template
  initial_prompt.md         Vocabulary bias fed to Whisper (223-token budget)
  requirements.txt          Windows dependencies
  requirements-mac.txt      macOS dependencies
  voice_daemon.log          Runtime log (rolls over at 1 MB, keeps one backup)
```

State lives in `~/.whispertype/`: `config.json`, `history.json` and the venv.

The overlay is rebuilt from scratch on macOS rather than shared. Tk cannot be used there: its `XMapWindow` calls `[NSApp activateIgnoringOtherApps:]` on every window map, so the overlay would steal focus from the very window the transcript is about to be typed into.

---

## License

MIT
