@echo off
rem ---------------------------------------------------------------------------
rem data_dog one-click start (Windows entry point)
rem
rem   dev.bat            start all three services (daemonized, returns at once)
rem   dev.bat stop       stop them (only our own processes, never someone else's)
rem   dev.bat status     show what is running
rem   dev.bat restart    stop, then start
rem   dev.bat logs       follow the logs (Ctrl-C leaves the logs, services keep running)
rem
rem Why this file exists:
rem   In cmd / PowerShell, `bash` resolves to C:\Windows\System32\bash.exe (WSL).
rem   WSL cannot see D:\ , so running tools/dev.sh there fails with
rem   `execvpe(/bin/bash) failed`. This file finds the Git-for-Windows bash instead.
rem
rem This file keeps ALL CMDPARSED TEXT IN ASCII ON PURPOSE.
rem   cmd reads a .bat byte-by-byte in the active codepage (936/GBK here). A UTF-8
rem   Chinese comment can desync that decoder and split a line into stray commands.
rem   The Chinese wording all lives in tools/dev.sh, which bash reads as UTF-8.
rem
rem Single source of truth for the logic is still tools/dev.sh; this file only
rem locates the right interpreter and forwards the arguments verbatim.
rem Same approach as scripts/verify_ci_offline.bat, for the same reason.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem Make the console UTF-8 so the Chinese that tools/dev.sh prints stays readable.
chcp 65001 >nul 2>&1

set "GB=%ProgramFiles%\Git\bin\bash.exe"
if exist "%GB%" goto run
set "GB=%ProgramFiles(x86)%\Git\bin\bash.exe"
if exist "%GB%" goto run
set "GB=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if exist "%GB%" goto run

echo [x] Git for Windows bash.exe not found.
echo     Install Git from https://git-scm.com/download/win
echo     or run this from WSL / Linux instead:  bash tools/dev.sh
exit /b 9

:run
rem dev.sh with no argument already defaults to `start`; keep the same behaviour.
if "%~1"=="" (
  "%GB%" "tools\dev.sh" start
) else (
  "%GB%" "tools\dev.sh" %*
)
exit /b %ERRORLEVEL%
