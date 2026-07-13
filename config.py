"""Zentrale Konfiguration für PandaBot.

Liest Werte aus Umgebungsvariablen bzw. einer ``.env``-Datei. So bleiben
Secrets (Client-ID/Secret) aus dem Code heraus und müssen nicht versioniert
werden. Kopiere ``.env.example`` zu ``.env`` und trage deine Werte ein.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    load_dotenv()
except ModuleNotFoundError:  # python-dotenv ist optional
    pass


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Pflicht-Variable '{name}' fehlt. Lege eine .env an "
            f"(siehe .env.example) oder setze sie als Umgebungsvariable."
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    """Float aus der Umgebung; bei Tippfehlern Default statt Crash beim Import."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        print(
            f"WARNUNG: {name}={value!r} ist keine gültige Zahl - nutze Default {default}.",
            file=sys.stderr,
        )
        return default


def _env_int(name: str, default: int) -> int:
    """Int aus der Umgebung; bei Tippfehlern Default statt Crash beim Import."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        print(
            f"WARNUNG: {name}={value!r} ist keine gültige Ganzzahl - nutze Default {default}.",
            file=sys.stderr,
        )
        return default


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _google_native_base_url(url: str) -> str:
    """Migrate old OpenAI-compatible Google URLs to the native API base."""
    clean = url.strip().rstrip("/")
    for suffix in ("/openai/chat/completions", "/openai"):
        if clean.endswith(suffix):
            return clean[: -len(suffix)]
    return clean


@dataclass
class Settings:
    # --- Twitch / App ---
    client_id: str = field(default_factory=lambda: _require("TWITCH_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _require("TWITCH_CLIENT_SECRET"))
    bot_id: str = field(default_factory=lambda: _require("TWITCH_BOT_ID"))
    owner_id: str = field(default_factory=lambda: _require("TWITCH_OWNER_ID"))

    channel_name: str = field(default_factory=lambda: os.getenv("TWITCH_CHANNEL", "dawasteh"))
    bot_name: str = field(default_factory=lambda: os.getenv("TWITCH_BOT_NAME", "PandaBot"))

    # --- LLM (lokaler llama-server oder Google/Gemma, beide OpenAI-kompatibel) ---
    llm_backend: str = field(default_factory=lambda: os.getenv("LLM_BACKEND", "ask"))
    llm_backend_label: str = "lokaler llama-server"
    llm_url: str = field(
        default_factory=lambda: os.getenv(
            "LLM_SERVER_URL", "http://127.0.0.1:1235/v1/chat/completions"
        )
    )
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "local-model"))
    llm_api_key: str | None = field(default_factory=lambda: _first_env("LLM_API_KEY"))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.8))
    llm_top_p: float = field(default_factory=lambda: _env_float("LLM_TOP_P", 0.95))
    llm_repeat_penalty: float = field(
        default_factory=lambda: _env_float("LLM_REPEAT_PENALTY", 1.15)
    )
    # repeat_penalty ist ein llama.cpp-Extra (nicht Teil der OpenAI-Spec).
    # Bei anderen Backends (z. B. vLLM) heißt der Parameter anders bzw. wird
    # nur über extra_body akzeptiert; auf "false" setzen, um ihn wegzulassen.
    llm_send_repeat_penalty: bool = field(
        default_factory=lambda: _env_bool("LLM_SEND_REPEAT_PENALTY", True)
    )
    llm_send_llama_extras: bool = field(
        default_factory=lambda: _env_bool("LLM_SEND_LLAMA_EXTRAS", True)
    )
    # System-Rolle nutzen? Gemma 4 via native generateContent unterstützt
    # System Instructions; lokal oder über ältere Endpunkte ist das nicht
    # garantiert.  Das Online-Profil belässt es per Default auf true (Gemma 4
    # kann System Instructions), per Env übersteuerbar.
    llm_use_system_role: bool = field(
        default_factory=lambda: _env_bool("LLM_USE_SYSTEM_ROLE", True)
    )
    # Transport: "openai" (chat/completions) oder "google_native" (generateContent).
    # Das Online-Profil setzt automatisch "google_native".
    llm_transport: str = field(default_factory=lambda: os.getenv("LLM_TRANSPORT", "openai"))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 80))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 20))

    # --- Google/Gemma Online-Profil ---
    # Basis-URL für den nativen generateContent-Transport.  Der Bot hängt
    # /models/{model}:generateContent selbst an.
    google_llm_url: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_LLM_SERVER_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        )
    )
    google_llm_model: str = field(
        default_factory=lambda: os.getenv("GOOGLE_LLM_MODEL", "gemma-4-31b-it")
    )
    google_llm_max_tokens: int = field(
        default_factory=lambda: _env_int("GOOGLE_LLM_MAX_TOKENS", 512)
    )
    google_llm_timeout: float = field(default_factory=lambda: _env_float("GOOGLE_LLM_TIMEOUT", 45))
    google_api_key: str | None = field(
        default_factory=lambda: _first_env("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_LLM_API_KEY")
    )

    # --- Verhalten ---
    history_length: int = field(default_factory=lambda: _env_int("HISTORY_LENGTH", 16))
    # Idle ist NICHT mehr ein starrer Poll-Zyklus, sondern ereignisgesteuert:
    # Der Hintergrund-Task schläft bis (letzte_Aktivität + idle_threshold + Jitter)
    # und wird bei jeder echten Chat-Nachricht neu auf diesen Zeitpunkt gelegt.
    idle_threshold: int = field(default_factory=lambda: _env_int("IDLE_THRESHOLD", 900))
    idle_jitter: int = field(default_factory=lambda: _env_int("IDLE_JITTER", 90))
    idle_max_solo_messages: int = field(
        default_factory=lambda: _env_int("IDLE_MAX_SOLO_MESSAGES", 1)
    )
    context_ttl: int = field(default_factory=lambda: _env_int("CONTEXT_TTL", 120))
    max_message_length: int = 480  # Twitch-Limit ist 500; etwas Puffer.
    user_memory_dir: str = field(
        default_factory=lambda: os.getenv("USER_MEMORY_DIR", "user_memories")
    )
    user_memory_enabled: bool = field(
        default_factory=lambda: _env_bool("USER_MEMORY_ENABLED", True)
    )

    # --- Reichhaltige Chatter-Profile (Auto-Summary) ---
    # Ab PROFILE_SUMMARY_AFTER echten Interaktionen wird erstmals ein kompaktes
    # Profil (Fakten + Gesprächsstil) erstellt, danach alle
    # PROFILE_SUMMARY_INTERVAL Interaktionen aufgefrischt. 0 deaktiviert die
    # automatische Zusammenfassung (dann wächst das Profil nur über explizite
    # "merk dir / remember"-Signale).
    profile_summary_after: int = field(default_factory=lambda: _env_int("PROFILE_SUMMARY_AFTER", 2))
    profile_summary_interval: int = field(
        default_factory=lambda: _env_int("PROFILE_SUMMARY_INTERVAL", 5)
    )
    profile_interactions_kept: int = field(
        default_factory=lambda: _env_int("PROFILE_INTERACTIONS_KEPT", 8)
    )
    profile_max_notes: int = field(default_factory=lambda: _env_int("PROFILE_MAX_NOTES", 10))

    def _normalized_google_model(self) -> str:
        """Normalisiert häufige Vertipper/Aliase für das Google-Online-Profil.

        Korrekte IDs laut Google-Doku (Stand 2026-07-02):
        ``gemma-4-31b-it`` und ``gemma-4-26b-a4b-it``.
        """
        model = self.google_llm_model.strip()
        # Die API-Modell-ID heißt Gemma, auch wenn sie über die Gemini API läuft.
        # "a4b" = aktive Parameter bei MoE-Varianten (z. B. gemma-4-26b-a4b-it).
        aliases = {
            "gemini-4-31b-it": "gemma-4-31b-it",
            "google/gemma-4-31b-it": "gemma-4-31b-it",
            "gemini-4-26b-a4b": "gemma-4-26b-a4b-it",
            "google/gemma-4-26b-a4b": "gemma-4-26b-a4b-it",
            "gemma-4-26b-a4b": "gemma-4-26b-a4b-it",
            "gemma-4-26b-it-a4b": "gemma-4-26b-a4b-it",
            "gemma-4-26b-a4b-it-a4b": "gemma-4-26b-a4b-it",
            "google/gemma-4-26b-a4b-it": "gemma-4-26b-a4b-it",
        }
        return aliases.get(model.lower(), model)

    def apply_llm_backend(self, backend: str, *, online_model: str | None = None) -> None:
        """Aktiviert zur Laufzeit das lokale oder Google/Gemma-LLM-Profil.

        ``online_model`` überschreibt das Google-Modell (z. B. ``gemma-4-26b-a4b``
        als Alternative zum Default ``gemma-4-31b-it``) und wird normalisiert.
        """
        normalized = backend.strip().lower()
        if normalized in ("1", "l", "local", "lokal", "llama", "llama-server"):
            self.llm_backend = "local"
            self.llm_backend_label = f"lokaler llama-server ({self.llm_model} @ {self.llm_url})"
            return

        if normalized in (
            "2",
            "3",
            "o",
            "online",
            "google",
            "gemini",
            "gemma",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b",
            "gemma-4-26b-a4b-it",
        ):
            api_key = self.google_api_key or self.llm_api_key
            if not api_key:
                raise RuntimeError(
                    "Online-LLM gewählt, aber kein Google API Key gefunden. "
                    "Setze GOOGLE_API_KEY oder GEMINI_API_KEY in deiner .env."
                )
            # Menu/Override: explizites Modell schlägt den Default aus der .env.
            if online_model:
                self.google_llm_model = online_model
            # Menu-Shortcut „3"/Alternative -> MoE-Variante, außer online_model
            # wurde schon anders gesetzt.
            elif normalized == "3":
                self.google_llm_model = "gemma-4-26b-a4b-it"
            self.llm_backend = "online"
            self.google_llm_model = self._normalized_google_model()
            self.llm_backend_label = f"Google Gemini API ({self.google_llm_model})"
            # Existing .env files may still contain the former OpenAI shim URL.
            # Migrate it automatically instead of appending a broken native path.
            self.llm_url = _google_native_base_url(self.google_llm_url)
            self.llm_model = self.google_llm_model
            self.llm_api_key = api_key
            # Gemma 4 can spend a few hundred tokens on native thought parts;
            # old .env files often still contain 120/200 and would truncate the
            # answer before the first visible text part.
            self.llm_max_tokens = max(512, self.google_llm_max_tokens)
            self.llm_timeout = self.google_llm_timeout
            self.llm_send_repeat_penalty = False
            self.llm_send_llama_extras = False
            # Gemma 4 unterstützt System Instructions via generateContent.
            self.llm_use_system_role = _env_bool("LLM_USE_SYSTEM_ROLE", True)
            # Google nutzt den nativen generateContent-Transport, nicht die
            # OpenAI-Kompatibilitätsschicht (die beim MoE-Modell HTTP 500 liefert).
            self.llm_transport = "google_native"
            return

        raise ValueError("LLM_BACKEND muss 'ask', 'local' oder 'online' sein.")


settings = Settings()
