@echo off
setlocal
call "%~dp0vps_env.bat"
echo Running health check on %VPS_USER%@%VPS_HOST%...
ssh -p "%VPS_PORT%" "%VPS_USER%@%VPS_HOST%" "bash -lc 'echo === SERVER ===; hostname; date; uptime; echo; echo === DISK ===; df -h /; echo; echo === SERVICES ===; echo scanner=$(systemctl is-active crypto-scanner.service); echo outcome_timer=$(systemctl is-active crypto-outcome-checker.timer); echo daily_timer=$(systemctl is-active crypto-daily-summary.timer); echo; echo === APP FILES ===; cd %VPS_APP_DIR% && pwd && test -f .env && echo .env=OK || echo .env=MISSING; test -f logs/signals.csv && echo signals.csv=OK || echo signals.csv=MISSING; test -f logs/signals_history.csv && echo signals_history.csv=OK || echo signals_history.csv=MISSING; test -f logs/performance_report.txt && echo performance_report=OK || echo performance_report=MISSING; echo; echo === PYTHON COMPILE ===; .venv/bin/python -m compileall -q . && echo python_compile=OK || echo python_compile=FAILED'"
echo.
pause
