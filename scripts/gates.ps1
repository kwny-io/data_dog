<#
.SYNOPSIS
  data_dog pre-commit gate - one command equivalent to CI's lint + test legs (Windows).

.DESCRIPTION
  Windows usually has no make, while the repo's `make gates` is POSIX-flavoured. This script
  provides the same gate on Windows so "must pass before commit" is one command on both
  platforms:

      powershell -ExecutionPolicy Bypass -File scripts/gates.ps1

  It runs ruff check -> mypy -> pytest in order and exits immediately with the failing step's
  exit code, so a failure is never masked.

  CI mapping: `ruff check .` + `mypy src` (lint job), `pytest -q` (test job) —— 三处逐字一致。
  CI additionally enforces coverage (--cov-fail-under) and a per-module floor
  (scripts/cov_gate.py); run `make cov` or add those flags locally when you want them.

  NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads BOM-less UTF-8 as
  ANSI, which would mangle non-ASCII text and can break parsing - a gate script that fails to
  load is worse than one with English messages.
#>
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

function Invoke-Gate {
    param([string]$Label, [string[]]$GateArgs)
    Write-Host "[gates] $Label" -ForegroundColor Cyan
    & $py @GateArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[gates] FAILED at: $Label (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Gate "ruff check ." @("-m", "ruff", "check", ".")
Invoke-Gate "mypy src" @("-m", "mypy", "src")
Invoke-Gate "pytest -q" @("-m", "pytest", "-q")

Write-Host "[gates] all passed" -ForegroundColor Green
exit 0
