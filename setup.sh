#!/bin/zsh
# One-time setup: installs what the launcher needs into this folder and creates your settings file.
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python 3 isn't installed. Install it from https://www.python.org/downloads/ and run this again."; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is needed (you have $(python3 --version)). Install a newer one from https://www.python.org/downloads/"; exit 1
fi
echo "Installing into .venv (about a minute)…"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p batches
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env"
fi
chmod +x start.sh install_app.sh "Open Launcher.command"
echo
echo "Done. Next:"
echo "  1. Open .env in a text editor and paste in your token and ad account ID (README, 'Get your Meta token')."
echo "  2. Run:  .venv/bin/python check_setup.py"
