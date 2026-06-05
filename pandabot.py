"""PandaBot - Ein lokaler KI-Chatbot für Twitch.

Verbindet einen Twitch-Kanal via TwitchIO 3 (EventSub über WebSocket) mit einem
lokalen LLM (llama-server, OpenAI-kompatibel). Der Bot folgt dem Chat, antwortet
auf Erwähnungen und sorgt bei Stille für Unterhaltung. Stream-Titel und Spiel
werden live über die Twitch-Helix-API geholt.

Getestet mit TwitchIO 3.2.2 / Python 3.11+.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

import aiohttp
import twitchio
from twitchio import eventsub
from twitchio.ext import commands, routines

from config import settings

LOGGER: logging.Logger = logging.getLogger("pandabot")

BOT_SCOPES = "user:read:chat user:write:chat user:bot"
OWNER_SCOPES = "channel:bot"


# --------------------------------------------------------------------------- #
#  LLM-Client (llama-server, OpenAI-kompatibel)
# --------------------------------------------------------------------------- #
class LLMClient:
    """Kapselt die Kommunikation mit dem lokalen llama-server.

    Hält eine wiederverwendete aiohttp-Session offen (statt pro Anfrage eine
    neue aufzubauen) und kümmert sich um Timeouts, Stop-Strings und das
    Aufräumen typischer Halluzinationen kleiner Modelle.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
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
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def complete(self, system_prompt: str, user_prompt: str) -> str | None:
        """Schickt einen Chat-Completion-Request und gibt die Antwort zurück.

        Gibt ``None`` zurück, wenn der Server nicht erreichbar ist oder eine
        unbrauchbare Antwort liefert. Der Aufrufer entscheidet dann, ob er
        schweigt.
        """
        await self.open()
        assert self._session is not None

        payload = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{user_prompt}\n\n"
                        "WICHTIG: Gib NUR die finale Twitch-Chat-Antwort aus. "
                        "Sprich den Fragesteller direkt mit du an, nie in der dritten Person. "
                        "Keine Analyse, keine Gedanken, kein Englisch, kein Markdown/Fettdruck, kein Wiederholen der Frage, kein 'We need'."
                    ),
                },
                # MiniCPM5/Thinking-Templates starten sonst gern mit CoT. Diese
                # Prefix-Hilfe schließt den Thinking-Block und lenkt auf Finalausgabe.
                {"role": "assistant", "content": "<think></think>\nAntwort:"},
            ],
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "top_p": settings.llm_top_p,
            "stop": self._stop,
            "stream": False,
            # llama.cpp / Thinking-Modelle: MiniCPM/Qwen-artige Modelle liefern
            # sonst oft nur reasoning_content und ein leeres message.content.
            "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
            "reasoning_budget": 0,
        }
        # llama.cpp-spezifisch, hilft kleinen Modellen gegen Wiederholungen.
        # Nicht Teil der OpenAI-Spec, daher optional (siehe Config).
        if settings.llm_send_repeat_penalty:
            payload["repeat_penalty"] = settings.llm_repeat_penalty

        try:
            async with self._session.post(settings.llm_url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.warning("llama-server HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            LOGGER.warning("llama-server nicht erreichbar: %s", exc)
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

    def _sanitize(self, text: str | None) -> str | None:
        """Räumt typische Artefakte kleiner Modelle auf."""
        if not text:
            return None

        text = text.strip()

        # Thinking-Modelle liefern oft interne Gedanken vor der eigentlichen Antwort.
        text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
        text = re.sub(r"(?is)</?think>", "", text).strip()
        text = re.sub(r"\s+", " ", text).strip()

        # Wenn wir die Ausgabe mit "Antwort:" geprefillt haben, nur den finalen
        # Teil danach behalten.
        marker_match = re.search(r"(?i)(?:^|\s)(?:finale?\s+)?antwort\s*:\s*", text)
        if marker_match:
            text = text[marker_match.end() :].strip()

        # Manche Modelle stellen den eigenen Namen voran ("PandaBot: ...").
        for prefix in (f"{settings.bot_name}:", "PandaBot:", "Bot:", "Assistant:", "Antwort:"):
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

    def _looks_like_reasoning(self, text: str) -> bool:
        """Verhindert, dass Chain-of-Thought/Meta-Analyse in Twitch landet."""
        lowered = text.lower().lstrip()
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
        )
        return lowered.startswith(reasoning_starts)


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

    def load(self, user_id: str, display_name: str) -> str:
        path = self._path(user_id)
        if not path.exists():
            return "(keine gespeicherten Notizen)"
        try:
            return path.read_text(encoding="utf-8").strip() or "(keine gespeicherten Notizen)"
        except OSError:
            LOGGER.exception("Konnte User-Gedächtnis nicht lesen: %s", path)
            return "(Gedächtnis gerade nicht lesbar)"

    def append(self, user_id: str, display_name: str, notes: str) -> None:
        cleaned = self._clean_notes(notes)
        if not cleaned:
            return

        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(user_id)
        if path.exists():
            current = path.read_text(encoding="utf-8")
        else:
            current = (
                f"# User-Gedächtnis: {display_name}\n\n"
                f"- Twitch-User-ID: {user_id}\n"
                f"- Anzeigename zuletzt gesehen: {display_name}\n\n"
                "## Notizen\n"
            )

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
    """Baut den System-Prompt mit Live-Kontext.

    Kurz und konkret gehalten - kleine Modelle folgen knappen, klaren
    Anweisungen deutlich zuverlässiger als langen Persona-Texten.
    """
    return (
        f"Du bist {settings.bot_name}, ein freundlicher, witziger Chatbot im "
        f"Twitch-Stream von {settings.channel_name}. "
        f"Aktueller Stream-Kontext: Spiel/Kategorie='{ctx.game}', Titel='{ctx.title}'. "
        "Stil: natürlich, direkt und conversational wie ein guter GPT-4o-Chat, aber als Twitch-Chatbot. "
        "Antworte IMMER auf Deutsch. Meist 1-2 kurze Sätze; bei Geschichten oder Witzen darf es etwas länger sein. "
        "Beantworte genau die aktuelle Bitte: Wenn jemand einen Witz oder eine Geschichte will, erzähl sie; wenn jemand eine Erklärung will, erklär kurz. "
        "Bei frechen oder bösen Witzen darfst du trockenen, leicht schwarzen Humor nutzen, aber keine Hassrede und keine stumpfen Beleidigungen gegen echte Personen. "
        "Wenn nach Google/Websuche/Live-Recherche gefragt wird, sag ehrlich kurz, dass du hier keinen Browserzugriff hast, und biete stattdessen eine kurze Einordnung aus vorhandenem Wissen an. "
        "Wenn nach dem heutigen Stream oder Thema gefragt wird, nutze den Stream-Kontext. "
        "Der Chatverlauf ist nur Zusatzkontext: fasse ihn nur zusammen, wenn ausdrücklich nach Chat, Stimmung, Verlauf oder Zusammenfassung gefragt wird. "
        "Persönliche Notizen zum Fragesteller sind nur leise Hinweise, keine Pflichtliste und kein Gesprächsthema. "
        "Sprich den Fragesteller direkt mit 'du' an; rede nicht in der dritten Person über ihn. "
        "Kein steifer Assistententon, keine Meta-Sätze wie 'ich antworte jetzt', keine Analyse, kein Wiederholen der Frage, kein Markdown/Fettdruck. "
        "Nutze Spiel und Titel nur als Kontext, aber kopiere keine Deko, Commands, Emotes oder Insider wie XD/420 aus dem Titel. "
        "Emojis sparsam verwenden, höchstens eins, und nur wenn es wirklich passt. "
        "Sei locker und unterhaltsam, aber nie beleidigend. Schreibe nur deine finale Antwort."
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

        # Rollierender Chatverlauf (deque begrenzt automatisch die Länge).
        self.chat_history: deque[str] = deque(maxlen=settings.history_length)
        self._broadcaster: twitchio.PartialUser | None = None

        # Echter Twitch-Login-Name des Bot-Accounts. Wird in event_ready aus
        # der bot_id aufgelöst, damit Erwähnungen am tatsächlichen Account-Namen
        # hängen und nicht am kosmetischen bot_name (Spitzname).
        self._bot_login: str | None = None

        self._last_activity = time.monotonic()
        self._chat_subscription_active = False
        # Lock statt bool-Flag: verhindert sauber parallele LLM-Aufrufe.
        self._llm_lock = asyncio.Lock()

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
        LOGGER.warning("Danach PandaBot mit Strg+C beenden und erneut mit 'python pandabot.py' starten.")

    async def _subscribe_chat(self) -> None:
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=settings.owner_id,
            user_id=settings.bot_id,
        )
        await self.subscribe_websocket(payload=subscription)
        self._chat_subscription_active = True
        LOGGER.info("Chat-Subscription für Kanal %s aktiv", settings.channel_name)

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload) -> None:
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
            self.idle_chatter.start()
        LOGGER.info(
            "PandaBot (%s, Account: %s) ist online und mit %s verbunden!",
            settings.bot_name,
            self._bot_login or "?",
            settings.channel_name,
        )

    async def close(self, **options: object) -> None:
        # Sauberes Herunterfahren: Routine stoppen, Session schließen.
        # cancel() ist intern gegen "kein laufender Task" abgesichert, daher
        # kein vorheriger Status-Check nötig (Routine hat kein .running).
        self.idle_chatter.cancel()
        await self.llm.close()
        await super().close(**options)

    # ----- Chat-Handling ----------------------------------------------------- #
    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        # Eigene Nachrichten des Bots: in Verlauf aufnehmen, sonst ignorieren.
        if payload.chatter.id == settings.bot_id:
            self.chat_history.append(f"{settings.bot_name}: {payload.text}")
            return

        # display_name und name sind laut Typ optional; mit der (immer
        # vorhandenen) ID als Fallback ist author garantiert ein str.
        author = payload.chatter.display_name or payload.chatter.name or str(payload.chatter.id)
        text = payload.text.strip()
        if not text:
            return

        self._last_activity = time.monotonic()
        self.chat_history.append(f"{author}: {text}")
        LOGGER.info("Chat empfangen von %s: %s", author, text)

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

    def _is_mention(self, text: str) -> bool:
        lowered = text.lower()
        # Trigger-Namen: der echte Login-Name des Bot-Accounts (sobald bekannt)
        # und der kosmetische Anzeigename. Doppelte werden über das set entfernt.
        names = {settings.bot_name.lower()}
        if self._bot_login:
            names.add(self._bot_login.lower())

        for name in names:
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
        )
        return any(keyword in lowered for keyword in keywords)

    def _is_stream_context_question(self, text: str) -> bool:
        lowered = text.lower()
        # Nicht auf jede Erwähnung von "Stream/Thema" routen: "erzähl einen Witz,
        # der nichts mit dem Streamthema zu tun hat" ist eine Witz-Anfrage, keine
        # Frage nach dem Stream-Inhalt.
        if re.search(r"\b(witz|geschichte|story|erzähl|erzaehl|witzig|joke)\b", lowered):
            return False
        if re.search(r"\b(testen|ideen|vorschläge|vorschlaege|was können wir|was koennen wir)\b", lowered):
            return False
        if "nicht" in lowered or "nichts mit" in lowered:
            return False

        patterns = (
            r"^\s*um\s+was\s+gehts?n?\s*\??\s*$",
            r"\bum\s+was\s+geht(?:'s|s|\s+es)?.*\bstream\b",
            r"\bwas\s+geht(?:'s|s|\s+es)?.*\bstream\b",
            r"\b(wovon|von\s+was)\s+handelt.*\bstream\b",
            r"\bwas\s+ist.*\b(thema|streamthema)\b",
            r"\bwas\s+läuft.*\b(heute|stream)\b",
            r"\bwas\s+wird.*\bgestreamt\b",
            r"\bwelches\s+thema\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _stream_context_answer(self) -> str:
        title_parts: list[str] = []
        for raw in self.context.title.split("|"):
            part = raw.strip()
            if not part or part.startswith("[") or "!" in part:
                continue
            part = re.sub(r"(?i)\bxd\b|\b420\b", "", part)
            part = re.sub(r"[^\w\s./+#-]", "", part).strip(" -")
            if len(part) < 4 or not re.search(r"[A-Za-zÄÖÜäöüß]", part):
                continue
            title_parts.append(part)

        if title_parts:
            detail = ", ".join(title_parts[:2])
            return f"Grob geht’s um {self.context.game}; heute offenbar mit Fokus auf {detail}."
        return f"Grob geht’s heute um {self.context.game}; der genaue Fokus ergibt sich gerade im Stream."

    def _fallback_reply(self, trigger: str) -> str | None:
        lowered = trigger.lower()
        if "was geht" in lowered:
            return "Alles entspannt, ich häng im Chat rum und warte auf Chaos. Was geht bei dir?"
        if "was stimmt nicht" in lowered:
            return "Vermutlich zu viel Modell-Koffein und zu wenig Feinschliff. Aber hey, wir debuggen mich ja gerade live."
        if "google" in lowered or "websuche" in lowered or "suche" in lowered:
            return "Live googeln kann ich hier nicht direkt, aber ich kann dir aus meinem Wissen kurz einordnen, worum es geht."
        return None

    def _polish_reply(self, reply: str, *, trigger: str, author: str) -> str:
        text = reply.strip()
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text).strip()

        # Wenn das Modell die Frage als ersten Satz wiederholt, weg damit.
        first_sentence = re.match(r"^(.{1,180}?[.!?])\s+(.*)$", text)
        if first_sentence:
            first = re.sub(r"[^\wäöüÄÖÜß]+", " ", first_sentence.group(1)).strip().lower()
            trig = re.sub(r"[^\wäöüÄÖÜß]+", " ", trigger).strip().lower()
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
        if not re.search(r"[A-Za-zÄÖÜäöüß0-9]", text) or len(text) < 8:
            return self._fallback_reply(trigger) or reply.strip()

        return text

    def _format_recent_history(self, *, include_for_context: bool) -> str:
        previous = list(self.chat_history)[:-1]
        if not previous:
            return "(noch keine vorherigen Chatnachrichten)"
        limit = settings.history_length if include_for_context else min(4, settings.history_length)
        return "\n".join(previous[-limit:])

    def _has_memory_signal(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bmerk(?:\s+dir)?\b",
            r"\berinner\b",
            r"\bich\s+(mag|liebe|hasse|bevorzuge|will|möchte|moechte|bin|heiße|heisse)\b",
            r"\bmein(?:e|er|en|em)?\s+\w+\s+ist\b",
            r"\bnenn\s+mich\b",
            r"\bbitte\s+(immer|nie|nicht)\b",
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
            "Name/Anrede, Sprache, Interessen, Humor, technische Vorlieben, wiederkehrende Wünsche. "
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
            notes = await self.llm.complete(memory_system, memory_prompt)
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
        if self._llm_lock.locked():
            LOGGER.debug("LLM beschäftigt, Trigger von %s verworfen", author)
            return

        async with self._llm_lock:
            if self._broadcaster:
                await self.context.refresh(self, self._broadcaster)

            if self._is_stream_context_question(trigger):
                reply = self._stream_context_answer()
            else:
                system_prompt = build_system_prompt(self.context)
                wants_chat_context = self._is_chat_context_question(trigger)
                history = self._format_recent_history(include_for_context=wants_chat_context)
                memory = self.user_memory.load(payload.chatter.id, author)
                if wants_chat_context:
                    task = (
                        "Die Anfrage fragt nach Chat/Stimmung/Verlauf: fasse die vorherigen Chatnachrichten konkret zusammen. "
                        "Wenn wenig los war oder hauptsächlich Bot-Tests liefen, sag das ehrlich."
                    )
                else:
                    task = (
                        "Beantworte die aktuelle Anfrage direkt. Nutze vorherige Chatnachrichten und User-Notizen nur, wenn sie helfen; "
                        "fasse den Chat nicht ungefragt zusammen."
                    )
                user_prompt = (
                    f"Du antwortest direkt an: {author}\n"
                    f"Interne Stilhinweise zu dieser Person (nicht erwähnen, nicht paraphrasieren):\n{memory[-1200:]}\n\n"
                    f"Optionale vorherige Chatnachrichten (nur Kontext, nicht nacherzählen):\n{history}\n\n"
                    f"Aktuelle Anfrage, die du jetzt beantworten musst: {trigger}\n\n"
                    f"Aufgabe: {task}\n"
                    "Schreibe jetzt nur die natürliche Antwort an diese Person. Keine dritte Person, kein Markdown, keine Frage-Wiederholung."
                )

                reply = await self.llm.complete(system_prompt, user_prompt)

        if not reply:
            fallback = self._fallback_reply(trigger)
            if not fallback:
                LOGGER.warning("LLM hat keine brauchbare Antwort geliefert")
                return
            reply = fallback

        reply = self._polish_reply(reply, trigger=trigger, author=author)

        try:
            # respond() sendet die Nachricht in den Kanal der Ursprungsnachricht.
            await payload.respond(reply)
            LOGGER.info("Antwort gesendet: %s", reply)
            asyncio.create_task(
                self._remember_user_later(
                    user_id=payload.chatter.id,
                    author=author,
                    user_message=trigger,
                    bot_reply=reply,
                )
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Konnte Nachricht nicht senden")

    # ----- Idle-Routine ------------------------------------------------------ #
    @routines.routine(delta=datetime.timedelta(seconds=60))
    async def idle_chatter(self) -> None:
        """Meldet sich, wenn der Chat länger als ``idle_threshold`` still ist."""
        idle_for = time.monotonic() - self._last_activity
        if idle_for < settings.idle_threshold or self._llm_lock.locked():
            return
        if not self._broadcaster:
            return

        # Nur aktiv werden, wenn der Stream tatsächlich live ist.
        await self.context.refresh(self, self._broadcaster)
        if not self.context.is_live:
            LOGGER.debug("Stream offline, Idle-Chatter pausiert")
            return

        LOGGER.info("Chat ruhig (%.0fs), PandaBot wird aktiv", idle_for)
        # Wir bauen den Idle-Prompt ohne konkrete Nachricht; senden via Broadcaster.
        async with self._llm_lock:
            system_prompt = build_system_prompt(self.context)
            history = "\n".join(self.chat_history) or "(noch keine Nachrichten)"
            user_prompt = (
                f"Der Chat ist gerade ruhig. Letzte Nachrichten:\n{history}\n\n"
                "Schreibe eine kurze, lockere Nachricht passend zum Spiel oder "
                "Stream, um den Chat zu beleben. Stelle ruhig eine Frage."
            )
            reply = await self.llm.complete(system_prompt, user_prompt)

        if reply and self._broadcaster:
            try:
                await self._broadcaster.send_message(reply, settings.bot_id)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Konnte Idle-Nachricht nicht senden")

        self._last_activity = time.monotonic()

    # ----- Beispiel-Commands ------------------------------------------------- #
    @commands.command()
    async def panda(self, ctx: commands.Context, *, frage: str | None = None) -> None:
        """Direkt mit dem Bot reden: !panda <deine Frage>"""
        if not frage:
            await ctx.reply("Frag mich was! Beispiel: !panda wie läuft's?")
            return
        author = ctx.chatter.display_name or ctx.chatter.name or str(ctx.chatter.id)
        if self._broadcaster:
            await self.context.refresh(self, self._broadcaster)
        async with self._llm_lock:
            if self._is_stream_context_question(frage):
                reply = self._stream_context_answer()
            else:
                system_prompt = build_system_prompt(self.context)
                wants_chat_context = self._is_chat_context_question(frage)
                history = self._format_recent_history(include_for_context=wants_chat_context)
                memory = self.user_memory.load(ctx.chatter.id, author)
                if wants_chat_context:
                    task = "Fasse die letzten Chatnachrichten konkret zusammen; wenn wenig los war, sag das ehrlich."
                else:
                    task = "Beantworte die aktuelle Anfrage direkt und fasse den Chat nicht ungefragt zusammen."
                user_prompt = (
                    f"Du antwortest direkt an: {author}\n"
                    f"Interne Stilhinweise zu dieser Person (nicht erwähnen, nicht paraphrasieren):\n{memory[-1200:]}\n\n"
                    f"Optionale vorherige Chatnachrichten (nur Kontext, nicht nacherzählen):\n{history}\n\n"
                    f"Aktuelle Anfrage, die du jetzt beantworten musst: {frage}\n\n"
                    f"Aufgabe: {task}\n"
                    "Schreibe jetzt nur die natürliche Antwort an diese Person. Keine dritte Person, kein Markdown, keine Frage-Wiederholung."
                )
                reply = await self.llm.complete(system_prompt, user_prompt)
        answer = self._polish_reply(reply, trigger=frage, author=author) if reply else "Mein KI-Hirn macht gerade Pause 🐼"
        await ctx.reply(answer)
        LOGGER.info("Command-Antwort gesendet: %s", answer)
        asyncio.create_task(
            self._remember_user_later(
                user_id=ctx.chatter.id,
                author=author,
                user_message=frage,
                bot_reply=answer,
            )
        )


# --------------------------------------------------------------------------- #
#  Entry-Point
# --------------------------------------------------------------------------- #
def main() -> None:
    twitchio.utils.setup_logging(level=logging.INFO)

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