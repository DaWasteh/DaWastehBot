"""Tests für PandaBot.

Decken die reine Logik ab (Sanitizing der LLM-Antworten und die
Erwähnungs-Erkennung), die ohne echte Twitch- oder LLM-Verbindung getestet
werden kann.
"""

from __future__ import annotations

import pytest

from pandabot import (
    LANGUAGE_DEFAULT,
    LANGUAGE_ENGLISH,
    LANGUAGE_ICELANDIC,
    LANGUAGE_POLISH,
    LANGUAGE_SWEDISH,
    LLMClient,
    PandaBot,
    UserMemoryStore,
    detect_message_language,
    language_reply_instruction,
)


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("PandaBot servus, wie gehts?", LANGUAGE_DEFAULT),
        ("@PandaBot what are we doing today?", LANGUAGE_ENGLISH),
        ("!panda hej, vad händer idag?", LANGUAGE_SWEDISH),
        ("PandaBot hvað er í gangi?", LANGUAGE_ICELANDIC),
        ("PandaBot siema, co dzisiaj robimy?", LANGUAGE_POLISH),
        ("PandaBot cześć, wyjaśnij proszę ten stream", LANGUAGE_POLISH),
    ],
)
def test_detect_message_language(message: str, expected: str) -> None:
    assert detect_message_language(message) == expected


def test_language_instruction_current_message_overrides_memory_language() -> None:
    instruction = language_reply_instruction(LANGUAGE_ENGLISH, LANGUAGE_DEFAULT)

    assert "Reply to this message in natural English only" in instruction
    assert "darf die aktuelle Antwortsprache NICHT überschreiben" in instruction


def test_user_memory_language_profile_tracks_dominant_language(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    store.update_language_profile("42", "Bobczak", LANGUAGE_DEFAULT)
    store.update_language_profile("42", "Bobczak", LANGUAGE_DEFAULT)
    store.update_language_profile("42", "Bobczak", LANGUAGE_ENGLISH)
    memory = store.load("42", "Bobczak")

    assert store.dominant_language(memory) == LANGUAGE_DEFAULT
    assert "Deutsch/Bayrisch=2" in memory
    assert "English=1" in memory
