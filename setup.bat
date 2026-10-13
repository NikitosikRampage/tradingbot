@echo off
setlocal
REM One-time setup: venv + dependencies + config templates.
REM Works with Python 3.11, 3.12 or 3.13 (64-bit) from python.org.
cd /d "%~dp0"
set "CHECK=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"

REM a .venv left by a failed attempt or made with an unsupported Python is recreated
if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "%CHECK%" >nul 2>nul && goto have_venv
echo Removing the old .venv made with an unsupported Python...
rmdir /s /q .venv

:find_python
set "PY="
where py >nul 2>nul || goto try_python
for %%v in (3.12 3.11 3.13) do call :probe %%v
if defined PY goto make_venv
:try_python
python -c "%CHECK%" >nul 2>nul && set "PY=python"
if defined PY goto make_venv
echo.
echo ERROR: Python 3.11, 3.12 or 3.13 was not found.
echo Install Python 3.12 64-bit: https://www.python.org/downloads/windows/
echo On the first screen of the installer tick "Add python.exe to PATH".
echo Then close this window and run setup.bat again.
echo.
where py >nul 2>nul && echo Python versions found on this PC: && py -0
pause
exit /b 1

:make_venv
echo Using %PY%
%PY% -m venv .venv
if errorlevel 1 goto venv_failed

:have_venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto pip_failed
if not exist config.yaml copy config.example.yaml config.yaml
if not exist .env copy .env.example .env
if not exist logs mkdir logs
if not exist data mkdir data
echo.
echo Done. Fill in .env (OKX demo keys), check: .venv\Scripts\python scripts\preflight.py
echo Then run start.bat
pause
exit /b 0

:venv_failed
echo ERROR: could not create .venv with %PY%
pause
exit /b 1

:pip_failed
echo ERROR: installing packages failed - check the internet connection and run setup.bat again.
pause
exit /b 1

:probe
REM sets PY to the first launcher version that exists and is supported
if defined PY exit /b 0
py -%1 -c "%CHECK%" >nul 2>nul
if not errorlevel 1 set "PY=py -%1"
exit /b 0
