@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Engine\start-portal.ps1" %*
if errorlevel 1 (pause & exit /b 1)
