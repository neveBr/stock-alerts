#!/bin/bash
# Double-click to install Stock Alerts as a background app on this Mac.
cd "$(dirname "$0")"
echo "=== Stock Alerts – install ==="
if ! python3 -c "" >/dev/null 2>&1; then
  echo "Python 3 is needed. If a window asks to install the Command Line Tools, click Install, then run this again."
  read -n1 -p "Press any key to close"; exit 1
fi
python3 stock_news.py --install
echo; read -n1 -p "Press any key to close"
