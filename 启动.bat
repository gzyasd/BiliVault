@echo off
chcp 936 >nul
title BiBiTool - B站收藏夹分类工具

cd /d "%~dp0"

echo ============================================
echo   BiBiTool - B站收藏夹自动分类工具
echo ============================================
echo.

REM 检测已有服务实例，避免重复启动多个控制台
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/runtime; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [√] BiBiTool 已在运行，正在打开浏览器
    start "" "http://127.0.0.1:8765"
    exit /b 0
)

REM 选择 Python 环境
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    echo [√] 使用项目虚拟环境 .venv
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [×] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH
        echo     下载地址: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    set "PYTHON=python"
    echo [√] 使用系统 Python
)

echo.
echo 正在启动服务...
echo 浏览器将自动打开 http://127.0.0.1:8765
echo 按 Ctrl+C 停止服务
echo.

set "PYTHONIOENCODING=utf-8"
"%PYTHON%" main.py

if errorlevel 1 (
    echo.
    echo [×] 服务异常退出，请检查上方错误信息
    echo     若提示缺少模块，请运行: pip install -r requirements.txt
    echo.
    pause
)
