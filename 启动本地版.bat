@echo off
chcp 936 >nul 2>nul
title 星海百科 - 本地编辑台
cd /d "%~dp0"
echo ============================================================
echo   星海百科 - 本地编辑台
echo ============================================================
echo.
echo   正在启动，稍等几秒浏览器会自动打开...
echo   关闭这个黑窗口 = 停止服务
echo.
echo ============================================================
echo.
where python >nul 2>nul
if errorlevel 1 goto nopython
python wiki.py --local
if errorlevel 1 goto failed
goto end
:nopython
echo.
echo [错误] 没有找到 Python
echo.
echo 请先安装 Python 3.10 或更新版本
echo 下载地址: https://www.python.org/downloads/
echo 安装时务必勾选 Add Python to PATH
echo.
goto end
:failed
echo.
echo ------------------------------------------------------------
echo   启动失败
echo   请先安装依赖，在这个窗口执行:
echo       pip install fastapi uvicorn python-multipart
echo ------------------------------------------------------------
echo.
:end
pause
