@echo off
setlocal enableextensions enabledelayedexpansion
title Flood Prediction and Early Warning System - Launcher

REM ---------------------------------------------------------------------------
REM  run_project.bat - one-click launcher
REM    1) locates a working Python 3 interpreter
REM    2) prepares a project-local virtual environment + dependencies
REM    3) trains the ML model if it is missing, verifies it is loadable
REM    4) detects this PC's network IPv4 (for the ESP32)
REM    5) starts FastAPI on 0.0.0.0:8000 in its own window (unless already up)
REM    6) waits for /health, then opens the dashboard
REM ---------------------------------------------------------------------------

cd /d "%~dp0"
set "ROOT=%CD%"
set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "ESP_IP="
set "BACKEND_ALREADY="

echo ===========================================================================
echo   Flood Prediction and Early Warning System
echo   Project root: %ROOT%
echo ===========================================================================
echo.

REM ------------------------------------------------------------------ [1/8] --
set "PY_CMD="
py -3.13 --version >nul 2>&1 && set "PY_CMD=py -3.13"
if not defined PY_CMD ( py -3.12 --version >nul 2>&1 && set "PY_CMD=py -3.12" )
if not defined PY_CMD ( py -3 --version >nul 2>&1 && set "PY_CMD=py -3" )
if not defined PY_CMD ( python --version >nul 2>&1 && set "PY_CMD=python" )
if not defined PY_CMD (
  echo [ERROR] No working Python 3 interpreter was found.
  echo         Install Python 3.10 - 3.13 from https://www.python.org/downloads/
  echo         and tick "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)
echo [1/8] Python interpreter: %PY_CMD%
%PY_CMD% --version

REM ------------------------------------------------------------ venv + deps --
if not exist "%VPY%" (
  echo [2/8] Creating the local virtual environment ^(.venv^) ...
  %PY_CMD% -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] Failed to create the virtual environment.
    pause
    exit /b 1
  )
) else (
  echo [2/8] Using the existing virtual environment ^(.venv^).
)

"%VPY%" -c "import fastapi, uvicorn, pydantic, sklearn, numpy, joblib" >nul 2>&1
if errorlevel 1 (
  echo [3/8] Installing dependencies ^(first run only - this can take a few minutes^) ...
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Scroll up for the pip error.
    pause
    exit /b 1
  )
) else (
  echo [3/8] Dependencies already installed.
)

REM ------------------------------------------------------------- ML model ----
REM  The normal launcher never retrains an existing model (it would waste time
REM  and is unnecessary). It only trains when the model file is missing.
if not exist "%ROOT%\ml\model\flood_model.pkl" (
  echo [4/8] No trained model found - training it now ^(one-off^) ...
  "%VPY%" -m ml.train_model
  if errorlevel 1 (
    echo [WARN] Model training failed. The backend will still start, but forecasts
    echo        will be reported as "unavailable" until you run train_model.bat.
  )
) else (
  echo [4/8] ML model found. Verifying it loads ...
  "%VPY%" -c "import sys; from ml.predict import get_predictor; sys.exit(0 if get_predictor().is_available else 1)" >nul 2>&1
  if errorlevel 1 (
    echo [WARN] The model file exists but could not be loaded. Forecasts will be
    echo        reported as "unavailable". Run train_model.bat to rebuild it.
  ) else (
    echo       Model loaded OK. ^(Use train_model.bat to retrain.^)
  )
)

REM ------------------------------------------------------------- Database ----
if not exist "%ROOT%\flood_system.db" (
  echo [5/8] Database not found - it will be created automatically on startup.
) else (
  echo [5/8] Database found.
)

REM --------------------------------------------------------- Email config ----
REM  Displays email configuration status WITHOUT ever printing the password.
"%VPY%" -c "from backend.services import email_service as m; es=m.email_service; ok,reason=es.validate_config(); s=es.sender or ''; mask=(s[:2]+'*'*(len(s)-4)+s[-2:]) if len(s)>4 else '***'; print('EMAIL_STATUS=' + ('CONFIGURED' if ok else 'NOT_CONFIGURED') + '|' + mask + '|' + es.host + ':' + str(es.port) + '|' + ('' if ok else reason))" > "%TEMP%\flood_email.txt" 2>nul
set "EMAIL_LINE="
for /f "tokens=2 delims==" %%A in ('findstr /b "EMAIL_STATUS=" "%TEMP%\flood_email.txt"') do set "EMAIL_LINE=%%A"
del "%TEMP%\flood_email.txt" >nul 2>&1
if defined EMAIL_LINE (
  echo       Email alerts: !EMAIL_LINE!
) else (
  echo       Email alerts: NOT CONFIGURED ^(see config.txt^)
)

