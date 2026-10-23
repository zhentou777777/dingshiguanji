@echo off
chcp 65001 >nul
cd /d %~dp0

rem ============================================================
rem  SelfShutdown 一键构建脚本
rem  产出：本目录下的 SelfShutdown.exe（单文件，无控制台）
rem ============================================================

set "PYEXE="
if exist "D:\py\python.exe" set "PYEXE=D:\py\python.exe"
if "%PYEXE%"=="" (
    where py >nul 2>nul && set "PYEXE=py -3"
)

if "%PYEXE%"=="" (
    echo [错误] 未找到 Python。请先安装 Python 3.11 或更高版本，
    echo        安装时务必勾选 "tcl/tk and IDLE"（tkinter  GUI 依赖）。
    pause
    exit /b 1
)

echo [1/3] 创建虚拟环境 .venv ...
if not exist ".venv\Scripts\python.exe" (
    %PYEXE% -m venv .venv || goto :error
)

echo [2/3] 安装 pyinstaller ...
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller || goto :error

echo [3/3] 打包 SelfShutdown.exe ...
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole --name SelfShutdown --distpath . --workpath build --specpath build --clean src\main.py || goto :error

echo.
echo ============================================================
echo  构建完成！产物：%~dp0SelfShutdown.exe
echo  双击即可使用；配置文件 config.json 会自动生成在 EXE 同目录。
echo ============================================================
pause
exit /b 0

:error
echo.
echo [错误] 构建失败，请查看上方输出排查。
pause
exit /b 1
