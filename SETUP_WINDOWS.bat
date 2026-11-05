@echo off
rem Double-click this file to set up Local AI (Ollama + Hermes) on Windows.
rem Do NOT use "Run as administrator"; the setup asks when it needs permission.
title Local AI setup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows_setup.ps1" %*
echo.
echo The setup has finished. The full output is in windows_setup_log.txt in this folder.
pause
