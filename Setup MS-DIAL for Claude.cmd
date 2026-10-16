@echo off
setlocal
rem Set up MS-DIAL Interactive for Claude Code and Claude Desktop: scripts\setup_claude.py explains
rem each step and asks before it changes anything. Arguments are passed on (--dry-run, --yes, --help).
set "SETUP=%~dp0scripts\setup_claude.py"
rem The Python install manager behind "py" and "python" (python.org's default from 3.14 on) installs
rem the newest Python by itself when none is registered. Nothing is installed without asking here.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHON_CMD="
call :try py -3
if not defined PYTHON_CMD call :try python
if not defined PYTHON_CMD goto :nopython
%PYTHON_CMD% "%SETUP%" %*
set "SETUP_EXIT=%ERRORLEVEL%"
goto :done
:nopython
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/ and run this file again.
set "SETUP_EXIT=1"
:done
echo.
pause
endlocal & exit /b %SETUP_EXIT%

:try
rem A command is taken only when it runs a Python 3.10 or newer. That rejects the Microsoft Store's
rem "python" alias with no Python installed (exit 9009), a launcher with no Python 3, and an older Python.
%* -c "import sys; sys.exit(sys.version_info < (3, 10))" <nul >nul 2>nul && set "PYTHON_CMD=%*"
exit /b 0
