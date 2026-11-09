@echo off
title Warehouse 4-Way Simulation
cd /d "%~dp0"
wsl.exe -d Gazebo-Ubuntu -u gazebo -- bash -lc "cd /mnt/d/gazebo/_inspect/Warehouse4Way && bash ./run.sh"
if errorlevel 1 pause
