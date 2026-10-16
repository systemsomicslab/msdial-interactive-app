@echo off
setlocal
rem Set up MS-DIAL Interactive for Claude Code and Claude Desktop: scripts\setup_claude.py explains
rem each step and asks before it changes anything. Arguments are passed on (--dry-run, --yes, --help).
set "SETUP=%~dp0scripts\setup_claude.py"
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"
if defined PYTHON_CMD (
    %PYTHON_CMD% "%SETUP%" %*
) else (
    echo Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and run this file again.
)
echo.
pause
endlocal
