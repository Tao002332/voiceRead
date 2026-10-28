# 启动曼波（GPT-SoVITS）本地服务
# 用法： powershell -ExecutionPolicy Bypass -File scripts\start_mambo_server.ps1 [-Port 9880]
param(
    [string]$Root = '',
    [int]$Port = 9880
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\mambo'))
}

$gsv = Join-Path $Root 'GPT-SoVITS'
$vpy = Join-Path $Root 'venv\Scripts\python.exe'
$cfg = Join-Path $gsv 'GPT_SoVITS\configs\tts_infer.yaml'

foreach ($p in @($gsv, $vpy, $cfg)) {
    if (-not (Test-Path $p)) { Write-Host "[ERROR] 缺少 $p，请先运行 setup_mambo_win.ps1" -ForegroundColor Red; exit 1 }
}

Write-Host "启动曼波服务 http://127.0.0.1:$Port ..." -ForegroundColor Cyan
Write-Host "首次加载模型约 15-40 秒，出现 'TTS server listening' 即为就绪" -ForegroundColor DarkGray
Set-Location $gsv
& $vpy api_v2.py -a 127.0.0.1 -p $Port -c $cfg
