#!/usr/bin/env bash
# PandaBot GUI starten.
# tkinter ist Teil der Python-Standardbibliothek.
# Auf manchen Linux-Distributionen: sudo apt install python3-tk

set -e
cd "$(dirname "$0")"

if [ -f ".venv-gui/bin/python" ]; then
    PYTHON=".venv-gui/bin/python"
elif [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif command -v python3 &> /dev/null; then
    PYTHON="python3"
else
    echo "[FEHLER] Kein Python gefunden."
    exit 1
fi

if ! "$PYTHON" -c "import sv_ttk" >/dev/null 2>&1; then
    echo "[INFO] Installiere modernes GUI-Theme ..."
    "$PYTHON" -m pip install -r requirements-gui.txt
fi

exec "$PYTHON" pandabot_gui.py
