@echo off
setlocal
call "%~dp0vps_env.bat"
echo Showing equity curve on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'cd %VPS_APP_DIR% && if [ -f logs/equity_curve.csv ]; then tail -n 30 logs/equity_curve.csv; else echo Missing logs/equity_curve.csv. Run python review_signals.py --dry-run; fi'"
echo.
pause
