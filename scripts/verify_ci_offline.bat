@echo off
rem ---------------------------------------------------------------------------
rem Windows 入口（cmd / PowerShell）。为什么不直接 `bash scripts\verify_ci_offline.sh`：
rem cmd 下 `bash` 解析到 C:\Windows\System32\bash.exe（WSL），会报
rem   <3>WSL ... ERROR: CreateProcessCommon:800: execvpe(/bin/bash) failed
rem 而脚本本身是 Git bash 兼容的。所以这里显式找 Git for Windows 的 bash.exe。
rem 单一真源不变：所有逻辑仍在 verify_ci_offline.sh 里，本文件只做“找对解释器”。
rem ---------------------------------------------------------------------------
setlocal
set "GB=%ProgramFiles%\Git\bin\bash.exe"
if exist "%GB%" goto run
set "GB=%ProgramFiles(x86)%\Git\bin\bash.exe"
if exist "%GB%" goto run
set "GB=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if exist "%GB%" goto run
echo [x] 未找到 Git for Windows 的 bash.exe。请安装 Git，或改用 WSL/Linux 执行 scripts/verify_ci_offline.sh >&2
exit /b 9
:run
"%GB%" "%~dp0verify_ci_offline.sh" %*
exit /b %ERRORLEVEL%
