"""
WhisperType — Push-to-talk voice dictation (Windows launcher).

The application itself lives in the `whispertype` package; this file only
exists so the Windows Startup shortcut can keep pointing at a .pyw, which is
what makes pythonw.exe run it without a console window.

- Double-tap Right Ctrl: start recording
- Single Right Ctrl during recording: stop recording
- 3s silence also stops recording
- Transcription queue: record next while previous transcribes
- System tray icon with model switching
- Overlay: GPU graph, recording indicator, queue status
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from whispertype import main  # noqa: E402

if __name__ == "__main__":
    main()
