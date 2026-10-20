@echo off
setlocal enableextensions enabledelayedexpansion
title Flood System - Retrain ML model

cd /d "%~dp0"
set "ROOT=%CD%"
set "VPY=%ROOT%\.venv\Scripts\python.exe"

REM ---------------------------------------------------------------------------
REM  train_model.bat - retrain the flood forecasting model.
REM  Any arguments you pass are forwarded to `python -m ml.train_model`
REM  (e.g. train_model.bat --input my_real_data.csv  or  --generate).
REM ---------------------------------------------------------------------------

echo ===========================================================================
echo   Retraining the flood forecasting model
echo ===========================================================================
echo.

if not exist "%VPY%" (
  echo [ERROR] The virtual environment was not found at:
  echo         %VENV%
  echo         Run run_project.bat once first - it creates .venv and installs
  echo         the dependencies. Then run this file again.
  echo.
  pause
  exit /b 1
)

echo   Default input : data\historical_data.csv
echo   Outputs       : ml\model\flood_model.pkl
echo                   ml\model\feature_meta.json
echo                   ml\model\evaluation_report.json
echo.
echo   To train on genuine data use:
echo       .venv\Scripts\python -m ml.train_model --input YOUR_FILE.csv
echo.
echo ---------------------------------------------------------------------------
echo.

"%VPY%" -m ml.train_model %*
if errorlevel 1 (
  echo.
  echo [ERROR] Training failed. Scroll up for details.
  pause
  exit /b 1
)

echo.
echo ---------------------------------------------------------------------------
echo   Training finished successfully.
echo   If the backend is currently running, restart it (close the
echo   "Flood Backend" window and re-run run_project.bat) so it reloads the model.
echo ---------------------------------------------------------------------------
echo.
pause
endlocal
