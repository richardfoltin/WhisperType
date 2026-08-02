#!/bin/bash
# WhisperType — macOS uninstaller.
#
# Removes everything install_mac.sh created. The model cache and your
# transcription history are only removed if you ask for them, because the model
# is a ~1.6 GB download you may want to keep.
set -euo pipefail

PREFIX="$HOME/.whispertype"
APP="$HOME/Applications/WhisperType.app"
AGENT="$HOME/Library/LaunchAgents/com.whispertype.agent.plist"
HF_CACHE="$HOME/.cache/huggingface/hub"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
gone() { printf '    removed %s\n' "$*"; }

PURGE_MODELS=0
KEEP_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --purge-models) PURGE_MODELS=1 ;;
        --keep-config)  KEEP_CONFIG=1 ;;
        -h|--help)
            cat <<EOF
Usage: ./uninstall_mac.sh [--purge-models] [--keep-config]

  --purge-models   also delete the downloaded Whisper models (~1.6 GB each)
  --keep-config    keep ~/.whispertype/config.json and history.json
EOF
            exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

say "Stopping WhisperType"
launchctl bootout "gui/$UID/com.whispertype.agent" >/dev/null 2>&1 || true
pkill -f "WhisperType.app/Contents/MacOS" >/dev/null 2>&1 || true

[ -f "$AGENT" ] && rm -f "$AGENT" && gone "$AGENT"
[ -d "$APP" ]   && rm -rf "$APP"   && gone "$APP"

if [ -d "$PREFIX/venv" ]; then
    rm -rf "$PREFIX/venv"
    gone "$PREFIX/venv"
fi

if [ "$KEEP_CONFIG" -eq 0 ]; then
    rm -f "$PREFIX/config.json" "$PREFIX/history.json" "$PREFIX/launchd.err.log"
    gone "$PREFIX/config.json, history.json"
    rmdir "$PREFIX" 2>/dev/null || true
else
    say "Kept $PREFIX/config.json and history.json"
fi

if [ "$PURGE_MODELS" -eq 1 ]; then
    for d in "$HF_CACHE"/models--mlx-community--whisper-*; do
        [ -e "$d" ] || continue
        rm -rf "$d"
        gone "$(basename "$d")"
    done
else
    say "Kept the downloaded models. Remove them with --purge-models,"
    say "or by hand from $HF_CACHE/models--mlx-community--whisper-*"
fi

cat <<EOF

$(printf '\033[1;32m')WhisperType removed.$(printf '\033[0m')

Two things this script cannot do for you, because macOS protects them:

  * The privacy permissions. Open System Settings > Privacy & Security and
    remove WhisperType from Accessibility, Input Monitoring and Microphone.
  * The source checkout you are standing in — delete it yourself if you are done.

EOF
