@echo off
rem ============================================================
rem  Clash Upload Monitor - Windows launcher
rem  Just double-click this file. It locates Python automatically.
rem ============================================================
setlocal
chcp 65001 >nul
title Clash Upload Monitor

cd /d "%~dp0"

rem ---- locate a usable Python interpreter ----
set "PY_EXE="
where python >nul 2>&1 && set "PY_EXE=python"
if not defined PY_EXE (
    where py >nul 2>&1 && set "PY_EXE=py"
)
if not defined PY_EXE (
    if exist "C:\Python313\python.exe" set "PY_EXE=C:\Python313\python.exe"
)
if not defined PY_EXE (
    echo.
    echo   [!] Python not found on this machine.
    echo       Please install Python 3.8+ and make sure it is on PATH:
    echo       https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

if not exist "monitor.py" (
    echo.
    echo   [!] monitor.py not found next to this launcher.
    echo       Expected: %~dp0monitor.py
    echo.
    pause
    exit /b 1
)

%PY_EXE% monitor.py menu

if errorlevel 1 pause
endlocal
