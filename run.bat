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
echo [5S Vision] Checking packages...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Package install failed. Check the internet connection and try again.
  pause
  exit /b 1
)
echo.
echo [5S Vision] Running at  http://localhost:%PORT%
echo Phones on the same Wi-Fi: open  http://THIS-PC-IP:%PORT%   (run "ipconfig" to see the IP)
echo Close this window or run stop.bat to stop.
echo.
start "" "http://localhost:%PORT%"
python -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
pause
