@echo off
rem YardGuard: double-click this file to start.
rem The first time, it installs everything YardGuard needs into this folder (setup\bootstrap.ps1).
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
if not exist "%ROOT%.cache\tmp" mkdir "%ROOT%.cache\tmp" >nul 2>&1
set "TEMP=%ROOT%.cache\tmp"
set "TMP=%ROOT%.cache\tmp"

rem --- Installed? Then start at once, without a console window ---
if not exist "%VENV%\Scripts\pythonw.exe" goto setup
if exist "%VENV%\yardguard-setup.pending" goto setup
set "PYHOME="
for /f "tokens=1,* delims==" %%a in ('findstr /b /i /c:"home" "%VENV%\pyvenv.cfg" 2^>nul') do set "PYHOME=%%b"
for /f "tokens=* delims= " %%h in ("%PYHOME%") do set "PYHOME=%%h"
if defined PYHOME if not exist "%PYHOME%\python.exe" goto setup
start "" /d "%ROOT%" "%VENV%\Scripts\pythonw.exe" "%ROOT%app.py"
exit /b 0

:setup
rem Hide this console when the file was double-clicked; the installer shows its own window.
set "HIDE="
echo %CMDCMDLINE% | findstr /i /c:"%~nx0" >nul && set "HIDE=-WindowStyle Hidden"
powershell.exe -NoProfile -ExecutionPolicy Bypass %HIDE% -File "%ROOT%setup\bootstrap.ps1" %*
set "CODE=%errorlevel%"
if "%CODE%"=="0" exit /b 0
if "%CODE%"=="10" exit /b 10
if "%CODE%"=="11" exit /b 11
rem The installer could not even start: say so in a message box (the console may be hidden).
powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('YardGuard setup could not start (code %CODE%). See .cache\setup.log in the YardGuard folder, or the Troubleshooting part of README.md.', 'YardGuard')"
exit /b %CODE%
