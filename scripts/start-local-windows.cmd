@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start-local-windows.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
rem A double-clicked window closes as soon as the script ends: on a failure, keep the message readable.
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
