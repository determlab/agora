@echo off
REM Our own chat server (issues #61, #72): set up and serve on http://127.0.0.1:8095.
REM Ctrl+C to stop. Extra flags pass through, e.g.  run.cmd --port 8096 --no-update
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=py
cd /d "%~dp0.."
"%PY%" chat\server.py up %*
