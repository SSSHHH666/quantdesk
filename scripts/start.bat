@echo off
chcp 936 >nul
rem 本文件使用 GBK 编码保存，以兼容中文 Windows 的 cmd 批处理解析，请勿转存为 UTF-8。
setlocal EnableExtensions EnableDelayedExpansion
title QuantDesk 启动器

rem ============================================================
rem  QuantDesk 一键启动脚本（M0-08 / M4 复核）
rem  用法：
rem    双击本文件，或命令行：scripts\start.bat
rem    scripts\start.bat --check            只检查环境/端口/日志，不启动（自动化演练用）
rem    scripts\start.bat --port 8502        指定端口（默认自动从 8501 起找空闲端口）
rem
rem  行为：
rem    1) 优先项目内 .venv，其次 D:\Anaconda3\python.exe，最后 PATH 中的 python
rem    2) 检查端口 8501（被占用则自动改用 8502~8510，并给出中文提示）
rem    3) 启动输出同时写入 runtime\logs\start.log（UTF-8）
rem    4) 窗口保持显示启动信息与运行输出；停止请用 scripts\stop.bat 或关闭窗口
rem ============================================================

set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
cd /d "%ROOT%"
set "CHECK_ONLY=0"
set "PORT="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--check" (
    set "CHECK_ONLY=1"
    shift
    goto parse_args
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
echo [提示] 忽略未知参数：%~1
shift
goto parse_args
:args_done

echo ============================================================
echo   QuantDesk 量化交易工作台 - 启动器
echo ============================================================
echo   项目目录：%CD%
echo.

rem ---------- 1. 检查入口文件 ----------
if not exist "app.py" (
    echo [错误] 未找到 app.py，无法启动 QuantDesk。
    echo        请确认 app.py 与 scripts 目录位于同一个 quantdesk 项目根目录下。
    echo        当前目录：%CD%
    echo.
    echo        如果刚拉取代码，请确认项目文件完整；或联系集成负责人（Integrator）。
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

rem ---------- 2. 选择 Python 解释器（优先 .venv） ----------
set "PY="
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
    echo [环境] 使用项目虚拟环境：.venv\Scripts\python.exe
) else (
    if exist "D:\Anaconda3\python.exe" (
        set "PY=D:\Anaconda3\python.exe"
        echo [环境] 未找到项目虚拟环境，回退到：D:\Anaconda3\python.exe
        echo [提示] 回退解释器可能未安装本项目依赖，建议优先创建 .venv 并安装 requirements.txt。
    ) else (
        for /f "delims=" %%I in ('where python 2^>nul') do (
            if not defined PY set "PY=%%I"
        )
        if defined PY echo [环境] 未找到 .venv/Anaconda，回退到 PATH 中的 python：!PY!
    )
)

if not defined PY (
    echo [错误] 未找到可用的 Python 解释器。
    echo        已检查以下位置：
    echo          1. .venv\Scripts\python.exe
    echo          2. D:\Anaconda3\python.exe
    echo          3. PATH 中的 python
    echo.
    echo        请先安装 Python 3.10 或更高版本，然后在项目根目录执行：
    echo          python -m venv .venv
    echo          .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [错误] 选定的 Python 解释器无法运行：
    echo        %PY%
    echo        请检查该文件是否被删除、损坏，或改用其它 Python 解释器。
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

rem ---------- 3. 检查关键依赖 streamlit ----------
"%PY%" -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前 Python 环境缺少 streamlit，无法启动界面。
    echo        解释器：%PY%
    echo.
    echo        请在项目根目录执行以下命令安装依赖：
    echo          "%PY%" -m pip install -r requirements.txt
    echo.
    echo        网络较慢时可加国内镜像，例如：
    echo          "%PY%" -m pip install --isolated -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
    echo.
    echo        安装完成后重新双击 start.bat；也可先运行 scripts\check_env.py 查看缺失清单。
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)

rem ---------- 4. 选择端口（8501 被占用则自动 8502~8510） ----------
if defined PORT goto check_given_port

set "PORT="
set "BUMPED=0"
for /L %%P in (8501,1,8510) do (
    if not defined PORT (
        call :port_in_use %%P
        if errorlevel 1 (
            set "PORT=%%P"
        ) else (
            if "%%P"=="8501" set "BUMPED=1"
        )
    )
)
if not defined PORT (
    echo [错误] 端口 8501~8510 全部被占用，无法启动。
    echo        请先关闭占用端口的程序（可用 netstat -ano ^| findstr :8501 查看），
    echo        或指定其它端口：scripts\start.bat --port 8600
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)
if "%BUMPED%"=="1" (
    echo [提示] 端口 8501 已被占用，自动改用 %PORT%（浏览器请访问 http://localhost:%PORT%）。
) else (
    echo [端口] 使用端口 %PORT%
)
goto port_ready

:check_given_port
call :port_in_use %PORT%
if not errorlevel 1 (
    echo [错误] 指定端口 %PORT% 已被占用，请换一个端口或先运行 scripts\stop.bat %PORT%
    echo.
    if not defined QD_NO_PAUSE pause
    exit /b 1
)
echo [端口] 使用指定端口 %PORT%

:port_ready

rem ---------- 5. 启动日志路径 ----------
set "LOGDIR=%ROOT%\runtime\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "LOG=%LOGDIR%\start.log"
>>"%LOG%" echo [%DATE% %TIME%] ===== start.bat：Python=%PY% 端口=%PORT% =====
echo [日志] 启动日志将写入：%LOG%
echo.

if "%CHECK_ONLY%"=="1" (
    echo [检查模式] 环境/端口检查通过，本次不启动服务。
    echo [将执行] "%PY%" "%ROOT%\scripts\run_streamlit.py" --port %PORT% --log "%LOG%"
    echo [访问地址] http://localhost:%PORT%
    echo [停止方式] scripts\stop.bat %PORT% ^(或关闭运行服务的窗口^)
    endlocal
    exit /b 0
)

rem ---------- 6. 启动 Streamlit（控制台 + start.log 双输出） ----------
echo [启动] 正在启动 QuantDesk，首次启动可能需要数秒，请稍候...
echo [访问] 启动完成后，在浏览器打开：http://localhost:%PORT%
echo [提示] 服务运行期间请保持本窗口打开；关闭窗口或按 Ctrl+C 即停止服务。
echo [提示] 也可在另一个窗口运行：scripts\stop.bat %PORT%
echo ------------------------------------------------------------
"%PY%" "%ROOT%\scripts\run_streamlit.py" --port %PORT% --log "%LOG%"
set "EXITCODE=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo.

if "%EXITCODE%"=="0" (
    echo [提示] QuantDesk 已正常退出。
) else (
    echo [错误] QuantDesk 退出，退出码：%EXITCODE%
    echo        请查看本窗口上方错误信息与日志：%LOG%
    echo        可运行 scripts\check_env.py 检查环境；若为端口问题可运行 scripts\stop.bat
    echo        如刚用 scripts\stop.bat 停止，此非 0 退出码属正常。
)
echo.
if not defined QD_NO_PAUSE pause
endlocal
exit /b %EXITCODE%

rem ============================================================
rem 子过程：判断端口是否被监听（0=被占用，1=空闲）
rem ============================================================
:port_in_use
netstat -ano 2>nul | findstr /R /C:":%~1 .*LISTENING" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
