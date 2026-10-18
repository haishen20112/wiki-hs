@echo off
chcp 936 >nul 2>nul
title 星海百科 - 服务器版
cd /d "%~dp0"
echo ============================================================
echo   星海百科 - 服务器版
echo ============================================================
echo.
echo   启动后不要关闭这个窗口，关了网站就停了。
echo   默认地址: http://127.0.0.1:8081
echo.
echo ============================================================
echo.
where python >nul 2>nul
if errorlevel 1 goto nopython
python wiki.py
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
