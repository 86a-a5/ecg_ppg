@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0launch_gui.py" %*
    goto :end
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0launch_gui.py" %*
    goto :end
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0launch_gui.py" %*
    goto :end
)

echo [ERROR] Install Python with tkinter, create .venv and install requirements.txt.
pause

:end
