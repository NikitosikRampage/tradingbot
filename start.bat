@echo off
REM Start the bot in the background (no console window) with auto-restart on crash,
REM then open the local interface in the browser.
cd /d "%~dp0"
if not exist logs mkdir logs
if exist SHUTDOWN del SHUTDOWN
if exist data\ui_url.txt del data\ui_url.txt
if not exist ".venv\Scripts\pythonw.exe" (echo Run setup.bat first & exit /b 1)
start "" ".venv\Scripts\pythonw.exe" "%~dp0run_forever.py"
echo Bot started. Logs: %~dp0logs\bot.log
echo Waiting for the interface...
for /l %%i in (1,1,30) do (
  if exist data\ui_url.txt goto open
  timeout /t 1 /nobreak >nul
)
echo Interface did not start in 30 s - see logs\bot.log (port busy?). Default address: http://127.0.0.1:8765
exit /b 0
:open
set /p URL=<data\ui_url.txt
echo Interface: %URL%
start "" "%URL%"
