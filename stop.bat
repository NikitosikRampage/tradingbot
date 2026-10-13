@echo off
REM Graceful stop: the bot saves the session state; positions are NOT closed
REM (an active session is restored at the next start). Emergency close-all: the "Stop" button in the interface or create file STOP.
cd /d "%~dp0"
echo.> SHUTDOWN
set PID=
if exist data\bot.lock set /p PID=<data\bot.lock
if "%PID%"=="" goto done
echo Waiting for the bot (PID %PID%) to stop...
for /l %%i in (1,1,30) do (
  tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul || goto done
  timeout /t 2 /nobreak >nul
)
echo Graceful stop timed out, killing PID %PID%
taskkill /PID %PID% /T /F
:done
echo Bot stopped.
