#!/bin/sh
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo "First run: installing a small helper called uv. This takes about a minute."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
export OPEN_BROWSER=1
echo "Starting Tickover. Keep this window open while you study. Press Ctrl+C to stop."
uv run app.py
