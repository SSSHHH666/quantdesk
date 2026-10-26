@echo off
chcp 936 >nul
rem 本文件使用 GBK 编码保存，以兼容中文 Windows 的 cmd 批处理解析，请勿转存为 UTF-8。
setlocal EnableExtensions EnableDelayedExpansion
title QuantDesk 停止器

rem ============================================================
rem  QuantDesk 一键停止脚本（M4 复核）
rem  用法：
rem    scripts\stop.bat                 自动查找 8501~8510 中运行中的实例（仅一个时直接停止）
rem    scripts\stop.bat 8502            指定端口
rem    scripts\stop.bat 8502 /Y         跳过确认（自动化演练用）
rem  说明：按端口找到监听进程（通常是 python.exe），中文确认后结束；
rem        不会影响其它端口上的程序；找不到实例时给出中文提示。
rem ============================================================

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
cd /d "%ROOT%"
set "PORT="
set "YES=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/y" (
    set "YES=1"
    shift
    goto parse_args
)
if /i "%~1"=="--yes" (
    set "YES=1"
    shift
    goto parse_args
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
echo(%~1|findstr /R "^[0-9][0-9]*$" >nul 2>&1
if not errorlevel 1 (
    set "PORT=%~1"
    shift
    goto parse_args
)
echo [提示] 忽略未知参数：%~1
shift
goto parse_args
:args_done

echo ============================================================
echo   QuantDesk - 停止服务
echo ============================================================
echo   项目目录：%CD%
echo.

rem ---------- 1. 定位实例 ----------
set "FOUND_COUNT=0"
set "FOUND_PORTS="
set "FOUND_PIDS="

if defined PORT (
    call :find_pid %PORT%
    if not defined PID (
        echo [提示] 端口 %PORT% 上没有正在监听的进程，QuantDesk 可能未运行。
        echo.
        if not defined QD_NO_PAUSE pause
        exit /b 0
    )
    set "FOUND_COUNT=1"
    set "FOUND_PORTS=%PORT%"
    set "FOUND_PIDS=%PID%"
    goto show_process
)

for /L %%P in (8501,1,8510) do (
    call :find_pid %%P
    if defined PID (
        set /a FOUND_COUNT+=1
        set "FOUND_PORTS=!FOUND_PORTS! %%P"
        set "FOUND_PIDS=!FOUND_PIDS! !PID!"
    )
)

if "%FOUND_COUNT%"=="0" (
    echo [提示] 未发现运行中的 QuantDesk（端口 8501~8510 均无监听）。
    echo        如果界面仍在运行，可能是手动使用的其它端口；可用：
    echo          netstat -ano ^| findstr LISTENING  ^(查找端口^)
    echo          scripts\stop.bat ^<端口^>
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 0
)

if not "%FOUND_COUNT%"=="1" (
    echo [提示] 发现 %FOUND_COUNT% 个监听实例，端口：%FOUND_PORTS%
    echo        请指定要停止的端口，例如：scripts\stop.bat 8502
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

set "PORT=%FOUND_PORTS: =%"
set "PID=%FOUND_PIDS: =%"
echo [发现] 端口 %PORT% 上有 QuantDesk 进程（PID=%PID%）.

:show_process
echo.
echo 进程信息：
tasklist /FI "PID eq %PID%" /FO TABLE
echo.
echo 进程命令行：
powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%' -ErrorAction SilentlyContinue).CommandLine" 2>nul
echo.

rem 安全检查：命令行不像 streamlit 时给出额外警告（仍交由用户确认）
set "CMDLINE="
for /f "delims=" %%L in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%' -ErrorAction SilentlyContinue).CommandLine" 2^>nul') do set "CMDLINE=%%L"
echo !CMDLINE! | findstr /I "streamlit" >nul 2>&1
if errorlevel 1 (
    echo [警告] 该进程命令行中未检测到 streamlit，可能不是 QuantDesk；请确认后再结束。
    echo.
)

rem ---------- 2. 二次确认 ----------
if "%YES%"=="1" goto do_stop
choice /C YN /N /M "确认结束端口 %PORT% 上的进程 PID=%PID% 并停止 QuantDesk？[Y=是 N=否] "
if errorlevel 2 (
    echo [已取消] 未结束任何进程，QuantDesk 继续运行。
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 0
)

:do_stop
echo.
echo [停止] 正在结束进程 PID=%PID% ...
taskkill /PID %PID% /F /T >nul 2>&1
if errorlevel 1 (
    echo [错误] 结束进程失败（可能权限不足或进程已退出）。
    echo        可尝试以管理员身份运行本脚本，或手动执行：
    echo          taskkill /PID %PID% /F /T
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

rem 等待端口释放（最多约 5 秒）
set "WAIT=0"
:wait_loop
call :find_pid %PORT%
if not defined PID goto stopped
set /a WAIT+=1
if %WAIT% GEQ 5 goto still_listening
ping -n 2 127.0.0.1 >nul
goto wait_loop

:still_listening
echo [警告] 进程已结束但端口 %PORT% 仍在监听，请稍后重试或用 netstat -ano ^| findstr :%PORT% 检查。
echo.
if not defined QD_NO_PAUSE pause
exit /b 1

:stopped
echo [成功] QuantDesk 已停止（端口 %PORT% 已释放）。
echo        如需重新启动，双击 scripts\start.bat。
echo.
if not defined QD_NO_PAUSE pause
exit /b 0

rem ============================================================
rem 子过程：查找端口监听进程 PID（结果写入变量 PID；无监听时清空）
rem ============================================================
:find_pid
set "PID="
for /f "tokens=5" %%A in ('netstat -ano 2^>nul ^| findstr /R /C:":%~1 .*LISTENING"') do set "PID=%%A"
exit /b 0
