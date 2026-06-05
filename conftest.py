"""Pytest-Setup.

Setzt Dummy-Werte für die Twitch-Pflicht-Variablen, BEVOR ``config`` bzw.
``pandabot`` importiert werden. Ohne das würde der Import scheitern, weil
``config.py`` die Variablen schon beim Laden verlangt. So sind die Tests
selbstständig lauffähig - egal ob lokal oder in der CI.

Echte ``.env``-Werte werden hier bewusst nicht angefasst; ``setdefault``
überschreibt nur, was noch nicht gesetzt ist.
"""

from __future__ import annotations

import os

for key in (
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "TWITCH_BOT_ID",
    "TWITCH_OWNER_ID",
):
    os.environ.setdefault(key, "test-dummy")
