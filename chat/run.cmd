@echo off
REM Our own chat server (issue #61) on http://127.0.0.1:8095. Ctrl+C to stop.
REM Extra flags pass through, e.g.  run.cmd --seed-ids 600
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=py
cd /d "%~dp0.."
"%PY%" chat\server.py serve --port 8095 %*
