@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\OEM\codex\install.ps1
exit /b %errorlevel%
