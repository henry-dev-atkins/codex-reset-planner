#!/bin/sh
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  exec python3 app.py
else
  echo 'Python 3 is required. Install Python 3.10+ with Tk support.'
  read -r _
fi
