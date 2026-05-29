@echo off
setlocal
call "%~dp0vps_env.bat"

:menu
cls
echo ====================================
echo VelaFlow Scanner Control Center
echo ===============================
echo.
echo Connected VPS: %VPS_HOST%
echo User: %VPS_USER%
echo.
echo 1. Scanner Status
echo 2. Scanner Logs
echo 3. Restart Scanner
echo 4. Pull Latest Code
echo 5. Outcome Checker Status
echo 6. Performance Report
echo 7. Daily Summary
echo 8. Health Check
echo 9. Exit
echo.
set /p choice=Select: 

if "%choice%"=="1" goto scanner_status
if "%choice%"=="2" goto scanner_logs
if "%choice%"=="3" goto restart_scanner
if "%choice%"=="4" goto pull_latest
if "%choice%"=="5" goto outcome_status
if "%choice%"=="6" goto performance_report
if "%choice%"=="7" goto daily_summary
if "%choice%"=="8" goto health_check
if "%choice%"=="9" goto exit_app

echo.
echo Invalid choice. Please select 1-9.
pause
goto menu

:scanner_status
cls
echo Scanner Status
echo --------------
ssh %VPS_USER%@%VPS_HOST% "systemctl status crypto-scanner"
pause
goto menu

:scanner_logs
cls
echo Scanner Logs
echo ------------
ssh %VPS_USER%@%VPS_HOST% "journalctl -u crypto-scanner -n 50"
pause
goto menu

:restart_scanner
cls
echo Restart Scanner
echo ---------------
ssh %VPS_USER%@%VPS_HOST% "systemctl restart crypto-scanner && systemctl status crypto-scanner"
pause
goto menu

:pull_latest
cls
echo Pull Latest Code
echo ----------------
ssh %VPS_USER%@%VPS_HOST% "cd /opt/Crypto-Multi-Coin-Scanner && git pull && systemctl restart crypto-scanner"
pause
goto menu

:outcome_status
cls
echo Outcome Checker Status
echo ----------------------
ssh %VPS_USER%@%VPS_HOST% "systemctl status crypto-outcome-checker.timer"
pause
goto menu

:performance_report
cls
echo Performance Report
echo ------------------
ssh %VPS_USER%@%VPS_HOST% "cd /opt/Crypto-Multi-Coin-Scanner && cat logs/performance_report.txt"
pause
goto menu

:daily_summary
cls
echo Daily Summary
echo -------------
ssh %VPS_USER%@%VPS_HOST% "cd /opt/Crypto-Multi-Coin-Scanner && python3 daily_summary.py"
pause
goto menu

:health_check
cls
echo Health Check
echo ------------
ssh %VPS_USER%@%VPS_HOST% "systemctl is-active crypto-scanner && systemctl is-active crypto-outcome-checker.timer"
pause
goto menu

:exit_app
echo.
echo Goodbye.
exit /b 0
