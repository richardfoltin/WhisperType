# 🎙️ WhisperType

**Push-to-talk voice dictation for Windows and macOS — speak anywhere, and the text
lands in the app you were working in.**

Whisper runs on your own GPU by default: no cloud, no API key, no subscription, and
your voice never leaves the machine. A hosted-API mode exists for machines with no
usable GPU — it is the only mode that ever uploads anything, and you have to turn it
on yourself.

| | 🪟 Windows | 🍎 macOS (Apple Silicon) |
|---|---|---|
| Engine | `openai-whisper` on CUDA | `mlx-whisper` on Metal |
| Hotkey | Right Ctrl (double-tap) | Right Command (double-tap) |
| Lives in | System tray | Menu bar |
| Overlay | tkinter, drawn to the Windows palette | NSPanel + WKWebView, system materials |
| Settings | **Settings…** window | Menu-bar submenus |
| Autostart | Startup shortcut | LaunchAgent |

---

## ✨ What's new

- **⚙️ A Settings window on Windows** — engine, model, language, microphone, silence
  handling, theme and the API key in one place, each applying as you change it. A
  Win32 tray menu closes on every click, so changing three things used to mean three
  trips. [Details](#-settings-window-windows)
- **🪟 Windows overlay parity with macOS** — the transcript runs across the panel
  before it is typed, the level meter is an auto-ranging waveform with the silence
  auto-stop as a hairline, hints are keycaps, panel size is fixed per state, and
  history is day-grouped rows led by the target app's real icon. [Details](#-the-overlay)
- **🎨 The Windows panel follows your Windows theme** — including your accent colour,
  corrected until it stays legible on the panel; `auto` is re-read every time the
  overlay appears.
- **🛡️ Elevated windows are detected before typing** — a transcript aimed at an
  app running as administrator is kept with a reason instead of being silently eaten
  by UIPI while `SendInput` reports success.
- **🪶 `large-v3-turbo-q4` on macOS** — 464 MB instead of 1.6 GB, identical transcript
  in testing, and a much faster reload after an idle unload. [Details](#-models)
- **🌐 The hosted model list is no longer pinned to one generation** — `/v1/models` is
  filtered on the capability word, so a future `gpt-5-transcribe` appears on its own.
  [Details](#-openai-api-mode)
- **⌨️ Space no longer opens history.** Watching a key you press constantly, globally,
  for as long as the overlay happened to be up was never a good trade. It is a menu
  item now.

---

## 🚀 Quick start

### 🪟 Windows

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
install.bat
```

That's it. The installer sets up everything: a Python virtual environment,
CUDA-accelerated PyTorch (falling back to a CPU build if that fails), the Whisper
model download (~809 MB), the config file, and a Windows Startup shortcut so it
launches on boot.

Afterwards run `start.bat`, or just restart your PC — WhisperType will be waiting in
the system tray.

### 🍎 macOS

```
git clone https://github.com/richardfoltin/WhisperType.git
cd WhisperType
./install_mac.sh
```

The installer creates a virtualenv in `~/.whispertype/venv`, installs the MLX stack,
downloads the model (~1.6 GB), builds a small `WhisperType.app` into `~/Applications`
and registers a LaunchAgent so it starts at login.

**Then grant three permissions** in System Settings ▸ Privacy & Security — WhisperType
cannot work without them, and macOS gives no error when they are missing:

| Permission | Why |
|---|---|
| 🎤 Microphone | recording (prompted automatically) |
| ♿ Accessibility | typing the transcript into other apps |
| ⌨️ Input Monitoring | the push-to-talk hotkey |

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

## ⌨️ How to use

1. **Double-tap Right Ctrl** (macOS: **Right ⌘**) to start recording — the overlay appears
2. Speak naturally
3. Tap the same key once to stop, or just pause — silence auto-stops after 3 seconds
4. The transcript is typed into whichever window was active when you **started**
   recording

Text is injected as keystrokes — no clipboard involved, so it works in any app and
never clobbers what you had copied.

| Action | How |
|--------|-----|
| ▶️ Start recording | **Double-tap Right Ctrl / Right ⌘** (within 400 ms) |
| ⏹️ Stop and transcribe | **Right Ctrl / Right ⌘** (single tap) |
| ↩️ Stop, transcribe, **then press Enter** in the target app | **Enter** (while recording) |
| ⏱️ Auto-stop | `silence_duration` seconds of silence (default 3, `0` disables it) |
| 🙈 Hide the overlay — discards a recording in progress | **Esc** |
| 🕘 History | Tray / menu bar ▸ **Show history** |
| ⚙️ Change any setting | Tray ▸ **Settings…** (Windows) · menu-bar submenus (macOS) |
| 🚪 Exit | Tray / menu bar ▸ **Exit** |

Esc is the only key WhisperType watches besides the hotkey, and only while the overlay
is on screen. It behaves normally in your other apps the rest of the time — including
while a transcription you started is still running, as long as you have hidden the
overlay.

> ℹ️ **The Enter that ends a dictation also reaches the app you are in.** Swallowing
> it means switching the global key tap out of listen-only mode, which puts our
> callback in the critical path of every keystroke on the machine — and when such a
> callback overruns the system timeout, the OS disables the tap and the hotkey
> silently stops working until a restart. A stray newline is the cheaper failure, so
> the suppression is deliberately off on **both** platforms.

### 🗑️ What happens to a recording in progress

| You press | The audio is |
|---|---|
| **Right Ctrl / Right ⌘** | transcribed |
| **Enter** | transcribed, then Enter is sent to the target app |
| 3 s of silence, or the length limit | transcribed |
| **Esc** | **discarded** |
| Tray / menu bar ▸ **Show history** | **discarded** |
| Opening the benchmark panel | discarded |
| Tray ▸ **Exit** | kept — it finishes transcribing and lands in history, it is just not typed |

Esc is the deliberate "forget this one" gesture. Every discard is written to
`voice_daemon.log`, so nothing disappears without a trace.

A clip with less than `min_speech_seconds` of actual speech in it is dropped with a
brief **Nothing heard** notice rather than transcribed — Whisper reliably invents a
sentence for silence, and an accidental double-tap would otherwise type it into your
document.

---

## 🪟 The overlay

A floating panel, visible while recording and while transcriptions are in flight.
Drag it anywhere; **×** or **Esc** hides it. Both platforms now show the same thing.

```
+---------------------------------------+
| WhisperType                        x  |  Drag to reposition / x hides it
+---------------------------------------+
| * Recording                     0:42  |  Live status and elapsed time
| large-v3-turbo          -> server.ts  |  Model, and where the text is going
|                                       |
|          .||ıı|||ı..ıı|||||ı.         |  The last few seconds of audio
|          ------------_________        |  Run-up to the silence auto-stop
|                                       |
|   [R-Ctrl] Transcribe  [Esc] Discard  |  Context-aware keycap hints
+---------------------------------------+
```

The panel is a **fixed size per state**, so changing state never resizes the window
under your pointer. The states:

- 🔴 **Recording** — blinking dot, a scrolling waveform of the last few seconds, and a
  hairline that fills as the silence auto-stop counts down, so a recording that ends
  itself is never a mystery. The waveform **auto-ranges to your microphone** instead
  of assuming a fixed ceiling — a webcam mic peaks around 1300 RMS and used to draw
  speech as dirt on the centre line
- 🟠 **Transcribing** — the finished transcript runs in from the right before it is
  typed, so there is a moment where you can see what is about to land in your
  document. A queue list appears only when something is genuinely waiting behind the
  job in flight (the **×** on a row cancels that job)
- 🕘 **History** — the last 50 transcriptions
- ❗ **Failed** — the reason, in the middle of the panel, until you press Esc
- 🧪 **Benchmark** (Windows) — one row per model, with timings and text previews

The overlay appears on the display your pointer is on, and a position you have dragged
it to is kept only while it is still on that display — otherwise it would reappear on
a screen you are not looking at, which is indistinguishable from the app not having
started.

The GPU graph is **off by default** — it is developer telemetry sitting above "am I
recording?". Turn it on in Settings ▸ Appearance (or `show_gpu_graph`); either way the
utilisation still appears as a small chip while a transcription runs.

**Colours follow the system.** On macOS the panel is an `NSVisualEffectView` HUD
material behind a transparent web view, so the background is translucent and the text
is opaque, and every colour is a system keyword — it tracks your appearance, accent
colour, Increase Contrast and Reduce Transparency with no settings of its own. Tk has
to be told all of that explicitly, so on Windows the palette is resolved from your
theme and accent colour, corrected until the accent stays legible on the panel.

---

## 🕘 History

The last 50 transcriptions, written to `~/.whispertype/history.json` so they survive
quitting and logging out. Open it from the tray / menu bar ▸ **Show history**.

Rows are grouped by day, newest first. Each one leads with the icon of the app the
text was meant for — dug out of the target executable on Windows, resolved from the
bundle id on macOS — and shows the transcript itself as the largest, highest-contrast
thing in the row, because that is the only thing anyone is ever scanning for.

- 📖 Long transcripts clamp to a few lines with a **Show more** disclosure and a word count
- 📋 Hovering a row swaps its timestamp for **Copy** and **✕**, in a fixed-width slot so
  revealing them cannot resize the panel under your pointer
- 🧹 **Clear All** arms on the first click and only clears on the second

Nothing is ever lost to a failed hand-off: if the target window has closed, if it
turned out to be running as administrator, or if the text was cancelled
mid-transcription, it still lands here for you to copy.

---

## 🧵 Transcription queue

You don't have to wait for one transcription to finish before recording the next:

1. Record message A → stops, enters the queue
2. While A transcribes, record message B → stops, enters the queue
3. A finishes → typed into A's target window
4. B finishes → typed into B's target window

Each recording remembers which window was active when you **started** it, so the text
goes to the right place even if you switch windows during transcription.

If the target window is gone by the time the text is ready, WhisperType refuses to
type into whatever happens to be in front — the transcript stays in history and the
log says why.

---

## 🧰 Settings window (Windows)

Tray ▸ **Settings…**, or just double-click the tray icon.

It exists because a Win32 tray menu cannot stay open across a click:
`TrackPopupMenuEx` is modal, so the OS dismisses the menu and only *then* delivers the
click — and a `radio=True` item is no different, it only swaps the tick for a bullet.
Changing three settings through the submenus therefore meant opening the menu three
times. This window stays open and applies each change as you make it.

| Section | Controls |
|---|---|
| 🗣️ **Transcription** | Engine (local / OpenAI API), Model, Language |
| 🎤 **Audio** | Microphone, Stop after silence, Silence threshold |
| 🎨 **Appearance** | Theme, GPU graph |
| 🧠 **Memory** | Release model when idle |
| 🌐 **OpenAI** | API key |

Two details worth knowing:

- The **silence threshold** is a drawn slider with a marker at your microphone's
  **measured idle level**. `200` means nothing on its own — seeing that your mic idles
  at `0.5` is exactly what explains why a thinking pause trips the auto-stop.
- The **model dropdown** re-reads the catalogue when you switch engine, showing
  "Switching…" while the new engine loads instead of quietly offering the previous
  engine's models.

The submenus stay in the tray for a quick one-off change. On macOS everything lives in
the menu-bar submenus; there is no separate window.

---

## 🎛️ Tray menu / menu bar

Everything you need day to day is in the menu, so you never have to edit JSON for the
common cases:

| Item | Notes | 🪟 | 🍎 |
|---|---|---|---|
| **Settings…** | One window with everything (also the double-click action) | ✅ | — |
| **Show history** | Opens the history list without starting a recording | ✅ | ✅ |
| **Pause dictation** | Ignores the hotkey until you switch it back; the icon shows a struck-through mic | ✅ | ✅ |
| **Engine** | Local model or the OpenAI API, plus **Set API key…** | ✅ | ✅ |
| **Model** | Runtime switch. `↓` means not downloaded yet | ✅ | ✅ |
| **Language** | Takes effect on the very next dictation, no restart | ✅ | ✅ |
| **Microphone** | Same. Pin the built-in mic here so recording does not drop AirPods into call quality | ✅ | ✅ |
| **Release model when idle** | Never / 2 / 5 / 10 / 30 min / 1 h. Hidden in API mode | ✅ | ✅ |
| **Download all models** | Hidden in API mode — nothing to download | ✅ | ✅ |
| **Benchmark next recording** / **Open last benchmark** | See below | ✅ | — |
| **Open log** | `voice_daemon.log` (macOS: in Console) | ✅ | ✅ |
| **Restart WhisperType** | Needed only after changing the hotkey | ✅ | ✅ |
| **Exit** | | ✅ | ✅ |

The first line of the menu says what the app is doing when the icon alone cannot: the
error, or which model is still loading. An engine switch takes seconds, and the menu
now moves the tick the instant you click rather than when the model finishes.

The icon colour is the app's status at a glance: 🟢 ready, 🔴 recording, 🟠
transcribing, ⚪ downloading or loading, **❗ red exclamation mark** when something
failed, struck-through when paused.

---

## 🧠 Models

Tray / menu bar ▸ **Model** (or Settings ▸ Transcription). A checkmark marks the
active one, `↓` means it is not downloaded yet, the icon changes during the download,
and your choice is remembered across restarts — but only if it actually loaded, so one
bad switch cannot break the next launch.

**🪟 Windows (`openai-whisper`, CUDA)**

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| `large-v3-turbo` | 809 MB | Fast | Very good |
| `large-v3` | 1.5 GB | Slow | Best |
| `large-v2` | 1.5 GB | Slow | Best |
| `medium` | 769 MB | Medium | Good |
| `small` | 244 MB | Fast | Decent |
| `base` | 74 MB | Very fast | Basic |
| `tiny` | 39 MB | Instant | Basic |

**🍎 macOS (`mlx-whisper`, Metal)** — different repos, so different sizes:

| Model | Size | Notes |
|-------|------|-------|
| `large-v3-turbo` | 1.6 GB | what the installer fetches |
| `large-v3-turbo-q4` | **464 MB** | quantised. Measured on 10.7 s of Hungarian speech: identical transcript, 0.75 s vs 0.74 s. The reload after an idle unload shrinks by the same factor as the weights |
| `large-v3` | 3.1 GB | Best |
| `large-v2` | 3.1 GB | Best |
| `medium` | 1.5 GB | Good |
| `small` | 481 MB | Decent |
| `base` | 144 MB | Basic |
| `tiny` | 74 MB | Basic |

For daily use, **`large-v3-turbo`** is the best balance — and on macOS the **`-q4`**
build is the better default for most people.

> The `-8bit` turbo repo is deliberately absent: it ships `model.safetensors`, and
> mlx-whisper 0.4.3 only reads `weights.npz`, so loading it raises
> `[load_npz] Input must be a zip file`. Tried, not guessed. The `.en` and
> distil-whisper repos are English-only.

---

## 🌐 OpenAI API mode

Tray / menu bar ▸ **Engine** switches from local Whisper to OpenAI's hosted
transcription API — useful on a machine with no usable GPU, or when you want a model
you cannot run locally.

| | 🖥️ Local | ☁️ OpenAI API |
|---|---|---|
| Where the audio goes | nowhere | uploaded to OpenAI |
| Cost | none | per minute of audio |
| First-use wait | model download + load | none |
| Works offline | yes | no |
| Models | `large-v3-turbo` … `tiny` | `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1`, … |

The list is fetched live from `/v1/models`, so new models appear without an update
here; a built-in list is used if the request fails. The filter matches the
**capability word** (`^whisper-` or `transcrib`) rather than a family prefix — the old
`gpt-4o.*transcribe` pattern pinned the menu to one generation, and a future
`gpt-5-transcribe` would simply never have shown up, with nothing to explain why.
Dated snapshots (`…-2025-12-15`) are hidden to keep the menu scannable.

Excluded on purpose: `gpt-realtime*` (websocket session models —
`POST /v1/audio/transcriptions` answers `404 Invalid URL` for them, and a menu entry
that silently transcribed with something else would be worse than no entry), plus the
TTS, DALL·E and embedding models that otherwise leak into the same list.

### 🔑 The API key

Set it from **Engine ▸ Set API key…** (Windows: also Settings ▸ OpenAI), or place it
yourself. WhisperType looks in this order:

1. the `OPENAI_API_KEY` environment variable
2. `~/.whispertype/openai_api_key` — a file of its own, mode `600`
3. `"openai_api_key"` in `config.json`

The first two are preferred, and the UI writes to (2). Keeping the key out of
`config.json` matters because that is the file people paste into bug reports. **None
of these locations is inside the repository**, so the key cannot reach git. It never
reaches `voice_daemon.log` either — API errors are logged without the request headers.
The UI shows which source a key came from without ever revealing it.

Switching engines loses nothing: each engine remembers its own model, and if the
switch fails WhisperType falls back to the engine that was working and says why.

---

## 🗣️ Vocabulary bias

`initial_prompt.md`, next to the launcher, is fed to Whisper as its `initial_prompt`.
Put your jargon in it — project names, library names, anything Whisper keeps
mishearing — and accuracy on those words improves sharply. It is sent in API mode too.

**It has a hard budget of 223 tokens.** Whisper truncates anything longer and drops
the overflow from the *front*, silently. WhisperType measures the file at startup and
logs exactly what would be lost:

```
[10:40:03] WARNING: initial prompt is 323 tokens but Whisper keeps only the last 223.
           The first 100 tokens are dropped:
             DROPPED -> A Helm CRM-en fejlesztek, Next.js és FastAPI stackkel...
```

Check `voice_daemon.log` after editing the file. A comma-separated word list uses the
budget far more efficiently than prose.

---

## 🚨 When something goes wrong

A transcript that cannot be delivered is never silently dropped. The overlay stays on
screen with the reason, the tray / menu-bar icon turns into a red exclamation mark,
and the text is kept in history so you can copy it. Press **Esc** to dismiss.

This covers: the target app quit, a password field took secure input (macOS), **the
target window turned out to be running as administrator** (Windows — the system drops
synthetic keystrokes aimed at a higher integrity level, and reports success while
doing it), the Accessibility permission was revoked, the microphone was unavailable,
the model failed to load, or transcription itself threw.

---

## 🧪 Benchmark (Windows)

Right-click the tray icon ▸ **Benchmark next recording**, then dictate as usual.
Instead of typing the result, WhisperType runs that one clip through **every
downloaded model** and shows a comparison table: load time, transcribe time, and the
text each model produced.

Models are loaded one at a time — the dictation model is released first and each
benchmarked model is freed before the next is loaded, so a full run does not need
seven models' worth of VRAM. Your original model is restored at the end.

Results are also written to `~/.whispertype/benchmarks/` as JSON plus an append-only
`benchmark_log.txt`.

Use **Download all models** first if you want the comparison to cover everything
rather than just what happens to be cached.

This is the quickest way to answer "is `large-v3` actually worth the extra seconds for
my voice and my vocabulary" — and, on a pre-Turing GPU, to compare `"fp16": true`
against `"fp16": false`. The benchmark decodes with exactly the same options as normal
dictation, so what it measures is what you get.

The results panel is Windows-only: the macOS overlay is an HTML page with no table for
per-model rows, so the menu items are not offered there.

---

## 🔧 Configuration

On Windows the easy route is **Settings…** in the tray menu; on macOS the menu-bar
submenus cover the same ground. Everything below can also be edited by hand in
`%USERPROFILE%\.whispertype\config.json` (Windows) or `~/.whispertype/config.json`
(macOS):

| Key | Default | Description |
|-----|---------|-------------|
| `push_to_talk_key` | `"ctrl_r"` / `"cmd_r"` | Hotkey: `ctrl_r`, `ctrl_l`, `shift_r`, `shift_l`, `alt_r`, `alt_l`, `cmd_r`, `cmd_l`. macOS defaults to `cmd_r` because Apple keyboards have no right Ctrl |
| `language` | `"en"` | Whisper language code (`en`, `hu`, `de`, `fr`, `es`, `ja`, …) |
| `input_device` | *(system default)* | Device index, or a substring of its name. Windows exposes the same physical mic several times over different host APIs, and the index is what distinguishes them — pick it from the list rather than typing it |
| `silence_threshold` | `200` | Audio level below this counts as silence (0–32768) |
| `silence_duration` | `3.0` | Seconds of silence before auto-stop. `0` disables it — end every dictation with the key instead |
| `max_recording_time` | `300.0` | Maximum recording length, in seconds |
| `min_speech_seconds` | `0.25` | A clip with less speech than this is dropped instead of transcribed |
| `idle_unload_minutes` | `10` | Release the model after this many idle minutes (`0` = keep it resident). Measured: 1799 MB → 260 MB, and reloading from the local cache takes about a second |
| `theme` | `"auto"` | Overlay appearance: `auto` follows the system light/dark setting (re-read every time the overlay appears), or pin it with `dark` / `light`. On Windows the accent colour is taken from Windows too, and corrected until it is legible on the panel |
| `show_gpu_graph` | `false` | Draw the 60-second utilisation graph on the overlay. Off because it sits above "am I recording?"; the number still shows as a chip while transcribing |
| `sample_rate` | `16000` | Capture rate. Whisper expects 16 kHz — leave it alone |
| `channels` | `1` | Capture channels. Whisper is mono — leave it alone |
| `chunk_size` | `1024` | Frames per read |
| `fp16` | `true` on GPU | Half-precision inference (Windows/CUDA). Forced off on CPU. Not in the template — add it by hand if you need it |
| `stt_engine` | `"local"` | `local` runs Whisper here; `openai` uses the hosted API |
| `openai_model` | `"gpt-4o-transcribe"` | Model used in API mode |
| `openai_endpoint` | `https://api.openai.com/v1` | Override for an Azure/compatible endpoint |
| `openai_api_key` | `null` | Last-resort key location — prefer the env var or the key file |
| `last_model` | *(written by the app)* | Model chosen from the menu |

Unrecognised keys are listed in `voice_daemon.log` at startup, so a typo shows up
instead of silently doing nothing.

If `config.json` is missing it is created from the defaults. If it exists but cannot
be parsed, WhisperType runs on defaults and **leaves your file alone** — one stray
comma must not cost you your settings, so nothing writes over a config it could not
read. Saves are atomic, so a crash mid-write cannot truncate it either.

Everything except `push_to_talk_key` takes effect on the next dictation — `language`
and `input_device` are re-read per job. The hotkey is read once at startup, so
changing it needs **Restart WhisperType** from the menu. (On macOS that asks launchd;
on Windows a small detached helper waits for the old process to exit before starting
the new one, because the single-instance guard will not let two run at once.)

### ✂️ If recordings cut themselves off mid-sentence

That is the silence auto-stop firing on a thinking pause. Every capture logs why it
ended:

```
Capture: 7.2s, speech 4.1s, peak RMS 4820 (threshold 200), ended by silence
  Auto-stopped after 3.0s below the threshold. Raise "silence_duration" if you
  pause longer than that while thinking, or set it to 0 and always stop with the key.
```

Microphones with aggressive noise suppression output *exact* digital zero between
words, so a pause has no room tone to keep the counter from advancing. On Windows the
threshold slider in Settings shows your microphone's measured idle level right next to
the value, which is the quickest way to see whether that is your situation; the
startup log prints the same number. If the peak never exceeds the threshold at all,
the log says so too, and the cause is the wrong `input_device` or a
`silence_threshold` set above your voice.

### 🔢 About `fp16`

Half precision is the right default on Turing and newer (RTX 20xx+). Pascal cards
(GTX 10xx, `sm_61`) run fp16 math at 1/64 rate, so fp32 can be faster there. The log
prints your GPU's compute capability at startup.

---

## 📋 Requirements

**🪟 Windows**

- Windows 10 or 11
- Python 3.10+ — [python.org](https://python.org) (check "Add to PATH" during install)
- NVIDIA GPU recommended — any CUDA-capable GPU; CPU works but is much slower
- A microphone

**🍎 macOS**

- macOS 13+ on Apple Silicon (M1 or newer)
- Python 3.11+ from [python.org](https://python.org) — the system `python3` is 3.9 and
  too old for PyObjC. `install_mac.sh` downloads the installer for you if it is missing
- A microphone

Intel Macs are not supported: the transcription backend is MLX, which is Apple Silicon
only.

---

## 🛠️ Manual installation (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -c "import whisper; whisper.load_model('large-v3-turbo', device='cpu')"
mkdir %USERPROFILE%\.whispertype
copy config.template.json %USERPROFILE%\.whispertype\config.json
```

`install.bat` does the same plus the Startup shortcut.

---

## 🩺 Troubleshooting

Run `start_debug.bat` to see console output. Check `voice_daemon.log` for errors — the
previous run is kept as `voice_daemon.prev.log`, so relaunching no longer destroys the
log of the crash you are chasing.

### 🪟 Windows

| Problem | Solution |
|---------|----------|
| "Python not found" | Install Python 3.10+ and check "Add to PATH" |
| Nothing happens on launch | It is probably already running — check the tray. A second instance refuses to start and says so |
| No CUDA / slow | Run `python -c "import torch; print(torch.cuda.is_available())"` — if `False`, reinstall PyTorch: `pip install torch --index-url https://download.pytorch.org/whl/cu124` |
| No microphone | Check Windows Sound Settings and make sure a mic is the default input, then pick it explicitly in Settings ▸ Audio |
| Nothing typed into an app running as administrator | Expected, and reported. Windows drops synthetic keystrokes aimed at a higher integrity level; the transcript stays in history. Run that app unelevated, or copy the text from history |
| Text not appearing anywhere | Check the log. "could not activate target window" means the target closed while transcribing — the text is in history. Some apps also block simulated keystrokes; try Notepad to verify |
| Vocabulary bias not working | `initial_prompt.md` is probably over the 223-token budget; the log says exactly what got dropped |
| `FutureWarning: pynvml is deprecated` | Both `pynvml` and `nvidia-ml-py` are installed. `pip uninstall -y pynvml && pip install --force-reinstall nvidia-ml-py` |
| Console window flash | Use `start_silent.vbs` instead of `start.bat` (the installer's startup shortcut already does) |

### 🍎 macOS

| Problem | Solution |
|---------|----------|
| Hotkey does nothing | Input Monitoring is not granted. System Settings ▸ Privacy & Security ▸ Input Monitoring ▸ enable WhisperType |
| Overlay appears but no text is typed | Accessibility is not granted. macOS drops synthetic keystrokes silently — there is no error. Check `voice_daemon.log` |
| Nothing typed into a password field | Expected. macOS secure input blocks synthetic keystrokes; the transcript stays in history so you can copy it |
| Permissions keep resetting | Re-run `./install_mac.sh`; the bundle is ad-hoc signed with a fixed identifier so grants survive rebuilds |
| Music quality drops when recording | Your AirPods are the input device. Pick the built-in mic in menu bar ▸ Microphone |
| Stop it running at login | `launchctl bootout gui/$UID/com.whispertype.agent && rm ~/Library/LaunchAgents/com.whispertype.agent.plist` |

---

## 🗂️ Files

```
WhisperType/
  whispertype/              The application
    app.py                  State machine, hotkeys, recording, queue worker
    config.py  jobs.py      Config, transcription queue + history
    audio.py                Capture (PyAudio on Windows, sounddevice on macOS)
    transcribe.py           Engines: CUDA / MLX / OpenAI API
    backends/windows.py     Win32 SendInput, HWND targeting, NVML, elevation check
    backends/macos.py       CGEvent typing, NSWorkspace/AX targeting
    backends/mac_gpu.py     Apple GPU utilisation via IOKit
    ui/tk_ui.py             Windows overlay, tray and settings window
    ui/theme.py             Windows palette: system theme + corrected accent
    ui/winicon.py           App icons out of the target executable (ExtractIconEx)
    ui/appkit_ui.py         macOS overlay + menu bar (NSPanel, WKWebView)
    ui/overlay.html         macOS overlay markup, styling and drawing
    ui/common.py            Menu content shared by both front-ends
    singleton.py            Single-instance guard (mutex / lock file)
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

State lives in `~/.whispertype/`: `config.json`, `history.json`, `openai_api_key`,
`benchmarks/` and the venv.

The overlay is built twice rather than shared. Tk cannot be used on macOS: its
`XMapWindow` calls `[NSApp activateIgnoringOtherApps:]` on every window map, so the
overlay would steal focus from the very window the transcript is about to be typed
into.

---

## 📄 License

MIT
