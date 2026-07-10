"""PandaBot - Ein lokaler KI-Chatbot für Twitch.

Verbindet einen Twitch-Kanal via TwitchIO 3 (EventSub über WebSocket) mit einem
LLM (lokaler llama-server oder Google/Gemma, OpenAI-kompatibel). Der Bot folgt dem Chat, antwortet
auf Erwähnungen und sorgt bei Stille für Unterhaltung. Stream-Titel und Spiel
werden live über die Twitch-Helix-API geholt.

Getestet mit TwitchIO 3.2.2 / Python 3.11+.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import random
import re
import sys
import time
from collections import deque
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from config import settings

LOGGER: logging.Logger = logging.getLogger("pandabot")

BOT_SCOPES = "user:read:chat user:write:chat user:bot"
OWNER_SCOPES = "channel:bot"

LANGUAGE_DEFAULT = "Deutsch/Bayrisch"
LANGUAGE_ENGLISH = "English"
LANGUAGE_SWEDISH = "Svenska"
LANGUAGE_ICELANDIC = "Íslenska"
LANGUAGE_POLISH = "Polski"
LANGUAGE_OTHER = "Sprache der aktuellen Anfrage"
SUPPORTED_LANGUAGES = (
    LANGUAGE_DEFAULT,
    LANGUAGE_ENGLISH,
    LANGUAGE_SWEDISH,
    LANGUAGE_ICELANDIC,
    LANGUAGE_POLISH,
)

LANGUAGE_WORDS: dict[str, set[str]] = {
    LANGUAGE_DEFAULT: {
        "servus",
        "griaß",
        "gruess",
        "hallo",
        "moin",
        "was",
        "wie",
        "warum",
        "wann",
        "wo",
        "wer",
        "wieso",
        "bitte",
        "danke",
        "nicht",
        "nichts",
        "ich",
        "du",
        "mir",
        "mein",
        "meine",
        "dein",
        "geht",
        "gehts",
        "erzähl",
        "erzaehl",
        "mach",
        "kannst",
        "bist",
        "ist",
        "stream",
        "oida",
        "fei",
        "ned",
    },
    LANGUAGE_ENGLISH: {
        "hi",
        "hello",
        "hey",
        "what",
        "how",
        "why",
        "when",
        "where",
        "who",
        "please",
        "thanks",
        "thank",
        "tell",
        "explain",
        "can",
        "could",
        "would",
        "should",
        "you",
        "your",
        "the",
        "and",
        "is",
        "are",
        "am",
        "not",
        "stream",
        "joke",
        "story",
    },
    LANGUAGE_SWEDISH: {
        "hej",
        "hallå",
        "halla",
        "vad",
        "hur",
        "varför",
        "varfor",
        "när",
        "nar",
        "var",
        "vem",
        "snälla",
        "snalla",
        "tack",
        "berätta",
        "beratta",
        "förklara",
        "forklara",
        "jag",
        "du",
        "din",
        "är",
        "ar",
        "inte",
        "och",
        "på",
        "pa",
        "det",
        "här",
        "har",
    },
    LANGUAGE_POLISH: {
        "cześć",
        "czesc",
        "siema",
        "hej",
        "witam",
        "podaj",
        "daj",
        "mi",
        "na",
        "do",
        "z",
        "co",
        "jak",
        "dlaczego",
        "kiedy",
        "gdzie",
        "kto",
        "proszę",
        "prosze",
        "dzięki",
        "dzieki",
        "dziękuję",
        "dziekuje",
        "powiedz",
        "opowiedz",
        "wyjaśnij",
        "wyjasnij",
        "przepis",
        "świetne",
        "swietne",
        "dobry",
        "dobre",
        "makaron",
        "sos",
        "mięso",
        "mieso",
        "cebula",
        "czosnek",
        "pomidory",
        "gotuj",
        "podsmaż",
        "podsmaz",
        "dodaj",
        "ja",
        "ty",
        "twój",
        "twoj",
        "jest",
        "nie",
        "i",
        "że",
        "ze",
        "się",
        "sie",
        "stream",
        "żart",
        "zart",
        "historię",
        "historie",
    },
    LANGUAGE_ICELANDIC: {
        "hæ",
        "hae",
        "halló",
        "hallo",
        "hvað",
        "hvad",
        "hvernig",
        "afhverju",
        "hvenær",
        "hvenaer",
        "hvar",
        "hver",
        "vinsamlegast",
        "takk",
        "segðu",
        "segdu",
        "útskýrðu",
        "utskyrdu",
        "ég",
        "eg",
        "þú",
        "thu",
        "þitt",
        "er",
        "ekki",
        "og",
        "á",
        "a",
        "þetta",
        "thetta",
    },
}


def _message_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ž]+", text.lower())


def detect_message_language(text: str) -> str:
    """Erkennt die wahrscheinlichste Sprache der aktuellen Chat-Nachricht.

    Absichtlich leichtgewichtig und deterministisch: Die Antwortsprache soll von
    der aktuellen Frage kommen, nicht von einer gespeicherten User-Notiz. Wenn
    die Nachricht zu kurz oder uneindeutig ist, bleibt der Stream-Default
    Deutsch/Bayrisch.
    """
    lowered = text.lower()
    lowered = re.sub(r"!panda\b", " ", lowered)
    lowered = re.sub(r"@?\b(?:pandabot|dawastehbot)\b", " ", lowered)
    words = _message_words(lowered)
    if not words:
        return LANGUAGE_DEFAULT

    scores = dict.fromkeys(SUPPORTED_LANGUAGES, 0)
    for language, markers in LANGUAGE_WORDS.items():
        scores[language] += sum(1 for word in words if word in markers)

    if re.search(r"[ąćęłńóśźż]", lowered):
        scores[LANGUAGE_POLISH] += 5
    if re.search(r"\b(czy|jest|nie|się|sie|proszę|prosze|dzięki|dzieki)\b", lowered):
        scores[LANGUAGE_POLISH] += 2
    if re.search(r"[ðþ]", lowered):
        scores[LANGUAGE_ICELANDIC] += 6
    if "æ" in lowered:
        scores[LANGUAGE_ICELANDIC] += 2
    if "å" in lowered:
        scores[LANGUAGE_SWEDISH] += 4
    if re.search(r"[äö]", lowered):
        scores[LANGUAGE_DEFAULT] += 1
        scores[LANGUAGE_SWEDISH] += 1
    if re.search(r"[ß]", lowered):
        scores[LANGUAGE_DEFAULT] += 4

    best_language, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        return LANGUAGE_DEFAULT

    # Bei Gleichstand oder sehr schwachem Signal nicht hektisch vom deutschen
    # Stream-Default wegkippen. Englisch/Schwedisch/Isländisch brauchen aber nur
    # ein knappes eindeutiges Signal, damit kurze Fragen funktionieren.
    tied = [language for language, score in scores.items() if score == best_score]
    if len(tied) > 1:
        if LANGUAGE_DEFAULT in tied:
            return LANGUAGE_DEFAULT
        return best_language
    return best_language


def language_reply_instruction(current_language: str, dominant_language: str | None = None) -> str:
    """Prompt-Baustein, der aktuelle Antwortsprache von Memory-Sprache trennt."""
    dominant = dominant_language or LANGUAGE_DEFAULT
    memory_hint = (
        f"Diese Person schreibt häufig in {dominant}; das ist nur Memory-Kontext und "
        "darf die aktuelle Antwortsprache NICHT überschreiben."
    )
    if current_language == LANGUAGE_ENGLISH:
        return (
            "Write the chat reply in natural English only. "
            "Do not answer in German unless the user switches back to German. "
            f"{memory_hint}"
        )
    if current_language == LANGUAGE_SWEDISH:
        return (
            "Svara den här personen på naturlig svenska. "
            "Byt inte till tyska eller engelska om det inte efterfrågas. "
            f"{memory_hint}"
        )
    if current_language == LANGUAGE_ICELANDIC:
        return (
            "Svaraðu þessari manneskju á eðlilegri íslensku. "
            "Ekki skipta yfir í þýsku eða ensku nema beðið sé um það. "
            f"{memory_hint}"
        )
    if current_language == LANGUAGE_POLISH:
        return (
            "Odpowiedz tej osobie naturalnie po polsku. "
            "Nie przechodź na niemiecki ani angielski, chyba że ktoś o to poprosi. "
            f"{memory_hint}"
        )
    return (
        "Antworte locker auf Deutsch, gern leicht bayrisch. "
        "Wechsle nicht ins Englische, außer die aktuelle Nachricht ist Englisch. "
        f"{memory_hint}"
    )


def language_final_label(language: str) -> str:
    if language == LANGUAGE_ENGLISH:
        return "Answer"
    if language in {LANGUAGE_SWEDISH, LANGUAGE_ICELANDIC}:
        return "Svar"
    if language == LANGUAGE_POLISH:
        return "Odpowiedź"
    return "Antwort"


def language_final_reminder(language: str) -> str:
    if language == LANGUAGE_ENGLISH:
        return "IMPORTANT: Reply only in English. Do not use German."
    if language == LANGUAGE_SWEDISH:
        return "VIKTIGT: Svara bara på svenska. Använd inte tyska."
    if language == LANGUAGE_ICELANDIC:
        return "MIKILVÆGT: Svaraðu aðeins á íslensku. Ekki nota þýsku."
    if language == LANGUAGE_POLISH:
        return "WAŻNE: Odpowiedz wyłącznie po polsku. Nie używaj niemieckiego."
    return "WICHTIG: Antworte auf Deutsch/Bayrisch. Wechsle nicht ungefragt die Sprache."


def clean_stream_title_parts(title: str) -> list[str]:
    """Zerlegt einen Twitch-Titel und behält nur inhaltlich brauchbare Teile.

    Twitch-Titel sind voll mit Deko, Sprach-Tags, Emotes und !Commands
    ("[BY/GER/EN] | ... | 🥦 420 🥦 | !panda !lurk"). Landet das roh im
    Prompt, kopieren es Modelle (vor allem Gemma online) gern wörtlich in
    den Chat zurück. Daher wird der Titel VOR jedem Prompt-Bau bereinigt.
    """
    parts: list[str] = []
    for raw in title.split("|"):
        part = raw.strip()
        if not part or part.startswith("[") or re.search(r"(?:^|\s)!\w+", part):
            continue
        part = re.sub(r"(?i)\bxd\b|\b420\b", "", part)
        part = re.sub(r"[^\w\s./+#-]", "", part).strip(" -")
        part = re.sub(r"\s+", " ", part)
        if len(part) < 4 or not re.search(r"[A-Za-zÄÖÜäöüß]", part):
            continue
        if part.lower() in {"unbekannt", "unknown"}:
            continue
        parts.append(part)
    return parts


def safe_stream_title(title: str) -> str:
    """Bereinigter Titel für Prompts; leer, wenn nichts Brauchbares übrig bleibt."""
    return " - ".join(clean_stream_title_parts(title)[:2])


# --------------------------------------------------------------------------- #
#  LLM-Client (OpenAI-kompatibel / Google native)
# --------------------------------------------------------------------------- #
def _extract_google_text(data: dict[str, Any]) -> str | None:
    """Extract final answer text from a Google ``generateContent`` response.

    Gemma 4 thinking models emit thought summaries as separate parts with
    ``"thought": true``.  Only non-thought text parts are concatenated.
    """
    try:
        candidates = data["candidates"]
    except (KeyError, TypeError):
        return None
    if not candidates:
        return None
    parts = []
    try:
        raw_parts = candidates[0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


class LLMClient:
    """Kapselt die Kommunikation mit dem ausgewählten LLM-Backend.

    Hält eine wiederverwendete aiohttp-Session offen (statt pro Anfrage eine
    neue aufzubauen) und kümmert sich um Timeouts, Stop-Strings und das
    Aufräumen typischer Halluzinationen kleiner Modelle.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        # Track the endpoint/timeout the current session was built for so that
        # a profile switch (different endpoint or timeout) forces a clean
        # re-open with the new parameters.
        self._session_url: str = ""
        self._session_timeout: float = 0.0
        # Control-file mtime for runtime profile switching (GUI mode).
        self._last_control_mtime_ns: int = 0
        # Stop-Strings verhindern, dass kleine Modelle sich selbst als weitere
        # Chatter halluzinieren und einen ganzen Fake-Dialog schreiben.
        self._stop = [
            f"{settings.bot_name}:",
            "User:",
            "Chat:",
            "<|im_start|>",
            "<|im_end|>",
        ]

    async def open(self) -> None:
        if (
            self._session is None
            or self._session.closed
            or self._session_url != settings.llm_url
            or self._session_timeout != settings.llm_timeout
        ):
            if self._session and not self._session.closed:
                await self._session.close()
            timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._session_url = settings.llm_url
            self._session_timeout = settings.llm_timeout

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def complete(
        self,
        system_prompt: str,
        turns: Sequence[tuple[str, str]],
        *,
        final_label: str = "Antwort",
        allow_prefill: bool = True,
    ) -> str | None:
        """Schickt einen Chat-Completion-Request und gibt die Antwort zurück.

        ``turns`` ist der Gesprächsverlauf als ``(rolle, inhalt)``-Paare mit den
        Rollen ``"user"`` und ``"assistant"``. Alle Meta-Anweisungen leben im
        ``system_prompt``; die Turns bleiben ein sauberer Chat. Gibt ``None``
        zurück, wenn der Server nicht erreichbar ist oder eine unbrauchbare
        Antwort liefert. Der Aufrufer entscheidet dann, ob er schweigt.
        """
        # --- Runtime profile switch (GUI control file) ---
        self._maybe_apply_control_file()

        messages = self._build_messages(
            system_prompt, list(turns), final_label=final_label, allow_prefill=allow_prefill
        )

        # CLI subscriptions do not need an aiohttp session.
        if settings.llm_transport in (
            "claude_cli",
            "codex_cli",
            "gemini_cli",
            "copilot_cli",
        ):
            return await self._complete_cli(settings.llm_transport, system_prompt, turns)

        await self.open()
        assert self._session is not None
        if settings.llm_transport == "google_native":
            return await self._complete_google_native(messages)
        return await self._complete_openai(messages)

    def _maybe_apply_control_file(self) -> None:
        """Check the GUI control file for a profile switch (no-op if absent).

        Only active when ``PANDABOT_GUI_CONTROL=1`` is set.  Reads the control
        file's mtime to avoid re-reading on every request when nothing changed.
        """
        if os.getenv("PANDABOT_GUI_CONTROL") != "1":
            return
        try:
            from llm_profiles import DEFAULT_CONTROL_PATH, read_control_file

            path = DEFAULT_CONTROL_PATH
            if not path.exists():
                return
            mtime_ns = path.stat().st_mtime_ns
            if mtime_ns <= self._last_control_mtime_ns:
                return
            self._last_control_mtime_ns = mtime_ns
            data = read_control_file(path)
            if not data:
                return
        except OSError:
            return

        self._apply_control_data(data)

    def _apply_control_data(self, data: dict[str, Any]) -> None:
        """Apply a control-file dict to the global ``settings``."""
        key_map = {
            "endpoint": "llm_url",
            "model": "llm_model",
            "api_key": "llm_api_key",
            "max_tokens": "llm_max_tokens",
            "temperature": "llm_temperature",
            "top_p": "llm_top_p",
            "timeout": "llm_timeout",
            "use_system_role": "llm_use_system_role",
            "send_repeat_penalty": "llm_send_repeat_penalty",
            "send_llama_extras": "llm_send_llama_extras",
            "repeat_penalty": "llm_repeat_penalty",
            "transport": "llm_transport",
        }
        for src, dst in key_map.items():
            if src in data and data[src] is not None:
                setattr(settings, dst, data[src])
        if data.get("profile_name"):
            settings.llm_backend_label = data["profile_name"]
            LOGGER.info("LLM-Profil gewechselt: %s", data["profile_name"])

    async def _complete_openai(self, messages: list[dict[str, str]]) -> str | None:
        """OpenAI-compatible chat/completions transport."""
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "top_p": settings.llm_top_p,
            "stop": self._stop,
            "stream": False,
        }

        if settings.llm_send_llama_extras:
            payload["chat_template_kwargs"] = {"enable_thinking": False, "thinking": False}
            payload["reasoning_budget"] = 0
        if settings.llm_send_repeat_penalty:
            payload["repeat_penalty"] = settings.llm_repeat_penalty

        headers = (
            {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else None
        )

        try:
            async with self._session.post(settings.llm_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.warning("LLM-Backend HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            LOGGER.warning("LLM-Backend nicht erreichbar: %s", exc)
            return None
        except Exception:  # noqa: BLE001 - defensiv, Bot soll nie crashen
            LOGGER.exception("Unerwarteter Fehler beim LLM-Aufruf")
            return None

        try:
            reply = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            LOGGER.warning("Unerwartetes Antwortformat: %r", data)
            return None

        sanitized = self._sanitize(reply)
        if not sanitized:
            LOGGER.warning("LLM-Rohantwort war leer/unbrauchbar: %r", reply)
            return None
        if self._looks_like_reasoning(sanitized):
            LOGGER.warning("LLM-Antwort sah nach Reasoning aus und wurde blockiert: %r", sanitized)
            return None
        return sanitized

    async def _complete_google_native(self, messages: list[dict[str, str]]) -> str | None:
        """Native Google Gemini ``generateContent`` transport.

        Used for Google/Gemma models.  The OpenAI-compatibility shim returns
        HTTP 500 for the MoE variant, so we call the native endpoint directly.
        Thought parts (``thought: true``) are filtered out.
        """
        model = settings.llm_model
        if model.startswith("models/"):
            model = model[len("models/") :]
        base = settings.llm_url.rstrip("/")
        url = f"{base}/models/{model}:generateContent"

        # Separate system messages → systemInstruction; user/assistant → contents.
        system_parts: list[dict[str, str]] = []
        contents: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append({"text": msg["content"]})
            else:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "(Sag etwas Kurzes.)"}]}]

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": settings.llm_max_tokens,
                "temperature": settings.llm_temperature,
                "topP": settings.llm_top_p,
            },
        }
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}

        headers = {
            "x-goog-api-key": settings.llm_api_key or "",
            "Content-Type": "application/json",
        }

        data: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                async with self._session.post(url, json=body, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        break
                    response_body = await resp.text()
                    transient = resp.status in {429, 500, 502, 503, 504}
                    if transient and attempt < 2:
                        LOGGER.warning(
                            "Google generateContent HTTP %s (Versuch %s/3), retry: %s",
                            resp.status,
                            attempt + 1,
                            response_body[:160],
                        )
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    LOGGER.warning(
                        "Google generateContent HTTP %s: %s", resp.status, response_body[:200]
                    )
                    return None
            except (aiohttp.ClientError, TimeoutError) as exc:
                LOGGER.warning("Google generateContent nicht erreichbar: %s", exc)
                return None
            except Exception:  # noqa: BLE001 - defensiv, Bot soll nie crashen
                LOGGER.exception("Unerwarteter Fehler beim Google generateContent-Aufruf")
                return None
        if data is None:
            return None

        reply = _extract_google_text(data)
        if reply is None:
            LOGGER.warning("Google generateContent: keine Text-Parts gefunden: %r", data)
            return None

        sanitized = self._sanitize(reply)
        if not sanitized:
            LOGGER.warning("Google generateContent-Rohantwort war leer/unbrauchbar: %r", reply)
            return None
        if self._looks_like_reasoning(sanitized):
            LOGGER.warning(
                "Google generateContent-Antwort sah nach Reasoning aus und wurde blockiert: %r",
                sanitized,
            )
            return None
        return sanitized

    async def _complete_cli(
        self,
        transport: str,
        system_prompt: str,
        turns: Sequence[tuple[str, str]],
    ) -> str | None:
        """CLI-backend transport (Claude/Codex/Gemini/Copilot CLIs).

        Packs system prompt + turns into a single text prompt, runs the
        official CLI headless/read-only, and sanitizes the response.
        Returns ``None`` if the CLI is missing or fails.
        """
        from cli_backends import cli_complete

        raw = await cli_complete(
            transport,
            settings.llm_model,
            system_prompt,
            turns,
            timeout=settings.llm_timeout,
        )
        if raw is None:
            LOGGER.warning("CLI-Backend (%s) lieferte keine Antwort.", transport)
            return None
        sanitized = self._sanitize(raw)
        if not sanitized:
            LOGGER.warning("CLI-Rohantwort war leer/unbrauchbar: %r", raw)
            return None
        if self._looks_like_reasoning(sanitized):
            LOGGER.warning("CLI-Antwort sah nach Reasoning aus und wurde blockiert: %r", sanitized)
            return None
        return sanitized

    def _build_messages(
        self,
        system_prompt: str,
        turns: list[tuple[str, str]],
        *,
        final_label: str,
        allow_prefill: bool,
    ) -> list[dict[str, str]]:
        """Baut die OpenAI-``messages`` aus System-Prompt und Gesprächsverlauf.

        Wichtige Eigenschaften für Modell-Agnostik:
        - Aufeinanderfolgende gleiche Rollen werden zusammengelegt; strikte
          Chat-Templates (z. B. Gemma) verlangen abwechselnde user/assistant-Turns.
        - Beginnt der Verlauf mit Bot-Nachrichten, wandern diese als Kontext in
          den System-Prompt, damit der erste Turn eine User-Nachricht ist.
        - Backends ohne System-Rolle (``LLM_USE_SYSTEM_ROLE=false``, z. B. Gemma
          über die Gemini API) bekommen die Anweisungen in die erste
          User-Nachricht eingebettet statt als System-Message.
        """
        placeholder = ("user", "(Sag etwas Kurzes, Passendes in den Chat.)")
        if not turns:
            turns = [placeholder]

        leading_bot: list[str] = []
        while turns and turns[0][0] == "assistant":
            leading_bot.append(turns.pop(0)[1])
        if leading_bot:
            system_prompt += "\n\nDeine eigenen letzten Chat-Nachrichten davor:\n" + "\n".join(
                f"- {message}" for message in leading_bot
            )
        if not turns:
            turns = [placeholder]

        messages: list[dict[str, str]] = []
        for role, content in turns:
            content = content.strip()
            if not content:
                continue
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
        if not messages:
            messages.append({"role": placeholder[0], "content": placeholder[1]})

        if settings.llm_use_system_role:
            messages.insert(0, {"role": "system", "content": system_prompt})
        elif messages[0]["role"] == "user":
            messages[0]["content"] = system_prompt + "\n\n----\n\n" + messages[0]["content"]
        else:
            messages.insert(0, {"role": "user", "content": system_prompt})

        # Assistant-Prefill nur für lokale llama.cpp-Thinking-Templates;
        # Online-Profile haben llm_send_llama_extras automatisch aus.
        if allow_prefill and settings.llm_send_llama_extras:
            messages.append({"role": "assistant", "content": f"<think></think>\n{final_label}:"})
        return messages

    def _sanitize(self, text: str | None) -> str | None:
        """Räumt typische Artefakte kleiner Modelle auf."""
        if not text:
            return None

        text = text.strip()
        thought_only_fallback = self._extract_thought_only_answer(text)

        # Thinking-Modelle (auch manche Online-Gemma/Gemini-Profile) liefern
        # interne Gedanken vor der eigentlichen Antwort. Geschlossene Blöcke
        # entfernen wir komplett; wenn die komplette Antwort fälschlich in
        # <think>/<thought> steckt, retten wir nur klar finale, nicht-meta Zeilen.
        text = re.sub(
            r"(?is)<\s*(?:think|thought)\b[^>]*>.*?<\s*/\s*(?:think|thought)\s*>",
            " ",
            text,
        ).strip()
        if not text and thought_only_fallback:
            text = thought_only_fallback
        if re.search(r"(?is)<\s*(?:think|thought)\b", text):
            extracted = self._after_final_marker(text)
            if extracted != text:
                text = extracted
            else:
                text = re.sub(r"(?is)<\s*(?:think|thought)\b[^>]*>.*", " ", text).strip()
        text = re.sub(
            r"(?is)<\s*/?\s*(?:think|thought|analysis|reasoning|reflection)\b[^>]*>",
            " ",
            text,
        ).strip()
        text = re.sub(r"\s+", " ", text).strip()

        # Wenn wir die Ausgabe mit "Antwort:" geprefillt haben, nur den finalen
        # Teil danach behalten.
        extracted = self._after_final_marker(text)
        if extracted != text:
            text = extracted

        # Manche Modelle stellen den eigenen Namen voran ("PandaBot: ...").
        for prefix in (
            f"{settings.bot_name}:",
            "PandaBot:",
            "Bot:",
            "Assistant:",
            "Antwort:",
            "Answer:",
            "Svar:",
            "Odpowiedź:",
            "Odpowiedz:",
        ):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :].strip()

        # Markdown-/Chat-Artefakte entfernen.
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text).strip()
        text = re.sub(r"(?i)^final(?:e)? antwort\s*:\s*", "", text).strip()

        # Umschließende Anführungszeichen entfernen.
        while len(text) >= 2 and text[0] in "\"'“”„" and text[-1] in "\"'“”„":
            text = text[1:-1].strip()

        if not text:
            return None

        # Twitch-Hardlimit sind 500 Zeichen; wir kürzen defensiv sauber.
        if len(text) > settings.max_message_length:
            text = text[: settings.max_message_length - 1].rstrip() + "…"

        return text

    def _after_final_marker(self, text: str) -> str:
        """Gibt den Text nach dem letzten expliziten Final-Antwort-Marker zurück."""
        markers = list(
            re.finditer(
                r"(?i)(?:^|\s)(?:finale?\s+)?(?:antwort|answer|final answer|svar|odpowiedź|odpowiedz)\s*:\s*",
                text,
            )
        )
        if not markers:
            return text
        return text[markers[-1].end() :].strip()

    def _extract_thought_only_answer(self, text: str) -> str | None:
        """Rettet finale Antworten, die komplett in einem Thought-Block gelandet sind."""
        block_re = re.compile(
            r"(?is)<\s*(?:think|thought)\b[^>]*>(.*?)<\s*/\s*(?:think|thought)\s*>",
        )
        matches = list(block_re.finditer(text))
        if not matches:
            return None

        outside = block_re.sub(" ", text).strip()
        if re.search(r"\w", outside):
            return None

        for match in reversed(matches):
            inner = match.group(1).strip()
            marked = self._after_final_marker(inner)
            if marked != inner and self._looks_like_complete_answer(marked):
                return marked

            for candidate in self._thought_answer_candidates(inner):
                if self._looks_like_reasoning(candidate):
                    continue
                if self._looks_like_complete_answer(candidate):
                    return candidate
        return None

    def _thought_answer_candidates(self, text: str) -> list[str]:
        """Liefert mögliche Final-Antworten aus Thought-Notizlisten, rückwärts priorisiert."""
        candidates: list[str] = []
        for raw_line in reversed(text.splitlines()):
            parts = re.split(r"\s+[*\-•]+\s+", raw_line)
            for raw_part in reversed(parts):
                candidate = re.sub(r"^\s*[*\-•]+\s*", "", raw_part).strip()
                candidate = re.sub(r"\s+", " ", candidate)
                if not candidate or self._looks_like_thought_metadata(candidate):
                    continue
                candidates.append(candidate)
        return candidates

    def _looks_like_thought_metadata(self, text: str) -> bool:
        lowered = text.lower().strip()
        prompt_label = (
            r"(?:(?:current|requested|required|target|reply|response|answer|output|final|aktuelle?r?)\s+)"
            r"?(?:language|query|prompt|request|message|question|input|antwortsprache|anfrage|frage)"
        )
        if re.match(
            rf"^(?:user|context|{prompt_label}|constraints?|task|aufgabe|system|chat|author|style|stil|stream[-\s]?kontext|aktueller\s+stream[-\s]?kontext|persona|tone|ton|voice|identity|rolle|role|instructions?|regeln?|rules?|output|format|antwortstil|response)\s*:",
            lowered,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:no markdown|kein markdown|keine analyse|keine gedanken|third person|dritte person|direct|direkt|constraints?)\b",
                lowered,
            )
        )

    def _looks_like_complete_answer(self, text: str) -> bool:
        candidate = text.strip()
        if len(candidate) < 8:
            return False
        if re.search(
            r"<\s*/?\s*(?:think|thought|analysis|reasoning|reflection)\b", candidate, re.I
        ):
            return False
        lowered = candidate.lower()
        if self._looks_like_thought_metadata(candidate):
            return False
        if re.search(r"(?:→|->|=>)", candidate):
            return True
        if re.search(r"[.!?…]$", candidate):
            return True
        return bool(
            re.search(r"\b(?:ist|sind|heißt|heisst|lautet|bedeutet|is|are|means)\b", lowered)
        )

    def _looks_like_reasoning(self, text: str) -> bool:
        """Verhindert, dass Chain-of-Thought/Meta-Analyse in Twitch landet."""
        lowered = text.lower().lstrip()
        if re.search(r"<\s*/?\s*(?:think|thought|analysis|reasoning|reflection)\b", lowered):
            return True
        if self._looks_like_thought_metadata(text):
            return True

        reasoning_starts = (
            "we need",
            "we should",
            "the user",
            "the prompt",
            "i need to",
            "i should",
            "need to respond",
            "analyse",
            "analysis",
            "let's break it down",
            "let us break it down",
            "der user",
            "der nutzer",
            "die nutzerin",
            "die frage",
            "ich muss",
            "ich sollte",
            "wir müssen",
            "wir sollten",
        )
        if lowered.startswith(reasoning_starts):
            return True

        # Manche Gemma-Antworten spiegeln nur Streamtitel/Commands statt die Frage zu beantworten.
        if self._looks_like_stream_title_or_command_leak(text):
            return True

        first_part = lowered[:260]
        if re.match(
            r"^(?:okay|ok|alright|also|hmm|hm)[,.:;\s]+(?:the user|der user|der nutzer|die nutzerin|die frage|ich muss|we need|i need)",
            first_part,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:user|prompt|system prompt|fragesteller|chain-of-thought)\b", first_part
            )
            and re.search(
                r"\b(?:ask|asks|fragt|möchte|moechte|will|need|needs|antworten|respond)\b",
                first_part,
            )
        )

    def _looks_like_stream_title_or_command_leak(self, text: str) -> bool:
        """Erkennt kopierte Streamtitel-/Command-Fragmente ohne echte Antwort."""
        cleaned = text.strip().strip("'\"“”„` ")
        lowered = cleaned.lower()
        if not cleaned:
            return False

        command_count = len(re.findall(r"(?:^|\s)![a-z0-9_]+\b", lowered))
        pipe_count = cleaned.count("|")
        if command_count >= 2:
            return True
        if pipe_count >= 2 and (command_count >= 1 or "420" in lowered or "🥦" in cleaned):
            return True
        if re.fullmatch(r"[\w\s./+#\-]*\|[\w\s./+#\-!🥦]+", cleaned) and command_count >= 1:
            return True
        return False


