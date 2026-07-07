#!/usr/bin/env bash
# One-command setup for FlakyDoctor's Claude (agentic-style) runner.
# Mirrors AF_Claude_Agent's setup.sh ergonomics. Linux-first (macOS also works).
#
# Usage (from anywhere):
#   ANTHROPIC_API_KEY=sk-ant-... bash FlakyDoctor/setup.sh [--build-images] [--force-rebuild-images]
#
# It:
#   - creates FlakyDoctor/.venv;
#   - installs FlakyDoctor/requirements.txt (Claude-only deps);
#   - stores the key in FlakyDoctor/.anthropic_api_key (git-ignored) if ANTHROPIC_API_KEY is set;
#   - checks Docker; and
#   - with --build-images, prebuilds the repair images (od8/od11 + their no-testorder ID variants).
#
# NOTE: this is the venv/Claude-runner setup. The original system installer
# (apt + pip for the full GPT-4/MagicCoder pipeline) is still src/setup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FD_DIR="$SCRIPT_DIR"
VENV="$FD_DIR/.venv"
KEY_FILE="$FD_DIR/.anthropic_api_key"
DOCKERFILE="$FD_DIR/docker/Dockerfile.flakydoctor_od"

BUILD_IMAGES=0
FORCE_REBUILD=0
for arg in "$@"; do
    case "$arg" in
        --build-images)          BUILD_IMAGES=1 ;;
        --force-rebuild-images)  FORCE_REBUILD=1 ;;
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

# 1. venv + Python deps
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "[setup] ERROR: python3 not found (install python3 + python3-venv)" >&2; exit 1; }
echo "[setup] creating virtualenv at $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$FD_DIR/requirements.txt"

# 2. API key (env wins; file is the persistent store)
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    ( umask 177; printf '%s\n' "$ANTHROPIC_API_KEY" > "$KEY_FILE" )
    echo "[setup] stored key in $KEY_FILE (mode 600)"
else
    echo "[setup] ANTHROPIC_API_KEY not set — put your key in $KEY_FILE before repairing"
fi

# 3. Docker check
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "[setup] docker OK: $(docker --version)"
else
    echo "[setup] WARNING: docker not installed/running — required for repair runs"
fi

# 4. Optional image prebuild
if [[ "$BUILD_IMAGES" == "1" ]]; then
    build_one() {  # name base with_testorder
        local name="$1" base="$2" wto="$3"
        if [[ "$FORCE_REBUILD" != "1" ]] && docker image inspect "$name" >/dev/null 2>&1; then
            echo "[setup] image $name exists — skip (use --force-rebuild-images to rebuild)"
            return
        fi
        echo "[setup] building $name (base=$base testorder=$wto) ..."
        docker build -t "$name" --build-arg "BASE=$base" --build-arg "WITH_TESTORDER=$wto" \
            -f "$DOCKERFILE" "$FD_DIR"
    }
    build_one flakydoctor-od8       maven:3.8.6-openjdk-8   true
    build_one flakydoctor-od11      maven:3.8.6-openjdk-11  true
    build_one flakydoctor-od8-noto  maven:3.8.6-openjdk-8   false
    build_one flakydoctor-od11-noto maven:3.8.6-openjdk-11  false
fi

echo "[setup] done. Example run:"
echo "  $VENV/bin/python $FD_DIR/agentic/run_agentic.py <container> --runs 1 --models claude --max-iterations 10"
