@echo off
REM Start the bot in the background (no console window) with auto-restart on crash.
cd /d "%~dp0"
if not exist logs mkdir logs
if exist SHUTDOWN del SHUTDOWN
if not exist ".venv\Scripts\pythonw.exe" (echo Run setup.bat first & exit /b 1)
start "" ".venv\Scripts\pythonw.exe" "%~dp0run_forever.py"
echo Bot started. Logs: %~dp0logs\bot.log
