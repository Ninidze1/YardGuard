@echo off
rem Runs a YardGuard script with the app's own Python, all caches kept inside this folder.
rem   run.cmd tests\test_engine.py
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" goto missing
set "PIP_CACHE_DIR=%ROOT%.cache\pip"
set "TORCH_HOME=%ROOT%.cache\torch"
set "YOLO_CONFIG_DIR=%ROOT%.cache\ultralytics"
set "MPLCONFIGDIR=%ROOT%.cache\matplotlib"
set "TEMP=%ROOT%.cache"
set "TMP=%ROOT%.cache"
cd /d "%ROOT%"
"%PY%" %*
exit /b %errorlevel%

:missing
echo YardGuard is not installed yet: double-click YardGuard.cmd first.
exit /b 1
