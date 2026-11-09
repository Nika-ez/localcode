@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
  echo Make sure "Add Python to PATH" is checked during installation.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the LocalCode environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Could not install dependencies. Check your internet connection.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
