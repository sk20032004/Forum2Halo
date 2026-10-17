@echo off
cd /d "%~dp0"
echo.
echo  Forum2Halo console: http://127.0.0.1:8765
echo  Close this window to stop.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo [1/2] First run: creating venv...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q --upgrade pip requests beautifulsoup4 lxml flask
)
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" app.py
pause
