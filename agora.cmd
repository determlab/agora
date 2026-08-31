@echo off
REM Start Agora and open the browser. Ctrl+C to stop.
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=py
cd /d "%~dp0"
"%PY%" -m agora.server %*
