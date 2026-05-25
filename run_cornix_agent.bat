@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo Missing .env. Copy .env.example to .env and edit it first.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python cornix_agent.py

echo.
pause
