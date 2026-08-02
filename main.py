#!/usr/bin/env python3
"""WhisperType entry point (macOS and Windows).

On Windows the historical `whispertype.pyw` launcher still works and does the
same thing; this file exists so the .app bundle and the LaunchAgent have a
stable, extension-neutral target.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from whispertype import main  # noqa: E402

if __name__ == "__main__":
    main()
