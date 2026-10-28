@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] .venv not found in this folder.
    echo Run "初始化环境并启动.bat" first,
    echo or create it with:  python -m venv .venv
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "run.py"
