"""Tests für PandaBot.

Decken die reine Logik ab (Sanitizing der LLM-Antworten und die
Erwähnungs-Erkennung), die ohne echte Twitch- oder LLM-Verbindung getestet
werden kann.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config import Settings, settings
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
def bot(monkeypatch: pytest.MonkeyPatch) -> PandaBot:
    """Erzeugt einen Bot und simuliert den Zustand nach event_ready.

    Der Konstruktor kommt mit den CI-Dummy-Werten aus den Umgebungsvariablen
    klar. ``_bot_login`` wird hier von Hand gesetzt - zur Laufzeit füllt das
    ``event_ready`` aus ``self.user.name``.
    """
    monkeypatch.setattr(settings, "bot_name", "PandaBot")
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


def test_sanitize_removes_complete_think_block(client: LLMClient) -> None:
    raw = "<think>Ich analysiere intern viel zu lang.</think> Antwort: Servus, klar doch!"

    assert client._sanitize(raw) == "Servus, klar doch!"


def test_sanitize_rescues_answer_after_unclosed_think(client: LLMClient) -> None:
    raw = "<think>Okay, der User fragt nach dem Chat. Antwort: Im Chat war gerade Bot-Testen angesagt."

    assert client._sanitize(raw) == "Im Chat war gerade Bot-Testen angesagt."


def test_sanitize_drops_unclosed_think_without_answer(client: LLMClient) -> None:
    assert client._sanitize("<think>Okay, der User fragt nach dem Prompt") is None


def test_reasoning_detector_catches_meta_text(client: LLMClient) -> None:
    assert client._looks_like_reasoning("Okay, der User fragt nach dem Streamtitel.") is True
    assert client._looks_like_reasoning("Klar, ich helf dir kurz.") is False


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


def test_is_own_message_detects_bot_by_login(bot: PandaBot) -> None:
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="other-id", name="dawastehbot", display_name="DaWastehBot"),
        text="PandaBot fragt sich selbst was",
    )

    assert bot._is_own_message(payload) is True


def test_is_own_message_does_not_treat_mentions_as_self(bot: PandaBot) -> None:
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="chat-user", name="chatuser", display_name="ChatUser"),
        text="@dawastehbot bist du da?",
    )

    assert bot._is_own_message(payload) is False


def test_recent_bot_repeat_detection(bot: PandaBot) -> None:
    bot._remember_bot_message("Was meint ihr zum Bosskampf?")

    assert bot._is_recent_bot_repeat("Was meint ihr zum Bosskampf?") is True
    assert bot._is_recent_bot_repeat("Ganz andere Frage an den Chat!") is False


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


def test_apply_online_llm_backend_uses_google_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GOOGLE_LLM_MODEL",
        "GOOGLE_LLM_MAX_TOKENS",
        "GOOGLE_LLM_TIMEOUT",
        "GOOGLE_LLM_SERVER_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings_obj = Settings()
    settings_obj.google_api_key = "test-key"

    settings_obj.apply_llm_backend("online")

    assert settings_obj.llm_backend == "online"
    assert settings_obj.llm_model == "gemma-4-31b-it"
    assert settings_obj.llm_api_key == "test-key"
    assert settings_obj.llm_send_repeat_penalty is False
    assert settings_obj.llm_send_llama_extras is False
    assert settings_obj.llm_max_tokens == 120
    assert settings_obj.llm_timeout == 30


def test_apply_online_llm_backend_normalizes_common_gemini_typo() -> None:
    settings_obj = Settings()
    settings_obj.google_api_key = "test-key"
    settings_obj.google_llm_model = "gemini-4-31b-it"

    settings_obj.apply_llm_backend("online")

    assert settings_obj.llm_model == "gemma-4-31b-it"


def test_apply_online_llm_backend_requires_api_key() -> None:
    settings_obj = Settings()
    settings_obj.google_api_key = None
    settings_obj.llm_api_key = None

    with pytest.raises(RuntimeError):
        settings_obj.apply_llm_backend("online")


def test_user_memory_touch_creates_seen_chatter_file(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    store.touch("99", "ChatKumpel")
    memory = store.load("99", "ChatKumpel")

    assert "Twitch-User-ID: 99" in memory
    assert "Anzeigename zuletzt gesehen: ChatKumpel" in memory


def test_user_memory_touch_updates_display_name(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    store.touch("99", "AlterName")
    store.touch("99", "NeuerName")
    memory = store.load("99", "NeuerName")

    assert "Anzeigename zuletzt gesehen: NeuerName" in memory
    assert "AlterName" not in memory


def test_user_memory_touch_sanitizes_display_name(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    store.touch("99", "Chat\nKumpel\tXD")
    memory = store.load("99", "ChatKumpel")

    assert "Anzeigename zuletzt gesehen: Chat Kumpel XD" in memory
    assert "Chat\nKumpel" not in memory


def test_event_message_creates_memory_for_seen_chatter(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "user_memory_enabled", True)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="123", name="chatkumpel", display_name="ChatKumpel"),
        text="hallo zusammen",
    )

    asyncio.run(bot.event_message(payload))

    memory = (tmp_path / "123.md").read_text(encoding="utf-8")
    assert "Twitch-User-ID: 123" in memory
    assert "Anzeigename zuletzt gesehen: ChatKumpel" in memory


def test_user_memory_language_profile_tracks_dominant_language(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    store.update_language_profile("42", "Bobczak", LANGUAGE_DEFAULT)
    store.update_language_profile("42", "Bobczak", LANGUAGE_DEFAULT)
    store.update_language_profile("42", "Bobczak", LANGUAGE_ENGLISH)
    memory = store.load("42", "Bobczak")

    assert store.dominant_language(memory) == LANGUAGE_DEFAULT
    assert "Deutsch/Bayrisch=2" in memory
    assert "English=1" in memory
