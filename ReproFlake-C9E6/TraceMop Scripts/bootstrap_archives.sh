#!/usr/bin/env bash
# Downloads and extracts the FULL_RUNS_RV / FULL_RUNS_NO_RV archive bundle
# from Zenodo into ReproFlake-C9E6/data/. Idempotent — re-running is a
# no-op once data/.bundle_extracted exists. Delete that sentinel to force
# re-fetch.
set -euo pipefail

ZENODO_URL="https://zenodo.org/records/20100590/files/RV_NRV.zip?download=1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA="$ROOT/data"
SENTINEL="$DATA/.bundle_extracted"

if [[ -f "$SENTINEL" ]]; then
  echo "[bootstrap] archives already extracted (rm $SENTINEL to re-fetch)"
  exit 0
fi

for tool in curl unzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: '$tool' not found on PATH" >&2
    exit 1
  fi
done

mkdir -p "$DATA"
TMP_ZIP="$DATA/.bundle.zip.partial"
TMP_DIR="$(mktemp -d "$DATA/.bundle.XXXXXX")"
trap 'rm -rf "$TMP_DIR" "$TMP_ZIP"' EXIT

echo "[bootstrap] downloading zipped containers from Zenodo (~2.4 GB)..."
curl -L --fail --progress-bar -o "$TMP_ZIP" "$ZENODO_URL"

echo "[bootstrap] unzipping..."
unzip "$TMP_ZIP" -d "$TMP_DIR" | awk '
  /^  inflating:|^   creating:|^ extracting:/ {
    n++
    if (n % 500 == 0) printf "\r[bootstrap] extracted %d files...", n
  }
  END { printf "\r[bootstrap] extracted %d files total.   \n", n }
'

# Locate FULL_RUNS_RV and FULL_RUNS_NO_RV. They may sit at the top level
# of the bundle, or nested one level deeper inside a wrapper folder.
# Exclude macOS metadata (__MACOSX) from the search.
find_dir() {
  find "$TMP_DIR" -maxdepth 3 -type d -name "$1" -not -path "*/__MACOSX/*" 2>/dev/null | head -n 1
}
RV_PATH="$(find_dir FULL_RUNS_RV)"
NO_RV_PATH="$(find_dir FULL_RUNS_NO_RV)"

if [[ -z "$RV_PATH" || -z "$NO_RV_PATH" ]]; then
  echo "ERROR: could not locate FULL_RUNS_RV or FULL_RUNS_NO_RV in the unzipped bundle" >&2
  exit 1
fi
if [[ "$(dirname "$RV_PATH")" != "$(dirname "$NO_RV_PATH")" ]]; then
  echo "ERROR: FULL_RUNS_RV and FULL_RUNS_NO_RV have different parent dirs in the bundle" >&2
  exit 1
fi

SOURCE="$(dirname "$RV_PATH")"
for name in FULL_RUNS_RV FULL_RUNS_NO_RV; do
  mkdir -p "$DATA/$name"
  shopt -s dotglob nullglob
  for child in "$SOURCE/$name"/*; do
    mv "$child" "$DATA/$name/"
  done
  shopt -u dotglob nullglob
done

touch "$SENTINEL"
echo "[bootstrap] done. Containers populated under data/FULL_RUNS_RV/ and data/FULL_RUNS_NO_RV/."
