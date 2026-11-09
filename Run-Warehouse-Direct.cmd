@echo off
title Warehouse 4-Way Simulation
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch.ps1" -Mode Run
if errorlevel 1 pause
