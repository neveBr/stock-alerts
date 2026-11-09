@echo off
rem Double-click to install Stock Alerts as a background app on this PC.
cd /d "%~dp0"
echo === Stock Alerts - install ===
where py >nul 2>nul && (py -3 stock_news.py --install & goto done)
python --version >nul 2>nul && (python stock_news.py --install & goto done)
echo Python 3 is not installed. Install it from python.org - tick "Add python.exe to PATH" - then run this again.
start https://www.python.org/downloads/
:done
pause
