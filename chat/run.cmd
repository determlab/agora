@echo off
REM Our own chat server (issues #61, #72): set up and serve on http://127.0.0.1:8095.
REM Ctrl+C to stop. Extra flags pass through, e.g.  run.cmd --port 8096 --no-update
REM --dashboard-cmd (issue #81): the server runs this PC's ops dashboard every 5 minutes.
REM --approve-cmd (ops#176): the approvals carousel's Yes/No/Later, same PC, same tools/.
REM Forward slashes: the server splits the command with shlex, which eats backslashes.
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=py
set DASH_PY=%PY:\=/%
cd /d "%~dp0.."
"%PY%" chat\server.py up --dashboard-cmd "\"%DASH_PY%\" C:/PlayGround/ops/tools/dashboard.py --json --no-tokens" --approve-cmd "\"%DASH_PY%\" C:/PlayGround/ops/tools/approve.py" %*
