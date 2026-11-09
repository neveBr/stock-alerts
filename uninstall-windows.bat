@echo off
rem Double-click to stop Stock Alerts and remove it from startup.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 stock_news.py --uninstall & goto done)
python stock_news.py --uninstall
:done
pause
