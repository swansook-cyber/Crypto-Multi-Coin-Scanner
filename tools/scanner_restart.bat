@echo off
setlocal
call "%~dp0vps_env.bat"
echo Restarting scanner service on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'sudo systemctl restart crypto-scanner.service && sleep 2 && systemctl status crypto-scanner.service --no-pager'"
echo.
pause
