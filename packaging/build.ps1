# 打包成免安装 exe（PyInstaller，onedir）
# 用法：在 voiceRead 目录下执行  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '[ERROR] .venv not found. Run 一键部署/创建 venv first.' -ForegroundColor Red
    exit 1
}

Write-Host '[1/3] install pyinstaller ...' -ForegroundColor Cyan
& $py -m pip install -q pyinstaller

Write-Host '[2/3] build ...' -ForegroundColor Cyan
& $py -m PyInstaller `
    --noconfirm --clean --windowed --onedir `
    --name '语音书架' `
    --collect-submodules src `
    --collect-submodules edge_tts `
    --collect-data edge_tts `
    --hidden-import pyttsx3 `
    --hidden-import comtypes `
    --hidden-import PySide6.QtMultimedia `
    --hidden-import PySide6.QtNetwork `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module tkinter `
    'run.py'

Write-Host '[3/3] done.' -ForegroundColor Green
Write-Host "输出目录: $root\dist\语音书架\" -ForegroundColor Yellow
Write-Host '双击其中的 语音书架.exe 即可运行；整个目录可拷贝到任意位置。' -ForegroundColor Yellow
