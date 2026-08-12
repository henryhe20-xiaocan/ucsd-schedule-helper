@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 分享版固定用 8778 端口，避免和本机的主版(8777)冲突
rem If port 8778 is already in use, just open the browser and exit.
netstat -ano | findstr /c:":8778" | findstr /c:"LISTENING" >nul
if %errorlevel%==0 (
  echo Server already running, opening browser.
  start "" http://127.0.0.1:8778/
  exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 goto run_python
where py >nul 2>nul
if %errorlevel%==0 goto run_py
echo Python not found. Please install Python and check "Add Python to PATH".
pause
exit /b 1

:run_python
python -u scheduler.py --port 8778
if errorlevel 1 pause
exit /b 0

:run_py
py -u scheduler.py --port 8778
if errorlevel 1 pause
exit /b 0
