@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment ...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create venv. Install Python 3.10+ first.
        pause
        exit /b 1
    )
    echo [2/3] Installing requirements ...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [WARN] pip reported an error. Try: pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
) else (
    echo [1/3] venv found, skip install
)

echo [3/3] Launching ...
start "" ".venv\Scripts\pythonw.exe" "run.py"
