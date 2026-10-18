@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
set "bundle_exit=%errorlevel%"
if not "%bundle_exit%"=="0" pause
exit /b %bundle_exit%
