@echo off
rem 5S Vision - start on this PC (Windows). Double-click to run.
setlocal
cd /d "%~dp0"
set PORT=8780

if not exist ".venv\Scripts\python.exe" (
  echo [5S Vision] First run: creating Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Python 3.10+ is required. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"

rem Install packages only on the first run, or when requirements.txt has changed after an update.
rem Later starts skip this step, so they are fast and work without internet.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
  echo [5S Vision] Installing packages. This takes 1 to 3 minutes and needs internet...
  python -m pip install --quiet --disable-pip-version-check --no-cache-dir -r requirements.txt
  if errorlevel 1 (
    echo Package install failed. Check the internet connection and run this file again.
    pause
    exit /b 1
  )
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

echo.
echo [5S Vision] Starting. The system is ready when you see "Application startup complete".
echo   On this PC:             http://localhost:%PORT%
echo   Phones on same Wi-Fi:   http://THIS-PC-IP:%PORT%   - run "ipconfig" to see the IP
echo   To stop: close this window or run stop.bat
echo.
start "" "http://localhost:%PORT%"
python -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --no-use-colors
pause
