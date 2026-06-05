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
            "\n",
            f"{settings.bot_name}:",
            "User:",
            "Chat:",
            "<|",
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
                {"role": "user", "content": user_prompt},
            ],
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "top_p": settings.llm_top_p,
            "stop": self._stop,
            "stream": False,
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

        return self._sanitize(reply)

    def _sanitize(self, text: str | None) -> str | None:
        """Räumt typische Artefakte kleiner Modelle auf."""
        if not text:
            return None

        text = text.strip()

        # Manche Modelle stellen den eigenen Namen voran ("PandaBot: ...").
        for prefix in (f"{settings.bot_name}:", "PandaBot:", "Bot:"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :].strip()

        # Umschließende Anführungszeichen entfernen.
        if len(text) >= 2 and text[0] in "\"'" and text[-1] in "\"'":
            text = text[1:-1].strip()

        if not text:
            return None

        # Twitch-Hardlimit sind 500 Zeichen; wir kürzen defensiv sauber.
        if len(text) > settings.max_message_length:
            text = text[: settings.max_message_length - 1].rstrip() + "…"

        return text


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
        f"Der Streamer spielt gerade '{ctx.game}'. Der Stream-Titel ist '{ctx.title}'. "
        "Regeln: Antworte IMMER auf Deutsch. Maximal ein bis zwei kurze Sätze. "
        "Sei locker und unterhaltsam, aber nie beleidigend. "
        "Schreibe nur deine eigene Antwort, kein Rollenspiel, keine Namen voranstellen. "
        "Keine Emotes-Codes erfinden."
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

        # !-Befehle laufen weiter durch die normale Command-Verarbeitung.
        if text.startswith("!"):
            return

        if self._is_mention(text):
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

            system_prompt = build_system_prompt(self.context)
            history = "\n".join(self.chat_history)
            user_prompt = (
                f"Aktueller Chatverlauf:\n{history}\n\n"
                f"{author} hat dich gerade angesprochen: '{trigger}'\n"
                f"Antworte direkt und passend auf {author}."
            )

            reply = await self.llm.complete(system_prompt, user_prompt)

        if not reply:
            return

        try:
            # respond() sendet die Nachricht in den Kanal der Ursprungsnachricht.
            await payload.respond(reply)
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
        system_prompt = build_system_prompt(self.context)
        history = "\n".join(self.chat_history)
        user_prompt = (
            f"Aktueller Chatverlauf:\n{history}\n\n"
            f"{author} fragt: '{frage}'\nAntworte direkt auf {author}."
        )
        async with self._llm_lock:
            reply = await self.llm.complete(system_prompt, user_prompt)
        await ctx.reply(reply or "Mein KI-Hirn macht gerade Pause 🐼")


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