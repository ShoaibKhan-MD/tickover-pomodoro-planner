#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="$ROOT/.venv-build"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python was not found. Install Python 3.12 and retry."
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Tickover requires Python 3.10 or newer; Python 3.12 is recommended.")
PY

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt -r requirements-build.txt

rm -rf "$ROOT/build" "$ROOT/dist/Tickover" "$ROOT/dist/Tickover.app" \
  "$ROOT/assets/Tickover.iconset" "$ROOT/assets/Tickover.icns"
mkdir -p "$ROOT/assets/Tickover.iconset" "$ROOT/release/dmg-stage"

sips -z 16 16 assets/Tickover.png --out assets/Tickover.iconset/icon_16x16.png >/dev/null
sips -z 32 32 assets/Tickover.png --out assets/Tickover.iconset/icon_16x16@2x.png >/dev/null
sips -z 32 32 assets/Tickover.png --out assets/Tickover.iconset/icon_32x32.png >/dev/null
sips -z 64 64 assets/Tickover.png --out assets/Tickover.iconset/icon_32x32@2x.png >/dev/null
sips -z 128 128 assets/Tickover.png --out assets/Tickover.iconset/icon_128x128.png >/dev/null
sips -z 256 256 assets/Tickover.png --out assets/Tickover.iconset/icon_128x128@2x.png >/dev/null
sips -z 256 256 assets/Tickover.png --out assets/Tickover.iconset/icon_256x256.png >/dev/null
sips -z 512 512 assets/Tickover.png --out assets/Tickover.iconset/icon_256x256@2x.png >/dev/null
sips -z 512 512 assets/Tickover.png --out assets/Tickover.iconset/icon_512x512.png >/dev/null
sips -z 1024 1024 assets/Tickover.png --out assets/Tickover.iconset/icon_512x512@2x.png >/dev/null
iconutil -c icns assets/Tickover.iconset -o assets/Tickover.icns

"$VENV/bin/pyinstaller" --noconfirm --clean "Tickover.spec"
cp -R "dist/Tickover.app" "$ROOT/release/dmg-stage/"
ln -sfn /Applications "$ROOT/release/dmg-stage/Applications"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) LABEL="Apple-Silicon" ;;
  x86_64) LABEL="Intel" ;;
  *) LABEL="$ARCH" ;;
esac
DMG="$ROOT/release/Tickover-macOS-${LABEL}.dmg"
hdiutil create -volname "Tickover" -srcfolder "$ROOT/release/dmg-stage" -format UDZO -ov "$DMG"
rm -rf "$ROOT/release/dmg-stage"
echo "Created: $DMG"
echo "This build is unsigned and unnotarized. Test it on a clean Mac before sharing."
