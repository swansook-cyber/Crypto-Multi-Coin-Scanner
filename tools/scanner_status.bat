@echo off
setlocal
call "%~dp0vps_env.bat"
echo Checking scanner service on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'systemctl status crypto-scanner.service --no-pager; echo; echo Active: $(systemctl is-active crypto-scanner.service); echo Enabled: $(systemctl is-enabled crypto-scanner.service 2>/dev/null || true)'"
echo.
pause
