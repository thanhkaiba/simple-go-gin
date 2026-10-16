@echo off
rem Camera attendance: stop it and remove it from this computer. Double-click this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1"
pause
