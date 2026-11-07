@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || exit /b 1
)
".venv\Scripts\python.exe" -c "import bs4, PIL, reportlab, keyring, pypdfium2, tldextract, playwright" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)
".venv\Scripts\python.exe" -m Engine.site_server --open %*
