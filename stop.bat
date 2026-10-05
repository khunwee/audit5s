@echo off
rem 5S Vision - stop the local server started by run.bat
set PORT=8780
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
  taskkill /PID %%a /F >nul 2>&1
  set FOUND=1
)
if "%FOUND%"=="1" (echo 5S Vision stopped.) else (echo 5S Vision is not running on port %PORT%.)
timeout /t 2 >nul
