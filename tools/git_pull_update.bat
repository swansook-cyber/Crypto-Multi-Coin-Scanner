@echo off
setlocal
call "%~dp0vps_env.bat"
echo Pulling latest GitHub code and restarting services on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'cd %VPS_APP_DIR% && git pull origin main && .venv/bin/python -m compileall -q . && sudo cp deploy/systemd/crypto-performance-report.service /etc/systemd/system/ && sudo cp deploy/systemd/crypto-performance-report.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart crypto-scanner.service && sudo systemctl restart crypto-outcome-checker.timer && sudo systemctl restart crypto-daily-summary.timer && sudo systemctl enable --now crypto-performance-report.timer && (sudo systemctl restart crypto-external-inbox.service || echo crypto-external-inbox.service not installed) && echo Update complete && systemctl status crypto-scanner.service --no-pager && systemctl status crypto-performance-report.timer --no-pager && (systemctl status crypto-external-inbox.service --no-pager || true)'"
echo.
pause
