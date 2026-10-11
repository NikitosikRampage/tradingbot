@echo off
REM One-time setup: venv + dependencies + config templates
cd /d "%~dp0"
where py >nul 2>nul && (py -3.11 -m venv .venv) || (python -m venv .venv)
if errorlevel 1 (echo Python 3.11 not found & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if not exist config.yaml copy config.example.yaml config.yaml
if not exist .env copy .env.example .env
if not exist logs mkdir logs
if not exist data mkdir data
echo.
echo Done. Edit .env and config.yaml, then run start.bat
