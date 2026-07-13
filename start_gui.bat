@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"

REM Wechsle in den Ordner, in dem diese BAT-Datei liegt.
cd /d "%~dp0"

REM GUI bevorzugt eine eigene .venv-gui; der Bot nutzt weiterhin .venv.
REM tkinter ist Teil der Python-Standardbibliothek.
set "VENVDIR=.venv-gui"
if not exist "%VENVDIR%\Scripts\python.exe" set "VENVDIR=.venv"
set "PYTHON=%VENVDIR%\Scripts\python.exe"
set "PYTHONW=%VENVDIR%\Scripts\pythonw.exe"
if not exist "%PYTHON%" (
    echo [INFO] Keine Projekt-venv gefunden, nutze System-Python.
    set "PYTHON=python"
    set "PYTHONW=pythonw"
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

REM GUI ohne Konsolenfenster starten (pythonw); dieses cmd-Fenster schließt
REM sich sofort. Fallback auf python.exe, falls pythonw in der venv fehlt.
if /i not "%PYTHONW%"=="pythonw" if not exist "%PYTHONW%" set "PYTHONW=%PYTHON%"
start "PandaBot GUI" "%PYTHONW%" pandabot_gui.py
exit /b 0
