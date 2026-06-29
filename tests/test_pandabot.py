"""Tests für PandaBot.

Decken die reine Logik ab (Sanitizing der LLM-Antworten und die
Erwähnungs-Erkennung), die ohne echte Twitch- oder LLM-Verbindung getestet
werden kann.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    import twitchio

from config import Settings, settings
from pandabot import (
    LANGUAGE_DEFAULT,
    LANGUAGE_ENGLISH,
    LANGUAGE_ICELANDIC,
    LANGUAGE_POLISH,
    LANGUAGE_SWEDISH,
    LLMClient,
    PandaBot,
    StreamContext,
    UserMemoryStore,
    build_system_prompt,
    clean_stream_title_parts,
    detect_message_language,
    language_reply_instruction,
)


def _chat_message(payload: object) -> twitchio.ChatMessage:
    """Castet duck-typed Test-Payloads für mypy zu TwitchIO-ChatMessage."""
    return cast("twitchio.ChatMessage", payload)


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


def test_sanitize_removes_gemma_thought_block(client: LLMClient) -> None:
    raw = (
        "<thought>* User: DaWasteh. * Input: photosynthese chemische formel. "
        "* Constraints: No Markdown.</thought> Antwort: Photosynthese kurz: "
        "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."
    )

    assert (
        client._sanitize(raw)
        == "Photosynthese kurz: 6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."
    )


def test_sanitize_rescues_answer_after_unclosed_think(client: LLMClient) -> None:
    raw = "<think>Okay, der User fragt nach dem Chat. Antwort: Im Chat war gerade Bot-Testen angesagt."

    assert client._sanitize(raw) == "Im Chat war gerade Bot-Testen angesagt."


def test_sanitize_rescues_answer_after_unclosed_gemma_thought(client: LLMClient) -> None:
    raw = "<thought>Der User fragt nach Chemie. Antwort: Die Formel ist 6 CO₂ + 6 H₂O → C₆H₁₂O₆ + 6 O₂."

    assert client._sanitize(raw) == "Die Formel ist 6 CO₂ + 6 H₂O → C₆H₁₂O₆ + 6 O₂."


def test_sanitize_drops_unclosed_think_without_answer(client: LLMClient) -> None:
    assert client._sanitize("<think>Okay, der User fragt nach dem Prompt") is None


def test_sanitize_drops_gemma_thought_only(client: LLMClient) -> None:
    assert client._sanitize("<thought>* User: DaWasteh. * Input: test.</thought>") is None


def test_sanitize_rescues_answer_when_gemma_wraps_everything_in_thought(client: LLMClient) -> None:
    raw = (
        "<thought>* User: DaWasteh (the streamer).\n"
        "* Request: photosynthese chemische formel\n"
        "* Constraints: No markdown, short and snappy.\n\n"
        "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."
        "</thought>"
    )

    assert client._sanitize(raw) == "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."


def test_sanitize_rescues_answer_from_same_line_gemma_thought(client: LLMClient) -> None:
    raw = (
        "<thought>* User: DaWasteh. * Request: photosynthese chemische formel. "
        "* 6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂.</thought>"
    )

    assert client._sanitize(raw) == "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."


def test_sanitize_drops_truncated_gemma_thought_without_complete_answer(client: LLMClient) -> None:
    raw = "<thought>* User: DaWasteh. * Request: photosynthese chemische formel. * 6 CO2 + 6 H"

    assert client._sanitize(raw) is None


def test_fallback_answers_photosynthesis_formula(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("!panda photosynthese chemische formel")
        == "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."
    )


def test_fallback_answers_decarboxylation_formula(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("!panda chemische formel für decarboxilierung")
        == "Allgemein: R-COOH → R-H + CO₂. Kurz: Carboxylgruppe ab, CO₂ raus."
    )


def test_fallback_answers_keto_enol_formula(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("!panda chemische formel für keto-enol-tautomerie")
        == "Keto-Enol-Tautomerie: R-CO-CH₂-R′ ⇌ R-C(OH)=CH-R′. Das ist Ketoform ↔ Enolform."
    )


def test_fallback_answers_mtp_llm_as_mcp(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("!panda erklär mir MTP in bezug auf LLM's")
        == "Meinst du MCP? Das ist wie ein USB-C-Port für LLMs: Tools, Dateien oder APIs werden standardisiert ans Modell angedockt."
    )


def test_fallback_answers_comfyui_video_question(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("!panda wie macht man in ComfyUI am besten Videos??")
        == "In ComfyUI am besten mit AnimateDiff oder WAN/I2V starten: kurze Clips, feste Seed/Resolution, dann upscalen/interpolieren. Erst Workflow stabil kriegen, dann Qualität hochdrehen."
    )


def test_fallback_answers_anthropic_fable_opinion(bot: PandaBot) -> None:
    assert (
        bot._fallback_reply("was is deine meinung zu anthropic fable 5")
        == "Fable 5 klingt spannend, vor allem wenn Anthropic bei Coding und Agenten nochmal nachlegt. Aber ich würd’s erst nach echten Benchmarks hypen – Marketing kann jeder."
    )


@pytest.mark.parametrize(
    "raw",
    [
        "Response Language: Deutsch/Bayrisch (informal, casual).",
        "Requested language: Deutsch/Bayrisch (informal/loose).",
        'Current Query: "wie macht man in ComfyUI am besten Videos??" (How to best make videos in ComfyUI??).',
        'Current Prompt: "@dawastehbot um was gehts heute?" (What\'s it about today?).',
        'Query: "was passiert heute so" (What\'s happening today?).',
        "Persona: dawastehbot (friendly, witty, conversational, chat buddy, slightly cheeky).",
        "Stil: natürlich, direkt und conversational.",
    ],
)
def test_sanitize_blocks_prompt_metadata(client: LLMClient, raw: str) -> None:
    sanitized = client._sanitize(raw)

    assert sanitized is not None
    assert client._looks_like_reasoning(sanitized) is True


def test_reasoning_detector_catches_meta_text(client: LLMClient) -> None:
    assert client._looks_like_reasoning("Okay, der User fragt nach dem Streamtitel.") is True
    assert (
        client._looks_like_reasoning("Response Language: Deutsch/Bayrisch (informal, casual).")
        is True
    )
    assert (
        client._looks_like_reasoning(
            'Current Query: "wie macht man in ComfyUI am besten Videos??" (How to best make videos in ComfyUI??).'
        )
        is True
    )
    assert (
        client._looks_like_reasoning('Query: "was passiert heute so" (What\'s happening today?).')
        is True
    )
    assert (
        client._looks_like_reasoning(
            "Persona: dawastehbot (friendly, witty, natural, conversational, chat buddy)."
        )
        is True
    )
    assert client._looks_like_reasoning("ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc'.") is True
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

    assert bot._is_own_message(_chat_message(payload)) is True


def test_is_own_message_does_not_treat_mentions_as_self(bot: PandaBot) -> None:
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="chat-user", name="chatuser", display_name="ChatUser"),
        text="@dawastehbot bist du da?",
    )

    assert bot._is_own_message(_chat_message(payload)) is False


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

    assert "Write the chat reply in natural English only" in instruction
    assert "darf die aktuelle Antwortsprache NICHT überschreiben" in instruction


def test_complete_reply_retries_after_empty_or_metadata_reply(bot: PandaBot) -> None:
    class StubLLM:
        def __init__(self) -> None:
            self.calls: list[list[tuple[str, str]]] = []

        async def complete(
            self, _system_prompt: str, turns: list[tuple[str, str]], **_kwargs: object
        ) -> str | None:
            self.calls.append(list(turns))
            if len(self.calls) == 1:
                return None
            return "Allgemein: R-COOH → R-H + CO₂."

    stub = StubLLM()
    bot.llm = stub  # type: ignore[assignment]

    reply = asyncio.run(
        bot._complete_reply("system", [("user", "DaWasteh: chemie frage")], LANGUAGE_DEFAULT)
    )

    assert reply == "Allgemein: R-COOH → R-H + CO₂."
    assert len(stub.calls) == 2
    # Der Retry hängt nur einen weiteren User-Turn an - ohne Beispiel-Labels,
    # die das Modell sonst lernen und zurückspiegeln könnte.
    retry_role, retry_content = stub.calls[1][-1]
    assert retry_role == "user"
    assert "leer" in retry_content
    assert "Persona" not in retry_content


def test_stream_context_question_handles_bot_mention_and_today(bot: PandaBot) -> None:
    assert bot._is_stream_context_question("@dawastehbot um was gehts heute?") is True
    assert bot._is_stream_context_question("!panda was passiert heute so") is True
    assert bot._is_stream_context_question("was passiert heute so") is True


def test_stream_context_answer_keeps_real_title_but_drops_commands(bot: PandaBot) -> None:
    bot.context.game = "Software and Game Development"
    bot.context.title = "[BY/GER/EN] | I Need You ! - ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc"

    reply = bot._stream_context_answer(LANGUAGE_DEFAULT)

    assert "I Need You - ComfyUI" in reply
    assert "!panda" not in reply
    assert "420" not in reply


def test_stream_title_leak_is_blocked_and_comfyui_fallback_answers(bot: PandaBot) -> None:
    assert bot.llm._looks_like_reasoning("ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc'.") is True
    assert (
        bot._fallback_reply("!panda wie macht man in ComfyUI am besten Videos??")
        == "In ComfyUI am besten mit AnimateDiff oder WAN/I2V starten: kurze Clips, feste Seed/Resolution, dann upscalen/interpolieren. Erst Workflow stabil kriegen, dann Qualität hochdrehen."
    )


@pytest.mark.parametrize(
    "leak",
    [
        "ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc'.",
        'Current Query: "wie macht man in ComfyUI am besten Videos??" (How to best make videos in ComfyUI??).',
        'Query: "wie macht man in ComfyUI am besten Videos??" (How to best make videos in ComfyUI??).',
    ],
)
def test_respond_replaces_llm_leak_with_comfyui_fallback(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch, leak: str
) -> None:
    class StubLLM:
        def __init__(self, response: str) -> None:
            self.response = response
            self.guard = LLMClient()

        async def complete(self, *_args: object, **_kwargs: object) -> str:
            return self.response

        def _looks_like_reasoning(self, text: str) -> bool:
            return self.guard._looks_like_reasoning(text)

    class Payload:
        chatter = SimpleNamespace(id="166166593", name="dawasteh", display_name="DaWasteh")
        text = "!panda wie macht man in ComfyUI am besten Videos??"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def respond(self, text: str) -> None:
            self.sent.append(text)

    monkeypatch.setattr(settings, "user_memory_enabled", False)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    bot.llm = StubLLM(leak)  # type: ignore[assignment]
    payload = Payload()

    asyncio.run(
        bot._respond(
            _chat_message(payload),
            author="DaWasteh",
            trigger="!panda wie macht man in ComfyUI am besten Videos??",
        )
    )

    assert payload.sent == [
        "In ComfyUI am besten mit AnimateDiff oder WAN/I2V starten: kurze Clips, feste Seed/Resolution, dann upscalen/interpolieren. Erst Workflow stabil kriegen, dann Qualität hochdrehen."
    ]


@pytest.mark.parametrize(
    "trigger",
    [
        "@dawastehbot um was gehts heute?",
        "!panda was passiert heute so",
        "was passiert heute so",
    ],
)
def test_respond_stream_context_question_bypasses_llm_and_answers_context(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    class FailingLLM:
        def _looks_like_reasoning(self, _text: str) -> bool:
            return False

        async def complete(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("LLM must not be called for stream-context questions")

    class Payload:
        chatter = SimpleNamespace(id="166166593", name="dawasteh", display_name="DaWasteh")

        def __init__(self, text: str) -> None:
            self.text = text
            self.sent: list[str] = []

        async def respond(self, text: str) -> None:
            self.sent.append(text)

    monkeypatch.setattr(settings, "user_memory_enabled", False)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    bot.context.game = "Software and Game Development"
    bot.context.title = "[BY/GER/EN] | I Need You ! - ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc"
    bot.llm = FailingLLM()  # type: ignore[assignment]
    payload = Payload(trigger)

    asyncio.run(bot._respond(_chat_message(payload), author="DaWasteh", trigger=trigger))

    assert payload.sent == [
        "Grob geht’s um Software and Game Development; heute offenbar mit Fokus auf I Need You - ComfyUI."
    ]


def test_respond_uses_opinion_fallback_when_gemma_returns_only_thoughts(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ThoughtOnlyLLM:
        def __init__(self) -> None:
            self.guard = LLMClient()
            self.calls = 0

        async def complete(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            return None

        def _looks_like_reasoning(self, text: str) -> bool:
            return self.guard._looks_like_reasoning(text)

    class Payload:
        chatter = SimpleNamespace(id="166166593", name="dawasteh", display_name="DaWasteh")
        text = "!panda was is deine meinung zu anthropic fable 5"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def respond(self, text: str) -> None:
            self.sent.append(text)

    monkeypatch.setattr(settings, "user_memory_enabled", False)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    llm = ThoughtOnlyLLM()
    bot.llm = llm  # type: ignore[assignment]
    payload = Payload()

    asyncio.run(
        bot._respond(
            _chat_message(payload),
            author="DaWasteh",
            trigger="was is deine meinung zu anthropic fable 5",
        )
    )

    assert payload.sent == [
        "Fable 5 klingt spannend, vor allem wenn Anthropic bei Coding und Agenten nochmal nachlegt. Aber ich würd’s erst nach echten Benchmarks hypen – Marketing kann jeder."
    ]
    assert llm.calls == 2


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
    assert settings_obj.llm_use_system_role is False
    assert settings_obj.llm_max_tokens == 200
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

    asyncio.run(bot.event_message(_chat_message(payload)))

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


def test_clean_stream_title_parts_drops_tags_commands_and_deko() -> None:
    parts = clean_stream_title_parts(
        "[BY/GER/EN] | I Need You ! - ComfyUI | 🥦 420 🥦 | !panda !lurk !git !dc"
    )

    assert parts == ["I Need You - ComfyUI"]


def test_build_system_prompt_uses_cleaned_title() -> None:
    ctx = StreamContext()
    ctx.game = "Software and Game Development"
    ctx.title = "[BY/GER/EN] | ComfyUI Deep Dive | 🥦 420 🥦 | !panda !lurk"

    prompt = build_system_prompt(ctx)

    assert "ComfyUI Deep Dive" in prompt
    assert "!panda" not in prompt
    assert "[BY/GER/EN]" not in prompt
    assert "420" not in prompt


def test_build_system_prompt_skips_unknown_title() -> None:
    ctx = StreamContext()
    ctx.game = "Just Chatting"
    ctx.title = "Unbekannt"

    prompt = build_system_prompt(ctx)

    assert "Just Chatting" in prompt
    assert "Unbekannt" not in prompt


def test_build_messages_merges_system_into_first_user_turn_without_system_role(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemma & Co. ohne System-Rolle: Anweisungen landen in der ersten User-Nachricht."""
    monkeypatch.setattr(settings, "llm_use_system_role", False)
    monkeypatch.setattr(settings, "llm_send_llama_extras", False)

    messages = client._build_messages(
        "SYSTEM-ANWEISUNGEN",
        [
            ("user", "Alice: hi"),
            ("user", "Bob: yo"),
            ("assistant", "Servus ihr zwei!"),
            ("user", "Alice: PandaBot erzähl was"),
        ],
        final_label="Antwort",
        allow_prefill=True,
    )

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"].startswith("SYSTEM-ANWEISUNGEN")
    # Aufeinanderfolgende User-Nachrichten werden zusammengelegt (strikte
    # Templates wie Gemma verlangen abwechselnde Rollen).
    assert "Alice: hi\nBob: yo" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Alice: PandaBot erzähl was"}


