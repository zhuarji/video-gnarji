@echo off
chcp 65001 >nul
title VideoGrab
cd /d "%~dp0"

echo ==========================================
echo    VideoGrab - Local Video Downloader
echo ==========================================
echo.

REM ---- 1. Locate Python ----
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py -3")
if not defined PY (
    echo [X] Python not found
    echo.
    echo     Download from https://www.python.org/downloads/
    echo     IMPORTANT: check "Add Python to PATH" during install
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo     Python: %%v

REM ---- 2. Virtual environment ----
if not exist ".venv\Scripts\python.exe" (
    echo     Creating virtual environment...
    %PY% -m venv .venv 2>nul
)
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo     Virtual environment ready
)

REM ---- 3. Dependencies ----
echo     Checking dependencies...
%PY% -c "import yt_dlp, fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo     Installing yt-dlp / fastapi / uvicorn, about 1 min...
    %PY% -m pip install -U yt-dlp fastapi uvicorn
    if errorlevel 1 (
        echo     Retrying with Tsinghua mirror...
        %PY% -m pip install -U yt-dlp fastapi uvicorn -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    echo     Dependencies installed
) else (
    echo     Dependencies ready
)

REM ---- 4. ffmpeg ----
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo     [!] ffmpeg not found - basic download still works,
    echo         but merging HD streams and MP3 conversion need it
    echo         Install: winget install ffmpeg
) else (
    echo     ffmpeg ready
)

REM ---- 5. Start ----
if not exist "downloads" mkdir downloads
echo.
echo ==========================================
echo     Open in browser:  http://localhost:8080
echo     Stop service:     Ctrl + C
echo ==========================================
echo.

start "" http://localhost:8080
%PY% app.py
pause
