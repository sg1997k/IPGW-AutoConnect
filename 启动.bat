@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [!] 没检测到 Python, 请先安装 Python 3.10 或更高版本。
    echo     下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import customtkinter, pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo [*] 正在安装依赖, 首次运行需要一点时间…
    python -m pip install -r requirements.txt
)

echo [*] 正在启动校园网守护…
start "" pythonw app.py
exit /b 0