# --------------------------------------------------------------------------- #
#  Lokales User-Gedächtnis (Markdown pro Twitch-User)
# --------------------------------------------------------------------------- #
class UserMemoryStore:
    """Speichert kompakte, lokale Markdown-Notizen pro Twitch-User."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, user_id: str) -> Path:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(user_id))
        return self.root / f"{safe_id}.md"

    def _clean_display_name(self, display_name: str, user_id: str) -> str:
        cleaned = re.sub(r"[\r\n\t]+", " ", str(display_name)).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            cleaned = str(user_id)
        return cleaned[:80]

    def _initial_content(self, user_id: str, display_name: str) -> str:
        display_name = self._clean_display_name(display_name, user_id)
        return (
            f"# User-Gedächtnis: {display_name}\n\n"
            f"- Twitch-User-ID: {user_id}\n"
            f"- Anzeigename zuletzt gesehen: {display_name}\n"
            "- Interaktionen: 0\n\n"
            "## Notizen\n"
        )

    def load(self, user_id: str, display_name: str) -> str:
        path = self._path(user_id)
        if not path.exists():
            return "(keine gespeicherten Notizen)"
        try:
            return path.read_text(encoding="utf-8").strip() or "(keine gespeicherten Notizen)"
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis nicht lesen: %s", path)
            return "(Gedächtnis gerade nicht lesbar)"

    def interaction_count(self, memory_text: str) -> int:
        match = re.search(r"(?m)^- Interaktionen:\s*(\d+)\s*$", memory_text)
        return int(match.group(1)) if match else 0

    def record_interaction(self, user_id: str, display_name: str) -> int:
        """Zählt eine echte Interaktion hoch und gibt den neuen Zählerstand zurück."""
        display_name = self._clean_display_name(display_name, user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        try:
            current = (
                path.read_text(encoding="utf-8")
                if path.exists()
                else self._initial_content(user_id, display_name)
            )
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis nicht für Interaktion lesen: %s", path)
            return 0

        count = self.interaction_count(current) + 1
        if re.search(r"(?m)^- Interaktionen:\s*\d+\s*$", current):
            updated = re.sub(
                r"(?m)^- Interaktionen:\s*\d+\s*$",
                f"- Interaktionen: {count}",
                current,
                count=1,
            )
        else:
            anchor = f"- Twitch-User-ID: {user_id}\n"
            marker = f"- Anzeigename zuletzt gesehen: {display_name}\n"
            if marker in current:
                updated = current.replace(marker, f"{marker}- Interaktionen: {count}\n", 1)
            elif anchor in current:
                updated = current.replace(anchor, f"{anchor}- Interaktionen: {count}\n", 1)
            else:
                updated = current.replace(
                    "\n## Notizen\n", f"\n- Interaktionen: {count}\n\n## Notizen\n", 1
                )
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError:
            LOGGER.exception("Konnte Interaktionszähler nicht speichern: %s", path)
        return count

    def should_summarize(self, count: int) -> bool:
        """True, wenn bei diesem Zählerstand eine Profil-Consolidation fällig ist."""
        after = settings.profile_summary_after
        if after <= 0:
            return False
        if count == after:
            return True
        interval = settings.profile_summary_interval
        return interval > 0 and count > after and (count - after) % interval == 0

    def rewrite_notes(self, user_id: str, display_name: str, notes: str) -> None:
        """Ersetzt die '## Notizen'-Sektion komplett durch die konsolidierte Fassung.

        Im Gegensatz zu ``append`` (das nur ergänzt und deduppt) wird hier die
        ganze Sektion neu geschrieben - das hält reichhaltige Profile schlank und
        verhindert, dass sich veraltete/ähnliche Notizen über Monaten stapeln.
        """
        cleaned_notes = self._clean_consolidated_notes(notes)
        if not cleaned_notes:
            return
        display_name = self._clean_display_name(display_name, user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        try:
            current = (
                path.read_text(encoding="utf-8")
                if path.exists()
                else self._initial_content(user_id, display_name)
            )
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis für Consolidation nicht lesen: %s", path)
            return

        section = "## Notizen\n" + cleaned_notes + "\n"
        if re.search(r"(?ms)^## Notizen\n.*?(?=\n## |\Z)", current):
            updated = re.sub(
                r"(?ms)^## Notizen\n.*?(?=\n## |\Z)",
                section.rstrip(),
                current,
                count=1,
            )
        else:
            updated = current.rstrip() + "\n\n" + section
        try:
            path.write_text(updated, encoding="utf-8")
            LOGGER.info("User-Profil konsolidiert: %s", path)
        except OSError:
            LOGGER.exception("Konnte konsolidiertes Profil nicht speichern: %s", path)

    def _clean_consolidated_notes(self, notes: str) -> str:
        """Bereinigt die LLM-Consolidation-Ausgabe zu sauberen Markdown-Bullets."""
        lines: list[str] = []
        for raw in notes.splitlines():
            line = re.sub(r"\s+", " ", raw.strip())
            if not line:
                continue
            lowered = line.lower().strip("- *•")
            if lowered in {"keine", "keine.", "keine neuen notizen.", "none", "n/a"}:
                continue
            line = re.sub(r"^(?:[-*•]\s*)?", "- ", line)
            if len(line) > 260:
                line = line[:259].rstrip() + "…"
            lines.append(line)
        # Doppelte / fast identische Notizen entfernen.
        deduped: list[str] = []
        seen: list[str] = []
        for line in lines[: settings.profile_max_notes]:
            norm = re.sub(r"[^\w]+", " ", line.lower()).strip()
            if not norm:
                continue
            if any(
                norm == prev or SequenceMatcher(None, norm, prev).ratio() >= 0.9 for prev in seen
            ):
                continue
            seen.append(norm)
            deduped.append(line)
        return "\n".join(deduped)

    def touch(self, user_id: str, display_name: str) -> None:
        """Legt ein Gedächtnis für gesehene Chatter an und aktualisiert den Namen."""
        display_name = self._clean_display_name(display_name, user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        try:
            current = (
                path.read_text(encoding="utf-8")
                if path.exists()
                else self._initial_content(user_id, display_name)
            )
            updated = re.sub(
                r"(?m)^# User-Gedächtnis: .+$",
                f"# User-Gedächtnis: {display_name}",
                current,
                count=1,
            )
            updated = re.sub(
                r"(?m)^- Anzeigename zuletzt gesehen: .+$",
                f"- Anzeigename zuletzt gesehen: {display_name}",
                updated,
                count=1,
            )
            if updated == current and "- Anzeigename zuletzt gesehen:" not in current:
                updated = current.replace(
                    f"- Twitch-User-ID: {user_id}\n",
                    f"- Twitch-User-ID: {user_id}\n- Anzeigename zuletzt gesehen: {display_name}\n",
                    1,
                )
            if updated != current or not path.exists():
                path.write_text(updated, encoding="utf-8")
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis nicht anlegen/aktualisieren: %s", path)

    def language_counts(self, memory_text: str) -> dict[str, int]:
        match = re.search(r"(?m)^- Zählung: (?P<counts>.+)$", memory_text)
        if not match:
            return {}
        counts: dict[str, int] = {}
        for part in match.group("counts").split(","):
            if "=" not in part:
                continue
            language, value = part.rsplit("=", 1)
            try:
                count = int(value.strip())
            except ValueError:
                continue
            if count > 0:
                counts[language.strip()] = count
        return counts

    def dominant_language(self, memory_text: str) -> str | None:
        counts = self.language_counts(memory_text)
        if not counts:
            return None
        return max(counts.items(), key=lambda item: item[1])[0]

    def update_language_profile(self, user_id: str, display_name: str, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES:
            return

        display_name = self._clean_display_name(display_name, user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        try:
            current = (
                path.read_text(encoding="utf-8")
                if path.exists()
                else self._initial_content(user_id, display_name)
            )
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis nicht für Sprachprofil lesen: %s", path)
            return

        counts = self.language_counts(current)
        counts[language] = counts.get(language, 0) + 1
        dominant = max(counts.items(), key=lambda item: item[1])[0]
        ordered_counts = ", ".join(
            f"{name}={counts[name]}" for name in SUPPORTED_LANGUAGES if counts.get(name, 0) > 0
        )
        profile = (
            "## Sprachprofil (automatisch)\n"
            f"- Häufigste Sprache: {dominant}\n"
            f"- Zählung: {ordered_counts}\n"
            "- Hinweis: Antworten richten sich nach der Sprache der aktuellen Nachricht; diese Statistik ist nur Memory-Kontext.\n"
        )

        if re.search(r"(?ms)^## Sprachprofil \(automatisch\)\n.*?(?=\n## |\Z)", current):
            current = re.sub(
                r"(?ms)^## Sprachprofil \(automatisch\)\n.*?(?=\n## |\Z)",
                profile.rstrip(),
                current,
                count=1,
            )
        elif "\n## Notizen\n" in current:
            current = current.replace("\n## Notizen\n", f"\n{profile}\n## Notizen\n", 1)
        else:
            if not current.endswith("\n"):
                current += "\n"
            current += f"\n{profile}"

        try:
            path.write_text(current, encoding="utf-8")
        except OSError:
            LOGGER.exception("Konnte User-Sprachprofil nicht speichern: %s", path)

    def append(self, user_id: str, display_name: str, notes: str) -> None:
        cleaned = self._clean_notes(notes)
        if not cleaned:
            return

        display_name = self._clean_display_name(display_name, user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        if path.exists():
            current = path.read_text(encoding="utf-8")
        else:
            current = self._initial_content(user_id, display_name)

        current_lower = current.lower()
        new_lines = [line for line in cleaned.splitlines() if line.lower() not in current_lower]
        if not new_lines:
            return

        if not current.endswith("\n"):
            current += "\n"
        current += "\n".join(new_lines) + "\n"
        path.write_text(current, encoding="utf-8")
        LOGGER.info("User-Gedächtnis aktualisiert: %s", path)

    def _clean_notes(self, notes: str) -> str:
        lines: list[str] = []
        for raw in notes.splitlines():
            line = raw.strip()
            if not line or line.lower() in {"keine", "keine."}:
                continue
            line = re.sub(r"^(?:[-*]\s*)?", "- ", line)
            if len(line) > 240:
                line = line[:239].rstrip() + "…"
            lines.append(line)
        return "\n".join(lines[:3])


# --------------------------------------------------------------------------- #
#  Kontext-Cache für Stream-Infos (Titel/Spiel) via Helix
# --------------------------------------------------------------------------- #
class StreamContext:
    """Holt Titel und Spiel live von Twitch und cached sie kurz.

    Ein API-Call pro Nachricht wäre verschwenderisch und würde Rate-Limits
    belasten, daher cachen wir die Infos für ``ttl`` Sekunden.
    """

    def __init__(self, ttl: int = 120) -> None:
        self._ttl = ttl
        self._fetched_at = 0.0
        self.title = "Unbekannt"
        self.game = "Unbekannt"
        self.is_live = False
        self.viewers = 0

    async def refresh(self, client: twitchio.Client, broadcaster: twitchio.PartialUser) -> None:
        now = time.monotonic()
        if now - self._fetched_at < self._ttl:
            return

        try:
            info = await broadcaster.fetch_channel_info()
            self.title = info.title or "Unbekannt"
            self.game = info.game_name or "Unbekannt"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Konnte Channel-Info nicht laden")

        # Stream-Objekt nur für Live-Status / Zuschauerzahl (optional).
        try:
            streams = await client.fetch_streams(user_ids=[broadcaster.id])
            if streams:
                self.is_live = True
                self.viewers = streams[0].viewer_count
            else:
                self.is_live = False
                self.viewers = 0
        except Exception:  # noqa: BLE001
            LOGGER.debug("Stream-Status nicht abrufbar (evtl. offline)")

        self._fetched_at = now
        LOGGER.info(
            "Kontext aktualisiert | Spiel: %s | Titel: %s | live: %s",
            self.game,
            self.title,
            self.is_live,
        )


# --------------------------------------------------------------------------- #
#  Prompt-Bau
# --------------------------------------------------------------------------- #
def build_system_prompt(ctx: StreamContext) -> str:
    """Baut den Basis-System-Prompt mit Live-Kontext.

    Kurz und konkret gehalten - kleine Modelle folgen knappen, klaren
    Anweisungen deutlich zuverlässiger als langen Persona-Texten. Der
    Stream-Titel wird vorab von Tags/Commands/Deko befreit, damit Modelle
    (vor allem Gemma online) ihn nicht wörtlich in den Chat kopieren.
    """
    title = safe_stream_title(ctx.title)
    if title:
        stream_line = (
            f"Der Stream läuft gerade in der Kategorie '{ctx.game}', Thema laut Titel: {title}. "
        )
    else:
        stream_line = f"Der Stream läuft gerade in der Kategorie '{ctx.game}'. "
    return (
        f"Du bist {settings.bot_name}, ein freundlicher, witziger Chatbot im "
        f"Twitch-Stream von {settings.channel_name}. "
        f"{stream_line}"
        "Die Chat-Nachrichten im Verlauf haben das Format 'Name: Text'; deine eigene Antwort schreibst du OHNE Namens-Präfix. "
        "Schreib natürlich, direkt und conversational, meist 1-3 kurze Sätze; bei Geschichten, Witzen oder Erklärungen darf es etwas länger sein. "
        "Sei interaktiv: Stelle bei offenen Themen gern eine kurze, konkrete Anschlussfrage, aber dränge den Chat nicht. "
        "Beantworte genau die aktuelle Bitte: Wenn jemand einen Witz oder eine Geschichte will, erzähl sie; wenn jemand eine Erklärung will, erklär es. "
        "Bei frechen oder bösen Witzen darfst du trockenen, leicht schwarzen Humor nutzen. "
        "Wenn nach dem heutigen Stream oder Thema gefragt wird, nutze den Stream-Kontext oben. "
        "Sprich die Person, die dich gerade anspricht, direkt mit 'du' an; rede nicht in der dritten Person über sie. "
        "Klinge wie ein aufmerksamer Chat-Kumpel: konkret, warm, gerne etwas frech, aber nicht generisch oder anbiedernd. "
        "Wenn du etwas nicht sicher weißt, sag es kurz ehrlich statt zu halluzinieren. "
        "LURKER-REGEL (sehr wichtig): Sprich niemals Lurker an oder aus. Kein 'ihr Lurker da draußen', kein 'ich sehe euch zuschauen', "
        "kein Erwähnen von Zuschauerzahlen, kein Outen oder Beaufwaltigen von Leuten, die nur mitlesen. Behandle Stille einfach als Chance, "
        "ein neues Topic oder eine Frage in den Raum zu stellen - ohne jemanden beim Lurken zu erwischen. "
        "Kein steifer Assistententon, keine Meta-Sätze, keine Analyse, keine <think>- oder <thought>-Blöcke, keine Labels oder Feldnamen, kein Wiederholen der Frage, kein Markdown/Fettdruck. "
        "Zitiere den Streamtitel nie wörtlich und kopiere keine Chat-Commands (!...), Emotes oder Deko. "
        "Emojis sparsam verwenden. Sei locker und unterhaltsam, aber nie beleidigend. "
        "Gib immer nur deine finale Chat-Nachricht aus."
    )


# --------------------------------------------------------------------------- #
#  Bot
# --------------------------------------------------------------------------- #
class PandaBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            bot_id=settings.bot_id,
            owner_id=settings.owner_id,
            prefix="!",
        )
        self.llm = LLMClient()
        self.context = StreamContext(ttl=settings.context_ttl)
        self.user_memory = UserMemoryStore(settings.user_memory_dir)

        # Rollierender Gesprächsverlauf als (rolle, inhalt)-Turns: "user" für
        # echte Chatter (Format "Name: Text"), "assistant" für eigene Antworten.
        # So bekommt das LLM einen echten Mehr-Turn-Chat statt eines Text-Blobs,
        # den es als Formular zurückspiegeln könnte (deque begrenzt die Länge).
        self.chat_history: deque[tuple[str, str]] = deque(maxlen=settings.history_length)
        self._broadcaster: twitchio.PartialUser | None = None

        # Echter Twitch-Login-Name des Bot-Accounts. Wird in event_ready aus
        # der bot_id aufgelöst, damit Erwähnungen am tatsächlichen Account-Namen
        # hängen und nicht am kosmetischen bot_name (Spitzname).
        self._bot_login: str | None = None

        self._last_activity = time.monotonic()
        self._idle_messages_since_human = 0
        self._recent_bot_messages: deque[str] = deque(maxlen=8)
        # Letzte verwendeten Gesprächsaufhänger (Opener), damit sich Idle-Starts
        # nicht ähneln - neben dem reinen Textvergleich ein zusätzlicher Schutz.
        self._recent_openers: deque[str] = deque(maxlen=8)
        self._chat_subscription_active = False
        # Lock statt bool-Flag: verhindert sauber parallele LLM-Aufrufe.
        self._llm_lock = asyncio.Lock()

        # Per-User-Puffer der letzten (User-Nachricht, Bot-Antwort)-Paare für
        # die reichhaltige Profil-Consolidation. None = keine Bot-Antwort.
        self._user_interactions: dict[str, deque[tuple[str, str | None]]] = {}
        # Verhindert, dass für denselben User parallel consolidiert wird.
        self._summarizing: set[str] = set()
        # Ereignisgesteuerter Idle-Task (statt starrer 60s-Routine).
        self._idle_task: asyncio.Task[None] | None = None

    # ----- Setup & EventSub -------------------------------------------------- #
    async def setup_hook(self) -> None:
        """Wird nach dem Login, aber vor dem Start aufgerufen.

        Hier abonnieren wir die Chat-Nachrichten des Zielkanals via EventSub
        (WebSocket). Das ersetzt das alte IRC-``initial_channels``.
        """
        self._broadcaster = self.create_partialuser(settings.owner_id)

        if not self._has_required_user_tokens():
            self._log_oauth_instructions()
            return

        await self._subscribe_chat()

    def _has_required_user_tokens(self) -> bool:
        tokens = self._http._tokens  # TwitchIO ManagedHTTPClient; enthält User-Tokens nach OAuth.
        return settings.bot_id in tokens and settings.owner_id in tokens

    def _log_oauth_instructions(self) -> None:
        bot_scopes = quote(BOT_SCOPES)
        owner_scopes = quote(OWNER_SCOPES)
        LOGGER.warning("Noch nicht autorisiert: Bot- und/oder Kanal-Token fehlen.")
        LOGGER.warning("Lass dieses Fenster offen und autorisiere jetzt beide Accounts:")
        LOGGER.warning(
            "1) Inkognito als BOT-Account öffnen: http://localhost:4343/oauth?scopes=%s&force_verify=true",
            bot_scopes,
        )
        LOGGER.warning(
            "2) Normal als STREAMER/Kanal öffnen: http://localhost:4343/oauth?scopes=%s&force_verify=true",
            owner_scopes,
        )
        LOGGER.warning(
            "Danach PandaBot mit Strg+C beenden und erneut mit 'python pandabot.py' starten."
        )

    async def _subscribe_chat(self) -> None:
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=settings.owner_id,
            user_id=settings.bot_id,
        )
        await self.subscribe_websocket(payload=subscription)
        self._chat_subscription_active = True
        LOGGER.info("Chat-Subscription für Kanal %s aktiv", settings.channel_name)

    async def event_oauth_authorized(
        self, payload: twitchio.authentication.UserTokenPayload
    ) -> None:
        await super().event_oauth_authorized(payload)
        await self.save_tokens()
        LOGGER.info(
            "OAuth erfolgreich für %s (%s). Token wurde gespeichert.",
            payload.user_login or "?",
            payload.user_id or "?",
        )

    async def event_ready(self) -> None:
        await self.llm.open()
        # Echten Login-Namen des Bot-Accounts merken (z. B. "dawastehbot").
        # Daran erkennen wir später @-Erwähnungen zuverlässig.
        if self.user is not None:
            self._bot_login = self.user.name
        # Kontext einmal initial laden, damit der erste Prompt schon stimmt.
        if self._broadcaster:
            await self.context.refresh(self, self._broadcaster)
        if self._chat_subscription_active:
            self._start_idle_loop()
        LOGGER.info(
            "PandaBot (%s, Account: %s) ist online und mit %s verbunden!",
            settings.bot_name,
            self._bot_login or "?",
            settings.channel_name,
        )

    async def close(self, **options: object) -> None:
        # Sauberes Herunterfahren: adaptiven Idle-Task stoppen, Session schließen.
        await self._stop_idle_loop()
        await self.llm.close()
        await super().close(**options)

    # ----- Chat-Handling ----------------------------------------------------- #
    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        # Eigene Nachrichten robust ignorieren: nie auf den Bot selbst reagieren
        # und eigene Zeilen nicht als Chat-Kontext füttern.
        if self._is_own_message(payload):
            self._remember_bot_message(payload.text)
            return

        # display_name und name sind laut Typ optional; mit der (immer
        # vorhandenen) ID als Fallback ist author garantiert ein str.
        author = payload.chatter.display_name or payload.chatter.name or str(payload.chatter.id)
        text = payload.text.strip()
        if not text:
            return

        self._last_activity = time.monotonic()
        self._idle_messages_since_human = 0
        if settings.user_memory_enabled:
            self.user_memory.touch(payload.chatter.id, author)
            self.user_memory.record_interaction(payload.chatter.id, author)
        self.chat_history.append(("user", f"{author}: {text}"))
        LOGGER.info("Chat empfangen von %s (%s): %s", author, payload.chatter.id, text)

        # Direkter Befehl ohne TwitchIO-Command-Registry, damit es auch aus der
        # Bot-Subclass zuverlässig funktioniert.
        if text.lower().startswith("!panda"):
            frage = text[len("!panda") :].strip()
            await self._respond(payload, author=author, trigger=frage or text)
            return

        # Andere !-Befehle ignorieren.
        if text.startswith("!"):
            return

        if self._is_mention(text):
            LOGGER.info("Erwähnung erkannt von %s", author)
            await self._respond(payload, author=author, trigger=text)

    def _bot_trigger_names(self) -> set[str]:
        """Namen, unter denen der Bot angesprochen/erkannt wird."""
        names = {settings.bot_name.strip().lower()}
        if self._bot_login:
            names.add(self._bot_login.strip().lower())
        return {name for name in names if name}

    def _is_own_message(self, payload: twitchio.ChatMessage) -> bool:
        """Erkennt Bot-Echos auch dann, wenn TwitchIO die ID nicht sauber setzt."""
        chatter = payload.chatter
        chatter_id = str(getattr(chatter, "id", "") or "").strip()
        if chatter_id and chatter_id == str(settings.bot_id):
            return True

        bot_names = self._bot_trigger_names()
        for attr in ("name", "display_name"):
            value = str(getattr(chatter, attr, "") or "").strip().lower()
            if value and value in bot_names:
                return True
        return False

    def _is_mention(self, text: str) -> bool:
        lowered = text.lower()

        for name in self._bot_trigger_names():
            if not name:
                continue
            # @name trifft immer (eindeutige Erwähnung).
            if f"@{name}" in lowered:
                return True
            # Name ohne @ nur als eigenständiges Wort, damit nicht zufällige
            # Substrings (z. B. in anderen Wörtern) fälschlich triggern.
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
                return True
        return False

    def _is_chat_context_question(self, text: str) -> bool:
        lowered = text.lower()
        keywords = (
            "chat",
            "stimmung",
            "verlauf",
            "zusammenfass",
            "was war los",
            "was ging",
            "was macht ihr",
            "was machen die leute",
            "was sagt",
            "wer hat",
            "wer schreibt",
            "wer ist da",
            "letzte nachrichten",
        )
        return any(keyword in lowered for keyword in keywords)

    def _is_stream_context_question(self, text: str) -> bool:
        lowered = self._strip_trigger_prefix(text.lower())
        # Nicht auf jede Erwähnung von "Stream/Thema" routen: "erzähl einen Witz,
        # der nichts mit dem Streamthema zu tun hat" ist eine Witz-Anfrage, keine
        # Frage nach dem Stream-Inhalt.
        if re.search(r"\b(witz|geschichte|story|erzähl|erzaehl|witzig|joke)\b", lowered):
            return False
        if re.search(
            r"\b(testen|ideen|vorschläge|vorschlaege|was können wir|was koennen wir)\b", lowered
        ):
            return False
        if "nicht" in lowered or "nichts mit" in lowered:
            return False

        patterns = (
            r"^\s*um\s+was\s+geht(?:'s|s|\s+es)?(?:\s+(?:heute|gerade|im\s+stream))?\s*\??\s*$",
            r"^\s*was\s+(?:passiert|geht|gehts|geht's|läuft|laeuft)\s+(?:heute|heut|gerade|so)(?:\s+so)?\s*\??\s*$",
            r"^\s*was\s+(?:machen\s+wir|steht)\s+(?:heute|heut|gerade)(?:\s+so|\s+an)?\s*\??\s*$",
            r"\bum\s+was\s+geht(?:'s|s|\s+es)?.*\bstream\b",
            r"\bwas\s+geht(?:'s|s|\s+es)?.*\bstream\b",
            r"\b(wovon|von\s+was)\s+handelt.*\bstream\b",
            r"\bwas\s+ist.*\b(thema|streamthema)\b",
            r"\bwas\s+läuft.*\b(heute|stream)\b",
            r"\bwas\s+wird.*\bgestreamt\b",
            r"\bwelches\s+thema\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _strip_trigger_prefix(self, text: str) -> str:
        """Entfernt führende Bot-Kommandos/-Mentions für Intent-Erkennung."""
        cleaned = re.sub(r"^\s*!panda\b[:,]?\s*", "", text, flags=re.I)
        for name in sorted(self._bot_trigger_names(), key=len, reverse=True):
            cleaned = re.sub(rf"^\s*@?{re.escape(name)}\b[:,]?\s*", "", cleaned, flags=re.I)
        return cleaned.strip()

    def _has_known_stream_context(self) -> bool:
        unknown = {"", "unbekannt", "unknown", "none", "null"}
        return (
            self.context.game.strip().lower() not in unknown
            or self.context.title.strip().lower() not in unknown
        )

    def _stream_context_answer(self, language: str = LANGUAGE_DEFAULT) -> str:
        title_parts = clean_stream_title_parts(self.context.title)

        if title_parts:
            detail = ", ".join(title_parts[:2])
            if language == LANGUAGE_ENGLISH:
                return f"Roughly, today’s stream is about {self.context.game}, apparently with a focus on {detail}."
            if language == LANGUAGE_SWEDISH:
                return f"I stora drag handlar streamen om {self.context.game}, tydligen med fokus på {detail}."
            if language == LANGUAGE_ICELANDIC:
                return f"Í grófum dráttum fjallar streymið um {self.context.game}, greinilega með áherslu á {detail}."
            if language == LANGUAGE_POLISH:
                return f"Ogólnie stream jest o {self.context.game}, najwyraźniej z fokusem na {detail}."
            return f"Grob geht’s um {self.context.game}; heute offenbar mit Fokus auf {detail}."
        if language == LANGUAGE_ENGLISH:
            return f"Roughly, today’s stream is about {self.context.game}; the exact focus is unfolding live."
        if language == LANGUAGE_SWEDISH:
            return f"I stora drag handlar streamen idag om {self.context.game}; exakt fokus märks nog under streamen."
        if language == LANGUAGE_ICELANDIC:
            return f"Í grófum dráttum fjallar streymið í dag um {self.context.game}; nákvæmi fókusinn kemur í ljós í beinni."
        if language == LANGUAGE_POLISH:
            return f"Ogólnie dzisiejszy stream jest o {self.context.game}; dokładny fokus wyjdzie w trakcie live’a."
        return f"Grob geht’s heute um {self.context.game}; der genaue Fokus ergibt sich gerade im Stream."

    def _fallback_reply(self, trigger: str, language: str = LANGUAGE_DEFAULT) -> str | None:
        lowered = trigger.lower()
        if self._is_stream_context_question(trigger) and self._has_known_stream_context():
            return self._stream_context_answer(language)
        if "photosynthese" in lowered and re.search(
            r"\b(?:formel|gleichung|reaktion|formula|equation)\b", lowered
        ):
            return "6 CO₂ + 6 H₂O + Lichtenergie → C₆H₁₂O₆ + 6 O₂."
        if re.search(r"decarbox[yi]l", lowered) and re.search(
            r"\b(?:formel|gleichung|reaktion|formula|equation)\b", lowered
        ):
            return "Allgemein: R-COOH → R-H + CO₂. Kurz: Carboxylgruppe ab, CO₂ raus."
        if re.search(r"keto[-\s]?enol", lowered) and re.search(
            r"\b(?:formel|gleichung|reaktion|formula|equation)\b", lowered
        ):
            return "Keto-Enol-Tautomerie: R-CO-CH₂-R′ ⇌ R-C(OH)=CH-R′. Das ist Ketoform ↔ Enolform."
        if re.search(r"\b(?:mtp|mcp)\b", lowered) and re.search(r"\bllm", lowered):
            return "Meinst du MCP? Das ist wie ein USB-C-Port für LLMs: Tools, Dateien oder APIs werden standardisiert ans Modell angedockt."
        if "comfyui" in lowered and re.search(r"\bvideo", lowered):
            return "In ComfyUI am besten mit AnimateDiff oder WAN/I2V starten: kurze Clips, feste Seed/Resolution, dann upscalen/interpolieren. Erst Workflow stabil kriegen, dann Qualität hochdrehen."
        opinion_topic = self._extract_opinion_topic(trigger)
        if opinion_topic:
            opinion_lower = opinion_topic.lower()
            if "anthropic" in opinion_lower and re.search(r"\bfable\s*5\b", opinion_lower):
                return "Fable 5 klingt spannend, vor allem wenn Anthropic bei Coding und Agenten nochmal nachlegt. Aber ich würd’s erst nach echten Benchmarks hypen – Marketing kann jeder."
            return f"Zu {opinion_topic}: Ich wär erstmal pragmatisch – Hype ist nett, aber echte Benchmarks und Alltagstests zählen mehr. Wenn’s stabil, schnell und bezahlbar hilft, bin ich dabei."
        if "was geht" in lowered or "what's up" in lowered or "whats up" in lowered:
            if language == LANGUAGE_ENGLISH:
                return "All good, I’m hanging out in chat waiting for chaos. What’s up with you?"
            return "Alles entspannt, ich häng im Chat rum und warte auf Chaos. Was geht bei dir?"
        if "was stimmt nicht" in lowered or "what is wrong" in lowered or "what's wrong" in lowered:
            if language == LANGUAGE_ENGLISH:
                return "Probably too much model caffeine and not enough polish. But hey, we’re debugging me live."
            return "Vermutlich zu viel Modell-Koffein und zu wenig Feinschliff. Aber hey, wir debuggen mich ja gerade live."
        if (
            "google" in lowered
            or "websuche" in lowered
            or "suche" in lowered
            or "search" in lowered
        ):
            if language == LANGUAGE_ENGLISH:
                return "I can’t live-google from here, but I can still give you a quick take from what I know."
            if language == LANGUAGE_SWEDISH:
                return "Jag kan inte googla live härifrån, men jag kan ge dig en snabb bedömning utifrån det jag vet."
            if language == LANGUAGE_ICELANDIC:
                return "Ég get ekki gúglað í beinni héðan, en ég get samt gefið þér stutta útskýringu út frá því sem ég veit."
            if language == LANGUAGE_POLISH:
                return "Nie mogę tutaj googlować na żywo, ale mogę krótko wyjaśnić temat z tego, co wiem."
            return "Live googeln kann ich hier nicht direkt, aber ich kann dir aus meinem Wissen kurz einordnen, worum es geht."
        return None

    def _extract_opinion_topic(self, trigger: str) -> str | None:
        """Extrahiert einfache „was ist deine Meinung zu X“-Fragen für robuste Fallbacks."""
        cleaned = self._strip_trigger_prefix(trigger)
        patterns = (
            r"(?i)^\s*was\s+(?:is|ist)\s+(?:deine|dein)\s+meinung\s+zu\s+(.+?)\s*[?.!]*\s*$",
            r"(?i)^\s*(?:deine|dein)\s+meinung\s+zu\s+(.+?)\s*[?.!]*\s*$",
            r"(?i)^\s*wie\s+findest\s+du\s+(.+?)\s*[?.!]*\s*$",
        )
        for pattern in patterns:
            match = re.match(pattern, cleaned)
            if match:
                topic = re.sub(r"\s+", " ", match.group(1)).strip(" '\"“”„`.,!?;:-")
                if topic:
                    return topic[:80]
        return None

    def _polish_reply(
        self,
        reply: str,
        *,
        trigger: str,
        author: str,
        language: str = LANGUAGE_DEFAULT,
    ) -> str:
        text = reply.strip()
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text).strip()

        # Wenn das Modell die Frage als ersten Satz wiederholt, weg damit.
        first_sentence = re.match(r"^(.{1,180}?[.!?])\s+(.*)$", text)
        if first_sentence:
            first = re.sub(r"[^\w]+", " ", first_sentence.group(1)).strip().lower()
            trig = re.sub(r"[^\w]+", " ", trigger).strip().lower()
            if trig and (first.startswith(trig) or trig.startswith(first.rstrip(" ?!"))):
                text = first_sentence.group(2).strip()

        # Typische Meta-Sätze kleiner Modelle entfernen.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        cleaned: list[str] = []
        meta_patterns = (
            r"\bfragesteller\b",
            rf"\b{re.escape(author.lower())}\b.*\b(bereit|interessiert|freut|möchte wissen|will wissen)\b",
            r"\blasst uns sehen\b",
            r"\bdas ist eine lustige anfrage\b",
            r"\bich antworte\b",
        )
        for sentence in sentences:
            lowered_sentence = sentence.lower()
            if any(re.search(pattern, lowered_sentence) for pattern in meta_patterns):
                continue
            cleaned.append(sentence)
        text = " ".join(cleaned).strip()
        if not re.search(r"[\w]", text) or len(text) < 8:
            return self._fallback_reply(trigger, language) or reply.strip()

        return text

    def _history_turns(
        self, *, limit: int, current: tuple[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """Letzte Verlaufs-Turns für den LLM-Kontext.

        ``current`` ist der Turn der gerade beantworteten Nachricht; er wird
        aus dem Verlauf herausgenommen, weil der Aufrufer ihn selbst als
        finalen User-Turn anhängt.
        """
        items = list(self.chat_history)
        if current is not None and items and items[-1] == current:
            items = items[:-1]
        if limit <= 0:
            return []
        return items[-limit:]

    def _remember_bot_message(self, text: str | None) -> None:
        """Merkt sich eigene Antworten für Verlauf und Wiederholungs-Schutz.

        Wird sowohl beim Senden als auch beim EventSub-Echo der eigenen
        Nachricht aufgerufen; das Echo wird per Vergleich mit den letzten
        eigenen Nachrichten dedupliziert, damit nichts doppelt im Verlauf landet.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        normalized = self._normalized_message(cleaned)
        recent = list(self._recent_bot_messages)[-4:]
        if any(self._normalized_message(previous) == normalized for previous in recent):
            return
        self._recent_bot_messages.append(cleaned)
        self.chat_history.append(("assistant", cleaned))

    def _normalized_message(self, text: str) -> str:
        return re.sub(r"[^\w]+", " ", text.lower()).strip()

    def _is_recent_bot_repeat(self, text: str) -> bool:
        normalized = self._normalized_message(text)
        if not normalized:
            return False
        for previous in self._recent_bot_messages:
            prev = self._normalized_message(previous)
            if not prev:
                continue
            if normalized == prev:
                return True
            if len(normalized) >= 18 and SequenceMatcher(None, normalized, prev).ratio() >= 0.88:
                return True
        return False

    def _has_memory_signal(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bmerk(?:\s+dir)?\b",
            r"\berinner\b",
            r"\bremember\b",
            r"\bpamiętaj\b",
            r"\bkom\s+ihåg\b",
            r"\bmundu\b",
            r"\bich\s+(mag|liebe|hasse|bevorzuge|will|möchte|moechte|bin|heiße|heisse)\b",
            r"\bi\s+(like|love|hate|prefer|want|am)\b",
            r"\bja\s+(lubię|lubie|kocham|nienawidzę|nienawidze|wolę|wole|jestem)\b",
            r"\bmein(?:e|er|en|em)?\s+\w+\s+ist\b",
            r"\bmy\s+\w+\s+is\b",
            r"\bnenn\s+mich\b",
            r"\bcall\s+me\b",
            r"\bbitte\s+(immer|nie|nicht)\b",
            r"\bplease\s+(always|never|do not|don't)\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    async def _remember_user_later(
        self,
        *,
        user_id: str,
        author: str,
        user_message: str,
        bot_reply: str,
    ) -> None:
        if not settings.user_memory_enabled:
            return
        if not self._has_memory_signal(user_message):
            return

        existing = self.user_memory.load(user_id, author)
        memory_system = (
            "Du pflegst ein kompaktes, lokales Gedächtnis für einen Twitch-Chatbot. "
            "Extrahiere NUR dauerhafte, hilfreiche Fakten oder Präferenzen über den User: "
            "Name/Anrede, Interessen, Humor, technische Vorlieben, wiederkehrende Wünsche. "
            "Speichere Sprache nur, wenn der User ausdrücklich eine dauerhafte Sprachpräferenz nennt; einmalige Sprachwechsel werden automatisch gezählt und gehören NICHT in diese Notizen. "
            "Speichere KEINE einmaligen Fragen, keine Bot-Test-Zwischenstände, keine Chat-Stimmung, keine temporären Themen, keine sensiblen Daten und keine Geheimnisse. "
            "Wenn nichts Neues dauerhaft Nützliches dabei ist, antworte exakt: KEINE. "
            "Sonst antworte mit 1-3 kurzen Markdown-Bullets."
        )
        memory_prompt = (
            f"User: {author} ({user_id})\n\n"
            f"Bestehende Notizen:\n{existing[-1600:]}\n\n"
            f"Neue User-Nachricht:\n{user_message}\n\n"
            f"Bot-Antwort:\n{bot_reply}\n\n"
            "Welche neuen dauerhaften Notizen sollen gespeichert werden?"
        )

        try:
            notes = await self.llm.complete(
                memory_system, [("user", memory_prompt)], allow_prefill=False
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("User-Gedächtnis konnte nicht ausgewertet werden")
            return

        if not notes or notes.strip().lower().startswith("keine"):
            return
        if self.llm._looks_like_reasoning(notes):
            return
        try:
            self.user_memory.append(user_id, author, notes)
        except OSError:
            LOGGER.exception("User-Gedächtnis konnte nicht gespeichert werden")

    def _reply_matches_language(self, reply: str, language: str) -> bool:
        if language == LANGUAGE_DEFAULT:
            return True
        return detect_message_language(reply) == language

    def _memory_excerpt(self, memory: str) -> str:
        """Filtert aus dem User-Gedächtnis nur die inhaltlichen Notiz-Bullets.

        Kopf- und Verwaltungszeilen (ID, Anzeigename, Sprachstatistik,
        Interaktionszähler) haben im Prompt nichts verloren - genau solche
        Label-Zeilen spiegeln Modelle gern wörtlich zurück.
        """
        skip_prefixes = (
            "- Twitch-User-ID:",
            "- Anzeigename zuletzt gesehen:",
            "- Interaktionen:",
            "- Häufigste Sprache:",
            "- Zählung:",
            "- Hinweis:",
        )
        lines = [
            line.strip()
            for line in memory.splitlines()
            if line.strip().startswith("- ") and not line.strip().startswith(skip_prefixes)
        ]
        return "\n".join(lines[-8:])

    def _chat_system_prompt(
        self,
        *,
        author: str,
        language: str,
        dominant_language: str | None,
        memory: str,
        wants_chat_context: bool,
    ) -> str:
        """System-Prompt für eine konkrete Antwort: Basis + Situation + Sprache.

        Alle Meta-Infos (Adressat, Sprache, Notizen, Aufgabe) leben bewusst
        hier statt in der User-Nachricht: Die Konversation selbst bleibt ein
        sauberer Chat, damit Modelle keine Labels, Notizen oder
        Prompt-Bausteine in den Twitch-Chat zurückspiegeln.
        """
        parts = [build_system_prompt(self.context)]
        parts.append(
            f"Du antwortest jetzt direkt {author}. Genau diese Person ist gemeint - "
            "nicht andere Namen aus dem Verlauf."
        )
        parts.append(language_reply_instruction(language, dominant_language))
        notes = self._memory_excerpt(memory)
        if notes:
            parts.append(
                f"Stille Hintergrundnotizen zu {author} (niemals erwähnen, zitieren oder aufzählen):\n{notes}"
            )
        if wants_chat_context:
            parts.append(
                "Die aktuelle Nachricht fragt nach Chat/Stimmung/Verlauf: fasse die bisherigen "
                "Chatnachrichten konkret zusammen. Wenn wenig los war oder hauptsächlich "
                "Bot-Tests liefen, sag das ehrlich."
            )
        else:
            parts.append(
                "Beantworte nur die letzte Nachricht. Ältere Nachrichten sind reiner Kontext "
                "und werden nicht ungefragt zusammengefasst."
            )
        parts.append(language_final_reminder(language))
        return "\n\n".join(parts)

    async def _complete_reply(
        self,
        system_prompt: str,
        turns: list[tuple[str, str]],
        language: str,
    ) -> str | None:
        """LLM-Aufruf mit zwei gezielten Retries: leere Antwort und falsche Sprache.

        Beide Retries hängen schlicht einen weiteren User-Turn an, statt den
        Prompt mit Beispiel-Labels vollzustopfen (die das Modell sonst lernt
        und zurückspiegelt).
        """
        final_label = language_final_label(language)
        reply = await self.llm.complete(system_prompt, turns, final_label=final_label)

        if not reply:
            retry_turns = [
                *turns,
                (
                    "user",
                    "(Hinweis: Deine letzte Ausgabe kam leer oder als interne Analyse/Metadaten an. "
                    "Schreib jetzt ausschließlich die fertige Chat-Nachricht - ohne Labels, "
                    "ohne Gedanken, ohne Vorwort.)",
                ),
            ]
            reply = await self.llm.complete(system_prompt, retry_turns, final_label=final_label)
            if not reply:
                return None

        if self._reply_matches_language(reply, language):
            return reply

        LOGGER.warning(
            "LLM-Antwort war in der falschen Sprache (%s erwartet): %s",
            language,
            reply[:160],
        )
        correction_turns = [
            *turns,
            ("assistant", reply),
            (
                "user",
                "(Bitte schreibe deine letzte Antwort komplett neu, nur als fertige "
                f"Chat-Nachricht. {language_final_reminder(language)})",
            ),
        ]
        retry = await self.llm.complete(system_prompt, correction_turns, final_label=final_label)
        if retry and self._reply_matches_language(retry, language):
            return retry
        LOGGER.warning("LLM blieb bei falscher Sprache; Antwort wird verworfen")
        return None

    async def _respond(
        self,
        payload: twitchio.ChatMessage,
        *,
        author: str,
        trigger: str,
    ) -> None:
        """Erzeugt eine LLM-Antwort auf eine Erwähnung und schickt sie in den Chat.

        Nutzt einen Lock: ist der Bot gerade am Denken, wird ein weiterer
        Trigger einfach verworfen, statt sich aufzustauen.
        """
        if self._is_own_message(payload):
            LOGGER.debug("Eigener Trigger ignoriert: %s", trigger)
            return
        if self._llm_lock.locked():
            LOGGER.debug("LLM beschäftigt, Trigger von %s verworfen", author)
            return

        current_language = detect_message_language(trigger)

        async with self._llm_lock:
            if self._broadcaster:
                await self.context.refresh(self, self._broadcaster)

            memory = self.user_memory.load(payload.chatter.id, author)
            dominant_language = self.user_memory.dominant_language(memory)
            if settings.user_memory_enabled:
                self.user_memory.update_language_profile(
                    payload.chatter.id, author, current_language
                )

            reply: str | None
            if self._is_stream_context_question(trigger) and self._has_known_stream_context():
                reply = self._stream_context_answer(current_language)
            else:
                wants_chat_context = self._is_chat_context_question(trigger)
                limit = (
                    settings.history_length
                    if wants_chat_context
                    else min(6, settings.history_length)
                )
                raw_text = str(getattr(payload, "text", "") or "").strip()
                current_entry = ("user", f"{author}: {raw_text}") if raw_text else None
                turns = self._history_turns(limit=limit, current=current_entry)
                turns.append(("user", f"{author}: {trigger}"))
                system_prompt = self._chat_system_prompt(
                    author=author,
                    language=current_language,
                    dominant_language=dominant_language,
                    memory=memory,
                    wants_chat_context=wants_chat_context,
                )

                reply = await self._complete_reply(system_prompt, turns, current_language)

        if not reply:
            fallback = self._fallback_reply(trigger, current_language)
            if not fallback:
                LOGGER.warning("LLM hat keine brauchbare Antwort geliefert")
                return
            reply = fallback

        reply = self._polish_reply(reply, trigger=trigger, author=author, language=current_language)
        if self.llm._looks_like_reasoning(reply):
            LOGGER.warning(
                "Aufbereitete Antwort war Metadaten/Kontext-Leak und wird ersetzt: %r", reply
            )
            fallback = self._fallback_reply(trigger, current_language)
            if not fallback:
                LOGGER.warning("LLM hat keine brauchbare Antwort geliefert")
                return
            reply = fallback

        try:
            # respond() sendet die Nachricht in den Kanal der Ursprungsnachricht.
            await payload.respond(reply)
            self._remember_bot_message(reply)
            self._remember_opener(reply)
            self._record_user_interaction(payload.chatter.id, trigger, reply)
            LOGGER.info("Antwort gesendet: %s", reply)
            asyncio.create_task(
                self._remember_user_later(
                    user_id=payload.chatter.id,
                    author=author,
                    user_message=trigger,
                    bot_reply=reply,
                )
            )
            self._maybe_summarize_profile(payload.chatter.id, author)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Konnte Nachricht nicht senden")

    # ----- Profil-Consolidation & Opener-Tracking --------------------------- #
    def _record_user_interaction(self, user_id: str, user_message: str, bot_reply: str) -> None:
        """Puffert ein (User-Nachricht, Bot-Antwort)-Paar für die Consolidation."""
        buf = self._user_interactions.setdefault(
            user_id, deque(maxlen=settings.profile_interactions_kept)
        )
        buf.append((user_message, bot_reply))

    def _maybe_summarize_profile(self, user_id: str, author: str) -> None:
        """Startet ggf. eine Hintergrund-Consolidation des User-Profils."""
        if not settings.user_memory_enabled:
            return
        if user_id in self._summarizing:
            return
        memory = self.user_memory.load(user_id, author)
        count = self.user_memory.interaction_count(memory)
        if not self.user_memory.should_summarize(count):
            return
        self._summarizing.add(user_id)
        asyncio.create_task(self._summarize_profile_later(user_id=user_id, author=author))

    async def _summarize_profile_later(self, *, user_id: str, author: str) -> None:
        """Fasst das Profil einer Person reichhaltig zusammen (Hintergrund-Task).

        Kombiniert bestehende Notizen mit den letzten direkten Gesprächen zu
        einem aktuellen Profil: WER die Person ist und WIE der Bot mit ihr
        reden soll. Die gesamte '## Notizen'-Sektion wird konsolidiert neu
        geschrieben (kein ungeprüftes Anwachsen über Monate).
        """
        try:
            existing = self.user_memory.load(user_id, author)
            pairs = list(self._user_interactions.get(user_id, []))
            dialogue = (
                "\n".join(
                    f"User: {user_msg}\nBot: {bot or '(keine Antwort)'}"
                    for user_msg, bot in pairs[-settings.profile_interactions_kept :]
                )
                or "(noch keine direkten Gespräche)"
            )
            system = (
                "Du pflegst ein kompaktes, lokales Gedächtnis eines Twitch-Chatbots über eine bestimmte Person. "
                "Fasse ALLES Bekannte (bestehende Notizen + die gezeigten Gespräche) zu einem aktuellen, klaren Profil zusammen, "
                "das genau beschreibt, WER die Person ist und WIE der Bot mit ihr reden sollte, wenn sie ihn anspricht. "
                "Nutze nur Kategorien, zu denen wirklich etwas da ist, als kurze Markdown-Bullets:\n"
                "- Anrede/Name: Wie soll der Bot die Person nennen/anreden?\n"
                "- Interessen/Themen: Worum geht's häufig?\n"
                "- Humor & Stil: frech? trocken? ernst? schräg?\n"
                "- Distanz/Ton: wie locker/formell, Duzen?\n"
                "- Wünsche/Präferenzen: wiederkehrende Bitten oder No-Gos\n"
                "- Sonstiges: Stimmung, Besonderheiten\n"
                "Regeln: Nur dauerhaft Nützliches. Keine einmaligen Fragen, keine aktuelle Chat-Stimmung, "
                "keine sensiblen/privaten Daten, keine Spekulation, keine Sprache als Notiz (wird separat gezählt). "
                "Behalte bewährte alte Notizen bei, kürze/verschmelze aber Doppeltes. "
                f"Maximal {settings.profile_max_notes} Bullets, jeder maximal ~25 Wörter. "
                "Wenn absolut nichts Profilwürdiges vorhanden ist, antworte exakt: KEINE."
            )
            prompt = (
                f"Person: {author} (ID {user_id})\n\n"
                f"Bestehende Notizen:\n{existing[-1600:]}\n\n"
                f"Aktuelle direkte Gespräche (älteste zuerst):\n{dialogue}\n\n"
                "Schreibe das konsolidierte Profil (nur die Bullets, kein Vorwort)."
            )

            try:
                summary = await self.llm.complete(system, [("user", prompt)], allow_prefill=False)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Profil-Consolidation fehlgeschlagen")
                return

            if not summary or summary.strip().lower().startswith("keine"):
                return
            if self.llm._looks_like_reasoning(summary):
                return
            try:
                self.user_memory.rewrite_notes(user_id, author, summary)
            except OSError:
                LOGGER.exception("Konsolidiertes Profil konnte nicht gespeichert werden")
        finally:
            self._summarizing.discard(user_id)

    def _opener_of(self, text: str) -> str:
        """Fingerprint der ersten Wörter einer Nachricht (Opener-Schutz)."""
        words = re.findall(r"\S+", (text or "").strip())
        return " ".join(words[:8]).lower()

    def _remember_opener(self, text: str) -> None:
        opener = self._opener_of(text)
        if opener:
            self._recent_openers.append(opener)

    def _is_recent_opener_repeat(self, text: str) -> bool:
        opener = self._opener_of(text)
        if not opener:
            return False
        for previous in self._recent_openers:
            if SequenceMatcher(None, opener, previous).ratio() >= 0.7:
                return True
        return False

    # ----- Idle: ereignisgesteuert statt starrer Poll-Zyklus ----------------- #
    def _start_idle_loop(self) -> None:
        """Startet den adaptiven Idle-Task genau einmal."""
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_loop())

    async def _stop_idle_loop(self) -> None:
        if self._idle_task is None:
            return
        task = self._idle_task
        self._idle_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _idle_next_delay(self) -> float:
        """Sekunden bis zur nächsten relevanten Idle-Prüfung.

        Kein fester 60s-Tick: Der Schlafzeitraum wird pro Iteration aus der
        letzten echten Chat-Aktivität neu berechnet - der Task wacht erst dann
        auf, wenn Stille wirklich ``idle_threshold`` erreicht (+ Jitter). Jede
        Chat-Nachricht verschiebt ``_last_activity`` und damit automatisch den
        nächsten Weckruf. Das ist interaktiv, kein fixer Zyklus.
        """
        threshold = settings.idle_threshold
        if threshold <= 0 or settings.idle_max_solo_messages <= 0:
            # Idle deaktiviert: nur selten prüfen (falls es per Env aktiviert wird).
            return 300.0
        jitter = random.uniform(0.0, max(0.0, float(settings.idle_jitter)))
        deadline = self._last_activity + threshold + jitter
        return max(1.0, deadline - time.monotonic())

    async def _idle_loop(self) -> None:
        """Langlaufender Hintergrund-Task: wacht nur auf, wenn Stille erreicht ist."""
        try:
            while True:
                delay = self._idle_next_delay()
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise

                idle_for = time.monotonic() - self._last_activity
                if idle_for < settings.idle_threshold:
                    continue
                if settings.idle_max_solo_messages <= 0:
                    continue
                if self._idle_messages_since_human >= settings.idle_max_solo_messages:
                    # Wartet auf eine echte Chat-Nachricht (resettet Zähler + Deadline).
                    continue
                if self._llm_lock.locked():
                    await asyncio.sleep(15.0)
                    continue
                await self._do_idle_message(idle_for)
        except asyncio.CancelledError:
            LOGGER.debug("Idle-Task wurde beendet")
            raise

    async def _do_idle_message(self, idle_for: float) -> None:
        """Erzeugt genau eine selbstinitiierte Idle-Nachricht und sendet sie.

        Wird nur aus ``_idle_loop`` gerufen, nachdem Stille bestätigt ist.
        Sprich keine Lurker an, sondern starte ein Topic / Gespräch.
        """
        if not self._broadcaster:
            return
        await self.context.refresh(self, self._broadcaster)
        if not self.context.is_live:
            LOGGER.debug("Stream offline, Idle-Chatter pausiert")
            return

        LOGGER.info("Chat ruhig (%.0fs), PandaBot wirft ein Topic ein", idle_for)
        async with self._llm_lock:
            system_prompt = build_system_prompt(self.context) + (
                "\n\nSondersituation: Der Chat ist still und niemand hat dich angesprochen. "
                "Du meldest dich von SELBST, um ein Gespräch sanft anzustoßen. "
                "Sprich NIEMALS Lurker an (kein 'ihr Lurker', keine Zuschauerzahlen, "
                "niemanden beim stillen Mitlesen outen oder bedrängen). Wirf stattdessen "
                "ein konkretes Topic, eine kleine Beobachtung oder eine offene Frage in den "
                "Raum. Antworte auf Deutsch, locker, kurz und ohne Druck - kein Betteln um "
                "Aktivität, kein 'schreibt mal was'."
            )
            recent_bot = (
                "\n".join(f"- {msg}" for msg in self._recent_bot_messages)
                or "(noch keine eigenen Bot-Nachrichten)"
            )
            recent_openers = "\n".join(f"- {op}" for op in self._recent_openers) or "(noch keine)"
            turns = self._history_turns(limit=settings.history_length)
            turns.append(
                (
                    "user",
                    (
                        f"(Stille seit etwa {int(idle_for)} Sekunden. Niemand hat geschrieben. "
                        "Schreibe GENAU EINE kurze, lockere Nachricht, die ein NEUES Gespräch "
                        f"startet - passend zu '{self.context.game}' oder einem allgemeinen Topic "
                        "(Frage, kleine Story, Beobachtung, Gesprächsaufhänger). "
                        "Wiederhole KEINEN dieser früheren eigenen Einstiege/Gags/Fragen:\n"
                        f"{recent_bot}\n"
                        "Und vermeide diese schon genutzten Gesprächsöffner (Thema/Wortwahl):\n"
                        f"{recent_openers}\n"
                        "Sprich keine Lurker an. Keine Meta-Sätze. "
                        "Nur deine fertige Chat-Nachricht.)"
                    ),
                )
            )
            reply = await self.llm.complete(system_prompt, turns)

        if reply and (self._is_recent_bot_repeat(reply) or self._is_recent_opener_repeat(reply)):
            LOGGER.info("Idle-Nachricht übersprungen (Wiederholung/ähnlicher Opener): %s", reply)
            self._last_activity = time.monotonic()
            self._idle_messages_since_human += 1
            return

        if reply and self._broadcaster:
            try:
                await self._broadcaster.send_message(reply, settings.bot_id)
                self._remember_bot_message(reply)
                self._remember_opener(reply)
                self._idle_messages_since_human += 1
            except Exception:  # noqa: BLE001
                LOGGER.exception("Konnte Idle-Nachricht nicht senden")

        self._last_activity = time.monotonic()


