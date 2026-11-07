@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 goto run_python
where py >nul 2>nul
if %errorlevel%==0 goto run_py
echo Python not found. Install Python and enable Add Python to PATH.
pause
exit /b 1
:run_python
python -u launch.py
if errorlevel 1 pause
exit /b 0
:run_py
py -u launch.py
if errorlevel 1 pause
exit /b 0
