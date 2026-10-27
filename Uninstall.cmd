@echo off
rem Removes YardGuard and everything it downloaded from this PC. It asks before deleting anything.
rem   Uninstall.cmd --dry-run   only lists what would be removed
setlocal
set "ROOT=%~dp0"
rem Work from the drive root so this folder is not "in use" while it is deleted
cd /d "%~d0\"
set "PY=%ROOT%runtime\python\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" goto nopython
"%PY%" -I "%ROOT%uninstall.py" %* & exit /b

:nopython
echo YardGuard's Python was not found, so nothing was installed by YardGuard here.
echo To remove YardGuard, close it and delete this folder:
echo   %ROOT%
pause
