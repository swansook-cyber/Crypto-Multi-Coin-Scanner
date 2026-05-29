@echo off
setlocal
call "%~dp0vps_env.bat"
echo Pulling latest GitHub code and restarting services on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'cd %VPS_APP_DIR% && git pull origin main && .venv/bin/python -m compileall -q . && sudo systemctl restart crypto-scanner.service && sudo systemctl restart crypto-outcome-checker.timer && sudo systemctl restart crypto-daily-summary.timer && echo Update complete && systemctl status crypto-scanner.service --no-pager'"
echo.
pause
