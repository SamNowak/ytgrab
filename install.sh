#!/usr/bin/env bash
# One-time setup: virtualenv + dependencies. Re-run any time to update yt-dlp
# (YouTube changes often; an outdated yt-dlp is the #1 cause of failures).
set -e
cd "$(dirname "$(readlink -f "$0")")"
if ! python3 -c "import ensurepip" 2>/dev/null; then
  echo "Installing python3-venv (needs sudo)..."
  sudo apt-get update -qq && sudo apt-get install -y python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --upgrade -r requirements.txt
chmod +x ytgrab ytgrab.py
echo "Ready. Run: $(pwd)/ytgrab --help"
echo "Optional, to run 'ytgrab' from anywhere:  sudo ln -sf $(pwd)/ytgrab /usr/local/bin/ytgrab"
