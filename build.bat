@echo off
setlocal
cd /d "%~dp0"

echo.
echo  Building LocalCode.exe (single-file, windowed)
echo  ---------------------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
  echo Make sure "Add Python to PATH" is checked during installation.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the build environment...
  python -m venv .venv
  if errorlevel 1 goto fail
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo Building...
".venv\Scripts\pyinstaller.exe" --clean --noconfirm LocalCode.spec
if errorlevel 1 goto fail

echo.
echo  Done: dist\LocalCode.exe
echo  Models, the llama.cpp engine and portable Node download on first use.
echo.
exit /b 0

:fail
echo.
echo Build failed. See the output above.
pause
exit /b 1
