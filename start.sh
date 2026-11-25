#!/bin/zsh
# Opens the Ad Launcher window. Starts the local server first if it isn't already running;
# the server then keeps running quietly in the background so reopening is instant.
cd "$(dirname "$0")"
URL="http://127.0.0.1:${AD_LAUNCHER_PORT:-8790}"
LOG="$HOME/.local/share/ad-launcher/launcher.log"
mkdir -p "${LOG:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo "Not set up yet. Run ./setup.sh in this folder first (see README.md)."; exit 1
fi
if ! curl -s -o /dev/null "$URL/"; then
  nohup .venv/bin/python app/server.py > "$LOG" 2>&1 &
  for i in {1..50}; do curl -s -o /dev/null "$URL/" && break; sleep 0.2; done
fi
# Its own window (Chrome app mode, no tabs or address bar); default browser if Chrome isn't installed
if [[ -d "/Applications/Google Chrome.app" ]]; then
  open -na "Google Chrome" --args --app="$URL" --window-size=1440,960
else
  open "$URL"
fi