def test_build_messages_keeps_system_role_and_folds_leading_bot_turns(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_use_system_role", True)
    monkeypatch.setattr(settings, "llm_send_llama_extras", False)

    messages = client._build_messages(
        "SYSTEM",
        [("assistant", "Ich bin wach!"), ("user", "Alice: hi")],
        final_label="Antwort",
        allow_prefill=True,
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    assert "Ich bin wach!" in messages[0]["content"]
    assert messages[1]["content"] == "Alice: hi"


def test_build_messages_adds_prefill_only_for_llama_extras(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_use_system_role", True)
    monkeypatch.setattr(settings, "llm_send_llama_extras", True)

    messages = client._build_messages(
        "SYS", [("user", "Alice: hi")], final_label="Antwort", allow_prefill=True
    )

    assert messages[-1] == {"role": "assistant", "content": "<think></think>\nAntwort:"}

    no_prefill = client._build_messages(
        "SYS", [("user", "Alice: hi")], final_label="Antwort", allow_prefill=False
    )

    assert no_prefill[-1]["role"] == "user"


def test_remember_bot_message_dedupes_eventsub_echo(bot: PandaBot) -> None:
    bot._remember_bot_message("Servus Chat!")
    bot._remember_bot_message("Servus Chat!")  # EventSub-Echo der eigenen Nachricht

    assert list(bot.chat_history) == [("assistant", "Servus Chat!")]
    assert list(bot._recent_bot_messages) == ["Servus Chat!"]


def test_event_message_appends_user_turn_to_history(
    bot: PandaBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "user_memory_enabled", False)
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="123", name="chatkumpel", display_name="ChatKumpel"),
        text="hallo zusammen",
    )

    asyncio.run(bot.event_message(_chat_message(payload)))

    assert list(bot.chat_history) == [("user", "ChatKumpel: hallo zusammen")]


def test_history_turns_excludes_current_user_message(bot: PandaBot) -> None:
    bot.chat_history.append(("user", "Alice: hi"))
    bot.chat_history.append(("assistant", "Servus Alice!"))
    bot.chat_history.append(("user", "Alice: PandaBot erzähl was"))

    turns = bot._history_turns(limit=10, current=("user", "Alice: PandaBot erzähl was"))

    assert turns == [("user", "Alice: hi"), ("assistant", "Servus Alice!")]


# --------------------------------------------------------------------------- #
#  Neue Tests: reichhaltige Profile, adaptiver Idle-Scheduler, MoE-Modell     #
# --------------------------------------------------------------------------- #
def test_record_interaction_increments_and_persists(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))

    count = store.record_interaction("77", "Rita")
    assert count == 1
    count = store.record_interaction("77", "Rita")
    assert count == 2

    memory = store.load("77", "Rita")
    assert store.interaction_count(memory) == 2
    assert "- Interaktionen: 2" in memory


