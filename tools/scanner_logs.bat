@echo off
setlocal
call "%~dp0vps_env.bat"
echo Showing latest scanner logs on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'journalctl -u crypto-scanner.service -n 140 --no-pager'"
echo.
pause
