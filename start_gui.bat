@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"

REM Wechsle in den Ordner, in dem diese BAT-Datei liegt.
cd /d "%~dp0"

REM GUI bevorzugt eine eigene .venv-gui; der Bot nutzt weiterhin .venv.
REM tkinter ist Teil der Python-Standardbibliothek.
set "PYTHON=.venv-gui\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [INFO] Keine Projekt-venv gefunden, nutze System-Python.
    set "PYTHON=python"
)

"%PYTHON%" -c "import sv_ttk" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installiere modernes GUI-Theme ...
    "%PYTHON%" -m pip install -r requirements-gui.txt
    if errorlevel 1 (
        echo [FEHLER] GUI-Abhängigkeiten konnten nicht installiert werden.
        pause
        exit /b 1
    )
)

"%PYTHON%" pandabot_gui.py
if errorlevel 1 pause
