@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 goto python_install
where py >nul 2>nul
if %errorlevel%==0 goto py_install
echo Please install Python first.
pause
exit /b 1
:python_install
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
python -m playwright install chromium
if errorlevel 1 goto failed
goto done
:py_install
py -m pip install -r requirements.txt
if errorlevel 1 goto failed
py -m playwright install chromium
if errorlevel 1 goto failed
:done
echo 登录组件安装完成。现在运行 start.bat。
pause
exit /b 0
:failed
echo 安装失败。请检查上方错误，不要关闭证书校验。
pause
exit /b 1
