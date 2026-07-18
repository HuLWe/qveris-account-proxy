@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "QVP_EXIT=%ERRORLEVEL%"
if not "%QVP_EXIT%"=="0" (
  echo.
  pause
)
exit /b %QVP_EXIT%
