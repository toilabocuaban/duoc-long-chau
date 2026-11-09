@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch.ps1" -Mode View -View outbound_detail