@pytest.mark.parametrize(
    ("after", "interval", "count", "expected"),
    [
        (2, 5, 1, False),
        (2, 5, 2, True),
        (2, 5, 3, False),
        (2, 5, 5, False),
        (2, 5, 7, True),
        (2, 5, 12, True),
        (0, 5, 5, False),  # komplett deaktiviert
        (2, 0, 2, True),   # nur die erste Zusammenfassung
    ],
)
def test_should_summarize(
    monkeypatch: pytest.MonkeyPatch, after: int, interval: int, count: int, expected: bool
) -> None:
    monkeypatch.setattr(settings, "profile_summary_after", after)
    monkeypatch.setattr(settings, "profile_summary_interval", interval)
    store = UserMemoryStore("ignored")
    assert store.should_summarize(count) is expected


def test_rewrite_notes_replaces_section(tmp_path) -> None:
    store = UserMemoryStore(str(tmp_path))
    store.append("5", "Felix", "- Felix mag Katzen")
    store.append("5", "Felix", "- alte temporäre Notiz")

    store.rewrite_notes(
        "5",
        "Felix",
        "- Anrede: einfach Felix\n- Interessen: Katzen, Synthesizer\n- Stil: trocken-frech",
    )
    memory = store.load("5", "Felix")

    assert "alte temporäre Notiz" not in memory
    assert "mag Katzen" not in memory
    assert "Interessen: Katzen, Synthesizer" in memory
    assert "Stil: trocken-frech" in memory


