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
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

call ".venv\Scripts\activate"
python review_signals.py
