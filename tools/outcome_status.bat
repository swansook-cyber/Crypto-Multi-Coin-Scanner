@echo off
setlocal
call "%~dp0vps_env.bat"
echo Checking outcome checker timer/service on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'systemctl status crypto-outcome-checker.timer --no-pager; echo; systemctl status crypto-outcome-checker.service --no-pager; echo; systemctl list-timers crypto-outcome-checker.timer --no-pager'"
echo.
pause
