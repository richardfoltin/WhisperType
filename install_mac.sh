#!/bin/bash
# WhisperType — macOS installer (Apple Silicon).
#
# Creates a venv, installs the MLX/PyObjC stack, builds a small .app wrapper so
# the macOS privacy permissions attach to a stable identity instead of to the
# Python binary, and registers a LaunchAgent for auto-start.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.whispertype"
VENV="$PREFIX/venv"
APP="$HOME/Applications/WhisperType.app"
AGENT="$HOME/Library/LaunchAgents/com.whispertype.agent.plist"
BUNDLE_ID="com.whispertype.app"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. Use install.bat on Windows."

# ── 1. Python ────────────────────────────────────────────────────────────────
# pyobjc needs >= 3.10, and a python.org framework build is what makes a
# menu-bar app behave. The stock /usr/bin/python3 (3.9) cannot be used.

PY=""
for cand in \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    "$(command -v python3.13 || true)" \
    "$(command -v python3.12 || true)" \
    "$(command -v python3.11 || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { PY="$cand"; break; }
done

if [ -z "$PY" ]; then
    PKG="/tmp/python-3.13.14-macos11.pkg"
    warn "No Python 3.11+ found (the system python3 is $(/usr/bin/python3 -V 2>&1 | cut -d' ' -f2), too old)."
    if [ ! -f "$PKG" ]; then
        say "Downloading the python.org 3.13.14 installer..."
        curl -fL# -o "$PKG" https://www.python.org/ftp/python/3.13.14/python-3.13.14-macos11.pkg
    fi
    echo
    echo "The Python installer needs administrator rights. Run this, then re-run install_mac.sh:"
    echo
    echo "    sudo installer -pkg $PKG -target /"
    echo
    exit 1
fi
say "Using Python: $PY ($("$PY" -V 2>&1))"

# ── 2. Virtualenv + dependencies ─────────────────────────────────────────────

mkdir -p "$PREFIX"
if [ ! -x "$VENV/bin/python3" ]; then
    say "Creating virtualenv at $VENV"
    "$PY" -m venv "$VENV"
else
    say "Virtualenv already exists at $VENV"
fi

say "Installing dependencies (a few hundred MB, takes a while)..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO/requirements-mac.txt"
say "Dependencies installed."

# ── 3. Whisper model ─────────────────────────────────────────────────────────

say "Fetching the default Whisper model (large-v3-turbo, ~1.6 GB, one time)..."
"$VENV/bin/python3" - <<'PY' || warn "Model download failed; it will be retried on first launch."
from huggingface_hub import snapshot_download
snapshot_download("mlx-community/whisper-large-v3-turbo")
PY

# ── 4. .app wrapper ──────────────────────────────────────────────────────────
# The bundle's main executable is a real Mach-O that re-execs the venv python.
# Without a bundle, macOS attaches the Accessibility / Input Monitoring grants
# to the python binary itself, so they silently break whenever the interpreter
# is upgraded and they show up in System Settings as a bare "python3".

say "Building $APP (py2app alias mode)"
rm -rf "$REPO/build" "$REPO/dist"
( cd "$REPO" && "$VENV/bin/python3" setup_mac.py -q py2app -A >/dev/null )
[ -d "$REPO/dist/WhisperType.app" ] || die "py2app did not produce a bundle."

mkdir -p "$HOME/Applications"
rm -rf "$APP"
mv "$REPO/dist/WhisperType.app" "$APP"
rm -rf "$REPO/build" "$REPO/dist"

# Ad-hoc signature with a FIXED identifier: without --identifier codesign
# derives one from the binary, so every rebuild would look like a different app
# to TCC and the user would have to re-grant the permissions each time.
codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP" >/dev/null 2>&1 \
    || warn "codesign failed — permissions may reset when the app is rebuilt."

# ── 5. Config ────────────────────────────────────────────────────────────────

if [ ! -f "$PREFIX/config.json" ]; then
    "$VENV/bin/python3" -c "import sys; sys.path.insert(0,'$REPO'); from whispertype import config; config.load()"
    say "Config created at $PREFIX/config.json"
else
    say "Config already exists at $PREFIX/config.json"
fi

# ── 6. LaunchAgent ───────────────────────────────────────────────────────────

mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>com.whispertype.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP/Contents/MacOS/WhisperType</string>
    </array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <false/>
    <key>ProcessType</key>      <string>Interactive</string>
    <key>StandardErrorPath</key><string>$PREFIX/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$UID/com.whispertype.agent" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$AGENT"
say "LaunchAgent installed — WhisperType starts at login."

# ── 7. Permissions ───────────────────────────────────────────────────────────

cat <<EOF

$(printf '\033[1;32m')Installation complete.$(printf '\033[0m')

WhisperType is running in the menu bar. Before it can do anything, macOS needs
three permissions. Open System Settings > Privacy & Security and add
"WhisperType" (or tick it if it is already listed):

  1. Microphone        — prompted automatically the first time you record
  2. Accessibility     — required to type the transcript into other apps
  3. Input Monitoring  — required for the push-to-talk hotkey

Without 2 and 3 nothing happens and nothing is logged by the system: macOS
silently drops synthetic keystrokes. WhisperType logs the missing grants to
$REPO/voice_daemon.log on startup.

Usage: double-tap Right Command to start dictating, tap it once to stop.
Language, microphone, model and pause are in the menu-bar menu and take effect
immediately. The hotkey lives in $PREFIX/config.json ("push_to_talk_key") and
is the one setting that needs a restart.

To update:     git pull && ./install_mac.sh     (safe to re-run any time)
To uninstall:  ./uninstall_mac.sh

EOF