REM ---------------------------------------------------------- port / reuse ----
echo [6/8] Checking port 8000 ...
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  REM  Something is listening on 8000. Is it our own backend?
  powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:8000/health; if ($r.StatusCode -eq 200) { exit 0 } else { exit 2 } } catch { exit 2 }" >nul 2>&1
  if not errorlevel 1 (
    set "BACKEND_ALREADY=1"
    echo       A healthy Flood backend is already running on port 8000 - reusing it.
  ) else (
    echo.
    echo [ERROR] Port 8000 is already in use by a DIFFERENT program.
    echo         The launcher will NOT kill it. Either:
    echo           * close that program, or
    echo           * run the backend on another port, e.g.:
    echo               set FLOOD_PORT=8080 ^&^& "%VPY%" -m uvicorn backend.main:app --port 8080
    echo         Then find it at http://localhost:8080 .
    echo.
    pause
    exit /b 1
  )
) else (
  echo       Port 8000 is free.
)

REM ---------------------------------------------------------------- IP -------
REM  NOTE: the IPv4 is captured into a temp file first, then read with findstr.
REM  Running a *quoted* python path piped directly into findstr inside for /f
REM  mis-parses the quotes in cmd.exe and silently yields nothing.
echo [7/8] Detecting this computer's network IPv4 address ...
set "IPTMP=%TEMP%\flood_local_ip.txt"
"%VPY%" tools\local_ip.py > "%IPTMP%" 2>&1
for /f "tokens=2 delims==" %%A in ('findstr /b "PRIMARY_IP=" "%IPTMP%"') do set "ESP_IP=%%A"
if defined ESP_IP (
  if "!ESP_IP!"=="" set "ESP_IP="
)
del "%IPTMP%" >nul 2>&1

REM ------------------------------------------------------------- backend -----
if not defined BACKEND_ALREADY (
  echo [8/8] Starting the FastAPI backend on 0.0.0.0:8000 in a new window ...
  start "Flood Backend (FastAPI)" cmd /k call "%ROOT%\tools\serve_backend.bat"
) else (
  echo [8/8] Backend already running - not starting a second copy.
)

echo.
echo Waiting for the backend to become ready ...
powershell -NoProfile -Command "for ($i=0; $i -lt 40; $i++) { try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/health; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
  echo [WARN] The backend did not report healthy within 40 seconds.
  echo        Check the "Flood Backend (FastAPI)" window for the error.
) else (
  echo Backend is healthy.
)

start "" http://localhost:8000

echo.
echo ===========================================================================
echo   RUNNING
echo ---------------------------------------------------------------------------
echo   Dashboard (this PC)   : http://localhost:8000
echo   Health check          : http://localhost:8000/health
echo   API docs (Swagger)    : http://localhost:8000/docs
if defined ESP_IP (
  echo.
  echo   THIS PC's IP          : %ESP_IP%
  echo   ESP32 BACKEND URL     : http://%ESP_IP%:8000/api/sensors/data
  echo   ^(put BACKEND_HOST=%ESP_IP% in hardware\esp32\wifi_config.h^)
) else (
  echo   [WARN] Could not detect an IPv4 address automatically.
  echo          Run:  .venv\Scripts\python tools\local_ip.py
)
echo ---------------------------------------------------------------------------
echo   IMPORTANT
echo   * Connect BOTH the ESP32 and this PC to the SAME mobile hotspot.
echo   * The ESP32 must use THIS PC's IP above - never 127.0.0.1.
echo   * Windows Firewall may ask to allow Python - choose to allow on
echo     Private networks, or add an inbound TCP rule for port 8000.
echo   * If the ESP32 cannot connect, check: same network, correct IP,
echo     firewall, and that hotspot "client isolation" is disabled.
echo   * Close the "Flood Backend" window ^(or run STOP.bat^) to stop.
echo ===========================================================================
echo.
echo Press any key to close THIS launcher window ^(the backend keeps running^).
pause >nul
endlocal
