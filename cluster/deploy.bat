@echo off
rem k3s 视频处理集群部署（Windows）
rem 用法: 双击运行进入菜单，或 deploy.bat deploy 直接执行某个动作
rem 说明: 本机作为操作端，通过 SSH 远程部署局域网里的 Linux 节点。
rem       master 必须是 Linux 机器，k3s 控制平面没有 Windows 版本。
setlocal enabledelayedexpansion
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% --version >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请到 https://www.python.org/downloads/ 安装
    echo        安装时记得勾选 "Add Python to PATH"
    pause
    exit /b 1
)

%PY% -c "import paramiko" >nul 2>nul
if errorlevel 1 (
    echo ^>^> 安装依赖 paramiko ...
    %PY% -m pip install --quiet paramiko
    if errorlevel 1 (
        echo [错误] 安装失败，请手动执行: %PY% -m pip install paramiko
        pause
        exit /b 1
    )
)

if not "%~1"=="" (
    %PY% deploy.py %1
    exit /b %errorlevel%
)

:menu
echo.
echo =========== 集群部署 ===========
if exist cluster.json (
    for /f "delims=" %%i in ('%PY% -c "import json;d=json.load(open('cluster.json'));print(d['master']['name']+' / '+str(1+len(d.get('workers',[])))+' 个节点')" 2^>nul') do echo   当前配置: master=%%i
) else (
    echo   尚未配置（先执行 1）
)
echo   1) 配置集群（录入节点、选定 master）
echo   2) 检查各节点环境（只读，不做改动）
echo   3) 执行部署
echo   4) 查看集群状态
echo   5) 卸载 k3s（保留数据）
echo   q) 退出
set "c="
set /p "c=请选择: "
if /i "%c%"=="1" (%PY% deploy.py init) & goto menu
if /i "%c%"=="2" (%PY% deploy.py check) & goto menu
if /i "%c%"=="3" (%PY% deploy.py deploy) & goto menu
if /i "%c%"=="4" (%PY% deploy.py status) & goto menu
if /i "%c%"=="5" (%PY% deploy.py teardown) & goto menu
if /i "%c%"=="q" exit /b 0
echo 无效选择
goto menu
