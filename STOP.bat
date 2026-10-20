@echo off
setlocal enableextensions enabledelayedexpansion
title Flood System - Stop backend

REM ---------------------------------------------------------------------------
REM  STOP.bat - stop ONLY this project's backend.
REM
REM  It does NOT blindly kill whatever owns port 8000. It kills only python
REM  processes whose command line contains BOTH "uvicorn" and
REM  "backend.main:app" (i.e. this project's server), so unrelated programs
REM  that happen to use port 8000 are left untouched.
REM ---------------------------------------------------------------------------

cd /d "%~dp0"
set "STOPPED=0"

echo Stopping the Flood system backend (uvicorn backend.main:app) ...
echo.

for /f "usebackq tokens=*" %%P in (`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'backend.main:app' } | Select-Object -ExpandProperty ProcessId"`) do (
  if not "%%P"=="" (
    echo   stopping PID %%P
    taskkill /PID %%P /T /F >nul 2>&1
    set "STOPPED=1"
  )
)

echo.
if "!STOPPED!"=="0" (
  echo   No running Flood backend was found - nothing to stop.
) else (
  echo   Flood backend stopped. The dashboard and ESP32 ingestion are now offline.
)
echo   ^(You can also just close the "Flood Backend" console window.^)
echo.
timeout /t 3 /nobreak >nul 2>&1
endlocal
