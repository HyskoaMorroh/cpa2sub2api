@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title CPA to sub2api - One Click Import

REM Force Python into UTF-8 mode so Chinese prints correctly
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM ---- locate Python (python, then py launcher) ----
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python 3 not found.
    echo Install from https://www.python.org and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

REM ---- ensure PyYAML ----
%PY% -c "import yaml" >nul 2>&1
if errorlevel 1 (
    echo Installing PyYAML ...
    %PY% -m pip install --quiet PyYAML
    %PY% -c "import yaml" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] PyYAML install failed. Check your network.
        pause
        exit /b 1
    )
)

%PY% run.py
set RC=%errorlevel%

echo.
if not "%RC%"=="0" echo [FAILED] exit code %RC%
pause
exit /b %RC%
