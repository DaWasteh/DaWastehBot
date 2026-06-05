"""Tests für PandaBot.

Decken die reine Logik ab (Sanitizing der LLM-Antworten und die
Erwähnungs-Erkennung), die ohne echte Twitch- oder LLM-Verbindung getestet
werden kann.
"""

from __future__ import annotations

import pytest

from pandabot import LLMClient, PandaBot


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.fixture
def bot() -> PandaBot:
    """Erzeugt einen Bot und simuliert den Zustand nach event_ready.

    Der Konstruktor kommt mit den CI-Dummy-Werten aus den Umgebungsvariablen
    klar. ``_bot_login`` wird hier von Hand gesetzt - zur Laufzeit füllt das
    ``event_ready`` aus ``self.user.name``.
    """
    instance = PandaBot()
    instance._bot_login = "dawastehbot"
    return instance


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PandaBot: Hallo zusammen!", "Hallo zusammen!"),
        ('"Na klar, viel Spaß!"', "Na klar, viel Spaß!"),
        ("  Bot: test  ", "test"),
        ("Normale Antwort", "Normale Antwort"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_sanitize(client: LLMClient, raw: str | None, expected: str | None) -> None:
    assert client._sanitize(raw) == expected


def test_sanitize_truncates_long_text(client: LLMClient) -> None:
    out = client._sanitize("A" * 600)
    assert out is not None
    assert len(out) <= 480
    assert out.endswith("…")


def test_sanitize_strips_own_name_case_insensitive(client: LLMClient) -> None:
    assert client._sanitize("pandabot: yo") == "yo"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Echter Account-Name (der eigentliche Bugfix).
        ("@dawastehbot mach mal was", True),
        ("dawastehbot hi", True),
        ("DaWasTehBot hallo", True),
        # Kosmetischer Spitzname funktioniert weiterhin.
        ("PandaBot wie gehts?", True),
        ("@PandaBot hallo", True),
        ("pandabot bist du da", True),
        # Keine Erwähnung.
        ("hallo zusammen", False),
        ("das war ein super stream", False),
        # Substring darf NICHT fälschlich triggern (Wortgrenze).
        ("ichliebedawastehbots", False),
    ],
)
def test_is_mention(bot: PandaBot, text: str, expected: bool) -> None:
    assert bot._is_mention(text) is expected


def test_is_mention_without_login_falls_back_to_botname(bot: PandaBot) -> None:
    """Vor event_ready ist nur der kosmetische Name als Trigger da."""
    bot._bot_login = None
    assert bot._is_mention("PandaBot hallo") is True
    assert bot._is_mention("@dawastehbot hallo") is False
