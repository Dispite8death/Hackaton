@echo off
rem Веб-интерфейс «Север» поверх движка audit/: http://127.0.0.1:8765
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (py -3 -m venv .venv || python -m venv .venv)
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || (pause & exit /b 1)
start "" http://127.0.0.1:8765
.venv\Scripts\python.exe -m sever serve --port 8765
if errorlevel 1 pause
