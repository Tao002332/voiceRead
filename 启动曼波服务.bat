@echo off
setlocal
cd /d "%~dp0"
if not exist "mambo\venv\Scripts\python.exe" (
    echo [ERROR] mambo environment not found.
    echo Run:  powershell -ExecutionPolicy Bypass -File scripts\setup_mambo_win.ps1
    pause
    exit /b 1
)
echo Starting Mambo TTS server on 127.0.0.1:9880 ...
echo Keep this window open while using the reader. Close it to stop the server.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start_mambo_server.ps1"
pause
