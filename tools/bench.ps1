# P4-8 一键基准（Windows 无 bash 时的等价物，与 tools/bench.sh 步骤一致）。
#
# 为什么需要它：本机只有 WSL 的 `bash` 桩（没装发行版），`make bench` 直接失败；
# 而"文档里的复现命令在本机跑不了"等于没有基线。步骤与 bench.sh 一一对应：
#   起服务（本机 SQLite + 离线 provider）→ 等 /healthz → loadgen 写/读各一轮
#   → 抓 /metrics 关键计数 → 报告写 benchmarks/last_run.txt（git 忽略）。
#
# 用法（Windows PowerShell 5.1 与 PowerShell 7 都能跑）：
#   .\tools\bench.ps1 -Port 8811 -Duration 15 -Devices 60 -WriteRps 300 -ReadRps 150
#   $env:DD_DATABASE_URL="postgresql+psycopg://..."; .\tools\bench.ps1 -Port 8812 ...
# 注：若 PATH 里没有 powershell/pwsh（某些受限环境就是这样），直接在当前会话里执行即可，
#     或用解释器全路径：& (Join-Path $PSHOME "powershell.exe") -File .\tools\bench.ps1 ...
param(
    [int]$Port = 8000,
    [int]$Duration = 30,
    [int]$Devices = 120,
    [int]$WriteRps = 500,
    [int]$ReadRps = 200,
    [string]$Tenant = "bench-t1"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$url = "http://127.0.0.1:$Port"
$outDir = Join-Path $root "benchmarks"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir "last_run.txt"
$logOut = Join-Path ([System.IO.Path]::GetTempPath()) ("dd_bench_{0}.out.log" -f $PID)
$logErr = Join-Path ([System.IO.Path]::GetTempPath()) ("dd_bench_{0}.err.log" -f $PID)

$lines = New-Object System.Collections.Generic.List[string]
function Say([string]$text) { $lines.Add($text); Write-Host $text }

Say "===== P4-8 基准开始（$(Get-Date -Format o)）====="
Say "URL=$url 时长=${Duration}s workers=$Devices 写=${WriteRps}rps 读=${ReadRps}rps"
Say "DD_DATABASE_URL=$($env:DD_DATABASE_URL)"

$env:DD_SERVICE_PORT = "$Port"
$proc = Start-Process -FilePath $py -ArgumentList "-m", "data_dog.service" `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru -NoNewWindow
try {
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing "$url/healthz" -TimeoutSec 2 | Out-Null
            $ready = $true; break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) {
        Say "✗ 服务未在 20s 内就绪；日志尾部："
        Say (Get-Content $logErr -Tail 30 | Out-String)
        exit 1
    }
    foreach ($mode in @("write", "read")) {
        $rps = if ($mode -eq "write") { $WriteRps } else { $ReadRps }
        Say ""
        Say "----- $mode @ $rps rps -----"
        $outText = & $py (Join-Path $root "tools\loadgen.py") --url $url --mode $mode `
            --devices $Devices --rps $rps --duration $Duration --tenant $Tenant `
            --verify-server --assert-no-llm 2>&1 | Out-String
        Say $outText
    }
    Say "----- /metrics 关键计数（压测后）-----"
    $m = (Invoke-WebRequest -UseBasicParsing "$url/metrics").Content
    foreach ($ln in ($m -split "`n")) {
        if ($ln -match "^data_dog_(investigations|jobs|telemetry)_total") { Say $ln.Trim() }
    }
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    Remove-Item $logOut, $logErr -Force -ErrorAction SilentlyContinue
}
Say ""
Say "===== 结束（$(Get-Date -Format o)）报告：benchmarks/last_run.txt ====="
$lines -join [Environment]::NewLine | Set-Content -Path $out -Encoding utf8
