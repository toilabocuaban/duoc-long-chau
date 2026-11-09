@echo off
title Warehouse 4-Way Simulation
cd /d "%~dp0"
wsl.exe -d Gazebo-Ubuntu -u gazebo -- wslpath -u "%~dp0" > "%TEMP%\warehouse4way-linux-path.txt"
set /p LINUX_ROOT=<"%TEMP%\warehouse4way-linux-path.txt"
del /q "%TEMP%\warehouse4way-linux-path.txt" >nul 2>&1
wsl.exe -d Gazebo-Ubuntu -u gazebo -- bash -lc "cd '%LINUX_ROOT%' && bash ./run.sh"
if errorlevel 1 pause
