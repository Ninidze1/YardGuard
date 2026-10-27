@echo off
rem Update YardGuard's libraries after "git pull" or after copying in a newer download.
rem Keeps your cameras, settings, alerts and downloaded AI models. Close YardGuard first.
rem   Update.cmd               normal update
rem   Update.cmd -Torch cpu    use the processor-only AI libraries (for example an old NVIDIA card)
rem   Update.cmd -Torch cuda   use the NVIDIA graphics card libraries
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.cache\tmp" mkdir "%ROOT%.cache\tmp" >nul 2>&1
set "TEMP=%ROOT%.cache\tmp"
set "TMP=%ROOT%.cache\tmp"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup\bootstrap.ps1" -Update %*
exit /b %errorlevel%
