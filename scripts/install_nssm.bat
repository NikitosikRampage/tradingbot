@echo off
REM Alternative to Task Scheduler: Windows service via NSSM (https://nssm.cc). Run as Administrator.
REM nssm.exe must be in PATH or next to this script.
cd /d "%~dp0.."
set ROOT=%CD%
set SVC=OKXBot
nssm install %SVC% "%ROOT%\.venv\Scripts\python.exe" "\"%ROOT%\main.py\""
nssm set %SVC% AppDirectory "%ROOT%"
nssm set %SVC% DisplayName "OKX trading bot"
nssm set %SVC% Start SERVICE_AUTO_START
nssm set %SVC% AppStdout "%ROOT%\logs\service.log"
nssm set %SVC% AppStderr "%ROOT%\logs\service.log"
nssm set %SVC% AppRotateFiles 1
nssm set %SVC% AppRotateBytes 10485760
nssm set %SVC% AppExit Default Restart
nssm set %SVC% AppExit 3 Exit
nssm set %SVC% AppRestartDelay 10000
nssm set %SVC% AppThrottle 30000
nssm set %SVC% AppStopMethodConsole 30000
nssm start %SVC%
echo Service %SVC% installed. Manage: nssm start/stop/restart %SVC%