def test_rewrite_notes_dedups_near_identical_bullets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "profile_max_notes", 10)
    store = UserMemoryStore(str(tmp_path))

    store.rewrite_notes(
        "6",
        "Nina",
        "- Nina spielt gerne Synthesizer\n"
        "- Nina spielt gern Synthesizer\n"  # fast identisch -> wird gedroppt
        "- Nina hat einen Hund namens Bello",
    )
    memory = store.load("6", "Nina")

    assert memory.count("Synthesizer") == 1
    assert "Hund namens Bello" in memory


def test_normalized_google_model_handles_a4b_aliases() -> None:
    fresh = Settings()
    for alias in (
        "gemma-4-26b-a4b",
        "gemini-4-26b-a4b",
        "google/gemma-4-26b-a4b",
        "gemma-4-26b-it-a4b",
        "gemma-4-26b-a4b-it",
    ):
        fresh.google_llm_model = alias
        assert fresh._normalized_google_model() == "gemma-4-26b-a4b", alias


def test_apply_llm_backend_online_with_a4b_override(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = Settings()
    monkeypatch.setattr(fresh, "google_api_key", "test-key")

    fresh.apply_llm_backend("online", online_model="gemma-4-26b-a4b")

    assert fresh.llm_backend == "online"
    assert fresh.llm_model == "gemma-4-26b-a4b"
    assert fresh.llm_send_repeat_penalty is False
    assert fresh.llm_send_llama_extras is False
    assert fresh.llm_use_system_role is False  # Gemma kennt keine System-Rolle


def test_apply_llm_backend_menu_shortcut_3_picks_a4b(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = Settings()
    monkeypatch.setattr(fresh, "google_api_key", "test-key")

    fresh.apply_llm_backend("3")

    assert fresh.llm_model == "gemma-4-26b-a4b"


def test_idle_next_delay_is_adaptive(
    bot: PandaBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "idle_threshold", 900)
    monkeypatch.setattr(settings, "idle_jitter", 90)
    monkeypatch.setattr(settings, "idle_max_solo_messages", 1)
    bot._last_activity = time.monotonic()

    delay = bot._idle_next_delay()

    # Ereignisgesteuert: schläft bis etwa threshold (+ Jitter), nicht starr 60s.
    assert 900.0 <= delay <= 990.0


def test_idle_next_delay_floors_to_one_second(bot: PandaBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "idle_threshold", 10)
    monkeypatch.setattr(settings, "idle_jitter", 0)
    monkeypatch.setattr(settings, "idle_max_solo_messages", 1)
    bot._last_activity = time.monotonic() - 10000  # Deadline lange vorbei

    assert bot._idle_next_delay() == 1.0


def test_idle_next_delay_returns_long_sleep_when_disabled(
    bot: PandaBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "idle_max_solo_messages", 0)
    assert bot._idle_next_delay() == 300.0


def test_opener_repeat_detection(bot: PandaBot) -> None:
    bot._remember_opener("Hey Leute, was macht ihr heute so im Stream?")

    assert bot._is_recent_opener_repeat("Hey Leute, was macht ihr morgen so?")
    assert not bot._is_recent_opener_repeat("Welches Spiel soll ich als nächstes anschauen?")


def test_record_user_interaction_buffers_pairs(bot: PandaBot) -> None:
    bot._record_user_interaction("1", "Hallo Panda", "Servus!")

    assert dict(bot._user_interactions)["1"][-1] == ("Hallo Panda", "Servus!")


def test_system_prompt_forbids_addressing_lurkers() -> None:
    ctx = StreamContext()
    ctx.game = "Just Chatting"
    ctx.title = "Quatsch"
    prompt = build_system_prompt(ctx)

    assert "LURKER" in prompt
    assert "Lurker" in prompt


def test_maybe_summarize_profile_gates_on_threshold(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consolidation darf erst nach PROFILE_SUMMARY_AFTER Interaktionen feuern."""
    monkeypatch.setattr(settings, "user_memory_enabled", True)
    monkeypatch.setattr(settings, "profile_summary_after", 3)
    monkeypatch.setattr(settings, "profile_summary_interval", 10)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    started: list[str] = []

    async def fake_summarize(*, user_id: str, author: str) -> None:
        started.append(user_id)

    monkeypatch.setattr(bot, "_summarize_profile_later", fake_summarize)

    async def scenario() -> None:
        bot.user_memory.record_interaction("9", "Niki")  # count 1
        bot.user_memory.record_interaction("9", "Niki")  # count 2
        bot._maybe_summarize_profile("9", "Niki")
        await asyncio.sleep(0)  # ggf. geplante Tasks laufen lassen
        assert started == []

        bot.user_memory.record_interaction("9", "Niki")  # count 3 -> fällig
        bot._maybe_summarize_profile("9", "Niki")
        await asyncio.sleep(0)
        assert started == ["9"]

    asyncio.run(scenario())


def test_event_message_increments_interaction_count(
    bot: PandaBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "user_memory_enabled", True)
    bot.user_memory = UserMemoryStore(str(tmp_path))
    payload = SimpleNamespace(
        chatter=SimpleNamespace(id="321", name="kiwi", display_name="Kiwi"),
        text="servus",
    )

    asyncio.run(bot.event_message(_chat_message(payload)))

    memory = (tmp_path / "321.md").read_text(encoding="utf-8")
    assert "- Interaktionen: 1" in memory
