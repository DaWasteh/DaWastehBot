@echo off
setlocal

REM Wechsle in den Ordner, in dem diese BAT-Datei liegt.
cd /d "%~dp0"

REM Virtuelle Umgebung aktivieren.
if not exist ".venv\Scripts\activate.bat" (
    echo [FEHLER] .venv wurde nicht gefunden.
    echo Bitte erst installieren/anlegen, z.B.:
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

REM PandaBot starten.
python pandabot.py

REM Fenster offen lassen, falls der Bot beendet wird oder ein Fehler auftritt.
pause