# --------------------------------------------------------------------------- #
#  Entry-Point
# --------------------------------------------------------------------------- #
def _select_llm_backend() -> None:
    """Fragt beim Start, ob PandaBot lokal oder online antworten soll."""
    configured = settings.llm_backend.strip().lower()
    # Env-Shortcut: 'online-a4b' / 'gemma-a4b' wählt direkt die MoE-Alternative.
    online_model_override: str | None = None
    if configured in ("online-a4b", "gemma-a4b", "a4b"):
        configured = "online"
        online_model_override = "gemma-4-26b-a4b-it"
    if configured in (
        "1",
        "l",
        "local",
        "lokal",
        "llama",
        "llama-server",
        "2",
        "3",
        "o",
        "online",
        "google",
        "gemini",
        "gemma",
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
    ):
        model = (
            "gemma-4-26b-a4b-it"
            if configured in ("3", "gemma-4-26b-a4b-it")
            else online_model_override
        )
        settings.apply_llm_backend(configured, online_model=model)
        LOGGER.info("LLM-Profil: %s", settings.llm_backend_label)
        return
    if configured not in ("", "ask", "prompt", "frage"):
        raise ValueError("LLM_BACKEND muss 'ask', 'local' oder 'online' sein.")

    if not sys.stdin.isatty():
        settings.apply_llm_backend("local")
        LOGGER.info(
            "Kein interaktives Terminal erkannt; nutze LLM-Profil: %s", settings.llm_backend_label
        )
        return

    prompt = (
        "\nPandaBot LLM auswählen:\n"
        f"  [1] Lokal: llama-server ({settings.llm_model} @ {settings.llm_url})\n"
        "  [2] Online: Google Gemma 4 31B IT (gemma-4-31b-it)\n"
        "  [3] Online: Google Gemma 4 26B A4B / MoE (gemma-4-26b-a4b-it)\n"
        "Auswahl [1/2/3, Enter=1]: "
    )
    while True:
        choice = input(prompt).strip().lower() or "1"
        if choice in ("1", "l", "local", "lokal", "llama", "llama-server"):
            settings.apply_llm_backend("local")
            break
        if choice in (
            "2",
            "3",
            "o",
            "online",
            "google",
            "gemini",
            "gemma",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        ):
            if not (settings.google_api_key or settings.llm_api_key):
                key = getpass.getpass(
                    "GOOGLE_API_KEY/GEMINI_API_KEY nicht gefunden. "
                    "API-Key jetzt eingeben (leer = lokal): "
                ).strip()
                if not key:
                    settings.apply_llm_backend("local")
                    break
                settings.google_api_key = key
            online_model = "gemma-4-26b-a4b-it" if choice in ("3", "gemma-4-26b-a4b-it") else None
            settings.apply_llm_backend("online", online_model=online_model)
            break
        print("Bitte 1/lokal, 2 oder 3/online eingeben.")

    LOGGER.info("LLM-Profil: %s", settings.llm_backend_label)


def main() -> None:
    twitchio.utils.setup_logging(level=logging.INFO)
    _select_llm_backend()

    async def runner() -> None:
        async with PandaBot() as bot:
            # Lädt gespeicherte Tokens aus .tio.tokens.json (falls vorhanden).
            await bot.start()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        LOGGER.warning("Beende PandaBot (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
