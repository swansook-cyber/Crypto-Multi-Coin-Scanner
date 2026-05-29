@echo off
setlocal
call "%~dp0vps_env.bat"
echo Showing latest trades from signals_history.csv on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'cd %VPS_APP_DIR% && if [ -f logs/signals_history.csv ]; then tail -n 25 logs/signals_history.csv; else echo Missing logs/signals_history.csv. Run python review_signals.py --dry-run; fi'"
echo.
pause
