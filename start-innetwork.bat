@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  InNetwork launcher (Windows)
REM  Double-click this file. It starts the InNetwork backend, which serves the
REM  web page AND queries the official CMS registry server-side (no browser
REM  CORS limits). Your browser opens automatically. Keep this window open
REM  while you use InNetwork; close it to stop the server.
REM ─────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"
title InNetwork

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   Starting InNetwork on http://localhost:8000
echo   Your browser will open in a few seconds...
echo   (Keep this window open. Close it to stop InNetwork.)
echo.

REM Open the browser shortly after the server starts.
start "" /b cmd /c "timeout /t 3 >nul & start "" http://localhost:8000"

"%PY%" -m uvicorn app.main:app --port 8000
echo.
echo   InNetwork has stopped. You can close this window.
pause
