@echo off
REM ---------------------------------------------------------------------------
REM  serve_backend.bat - run the FastAPI backend in a dedicated console window.
REM  Launched by run_project.bat via:  start "..." cmd /k call "tools\serve_backend.bat"
REM  Keeping the uvicorn command in its own file avoids fragile nested-quote
REM  parsing inside the parent launcher.
REM ---------------------------------------------------------------------------
setlocal enableextensions
cd /d "%~dp0.."
set "ROOT=%CD%"
set "VPY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"

echo ===========================================================================
echo   Flood Prediction and Early Warning System - Backend (FastAPI / uvicorn)
echo   Serving on 0.0.0.0:8000
echo   Dashboard: http://localhost:8000
echo   API docs : http://localhost:8000/docs
echo ---------------------------------------------------------------------------
echo   Close this window (or run STOP.bat) to stop the backend.
echo ===========================================================================
echo.

"%VPY%" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

echo.
echo [Flood backend has exited with code %errorlevel%.]
echo Press any key to close this window.
pause >nul
endlocal
