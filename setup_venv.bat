@echo off
rem venv 构建脚本（Windows）
rem 用法: 双击运行，或在 cmd 中执行 setup_venv.bat
setlocal
cd /d "%~dp0"

rem 优先用 py 启动器，找不到再用 python
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

%PY% --version >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先到 https://www.python.org/downloads/ 安装，
    echo        安装时勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

if not exist .venv (
    echo ^>^> 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建 venv 失败
        pause
        exit /b 1
    )
)

echo ^>^> 安装依赖 ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [警告] 未检测到 ffmpeg，请安装后确保其在 PATH 中:
    echo   winget install Gyan.FFmpeg
    echo   或从 https://www.gyan.dev/ffmpeg/builds/ 下载解压，把 bin 目录加入 PATH
)

echo.
echo 完成。使用方法:
echo   .venv\Scripts\activate
echo   python split_shots.py .\videos
pause
