@echo off
if "%VPS_PORT%"=="" set "VPS_PORT=22"
if "%VPS_APP_DIR%"=="" set "VPS_APP_DIR=/opt/Crypto-Multi-Coin-Scanner"
if "%VPS_USER%"=="" set "VPS_USER=root"
if "%VPS_HOST%"=="" (
  echo VPS_HOST is not set.
  echo Example one-time setup:
  echo   setx VPS_HOST 123.123.123.123
  echo   setx VPS_USER root
  echo   setx VPS_PORT 22
  echo.
  set /p VPS_HOST=Enter VPS IP or hostname: 
)
if "%VPS_HOST%"=="" (
  echo VPS host is required.
  pause
  exit /b 1
)
