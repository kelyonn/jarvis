#!/usr/bin/env bash
# One-shot install for Jarvis on Apple Silicon macOS.
#
#   ./scripts/install.sh                  full install
#   ./scripts/install.sh --no-shell       skip building Jarvis.app (Python side only)
#   ./scripts/install.sh --no-models      skip pre-downloading the speech models
#
# Result: ~/.jarvis/venv (the Python the Shell launches), `jarvisd` on a stable path, ~/Applications/Jarvis.app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${JARVIS_HOME:-$HOME/.jarvis}"
VENV="${JARVIS_VENV:-$HOME_DIR/venv}"
BIN_DIR="${JARVIS_BIN:-$HOME/.local/bin}"
WITH_SHELL=1 WITH_MODELS=1
for arg in "$@"; do
  case "$arg" in
    --no-shell) WITH_SHELL=0 ;;
    --no-models) WITH_MODELS=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1; }

step "Checking the machine"
[[ "$(uname -s)" == "Darwin" ]] || { echo "Jarvis is macOS only." >&2; exit 1; }
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "warning: Apple Silicon is expected (mlx-whisper needs it). Continuing, but speech-to-text will fall back." >&2
fi
if ! xcode-select -p >/dev/null 2>&1 && [[ $WITH_SHELL -eq 1 ]]; then
  echo "Xcode command line tools are needed to build the Shell. Run: xcode-select --install" >&2
  exit 1
fi
need brew || { echo "Homebrew is required: https://brew.sh" >&2; exit 1; }

step "Brew tools (uv, blueutil, brightness)"
for formula in uv blueutil brightness; do
  if need "$formula"; then echo "  $formula: present"; else brew install "$formula"; fi
done

step "Python environment at $VENV"
mkdir -p "$HOME_DIR"
if [[ ! -x "$VENV/bin/python" ]]; then
  uv venv --python 3.12 "$VENV"
fi
export VIRTUAL_ENV="$VENV"
uv pip install --python "$VENV/bin/python" -e "$ROOT[voice,markets]"
# Voice recognition (only your voice is acted on). --no-deps: its audio helpers need librosa/webrtcvad, which we do not use.
uv pip install --python "$VENV/bin/python" --no-deps resemblyzer

step "Putting jarvisd on your PATH ($BIN_DIR)"
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/jarvisd" "$BIN_DIR/jarvisd"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  add this to your shell profile:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

if [[ $WITH_MODELS -eq 1 ]]; then
  step "Downloading speech models (wake word, Whisper, Kokoro; a few GB, once)"
  "$VENV/bin/jarvisd" doctor --fix >/dev/null 2>&1 || echo "  (model download can be retried later with: jarvisd doctor --fix)"
fi

if [[ $WITH_SHELL -eq 1 ]]; then
  step "Building Jarvis.app"
  "$ROOT/scripts/build_shell.sh"
fi

step "Done"
cat <<EOF
Next:
  1. jarvisd set-key        # NVIDIA NIM key (free at build.nvidia.com)
  2. jarvisd setup          # name, permissions, Google, VIPs, brief times
  3. open ~/Applications/Jarvis.app    # registers itself to start at login
  4. jarvisd doctor         # should end with 0 broken
EOF
