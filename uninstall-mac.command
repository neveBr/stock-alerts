#!/bin/bash
# Double-click to stop Stock Alerts and remove it from startup.
cd "$(dirname "$0")"
python3 stock_news.py --uninstall
echo; read -n1 -p "Press any key to close"
