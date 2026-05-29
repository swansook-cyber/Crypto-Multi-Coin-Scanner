@echo off
setlocal
call "%~dp0vps_env.bat"
echo Opening performance report on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'cd %VPS_APP_DIR% && if [ -f logs/performance_report.txt ]; then tail -n 220 logs/performance_report.txt; else echo Missing logs/performance_report.txt. Run python stats_dashboard.py; fi'"
echo.
pause
