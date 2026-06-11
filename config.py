"""Zentrale Konfiguration für PandaBot.

Liest Werte aus Umgebungsvariablen bzw. einer ``.env``-Datei. So bleiben
Secrets (Client-ID/Secret) aus dem Code heraus und müssen nicht versioniert
werden. Kopiere ``.env.example`` zu ``.env`` und trage deine Werte ein.
"""

from __future__ import annotations

import os
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


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


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
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.8"))
    )
    llm_top_p: float = field(default_factory=lambda: float(os.getenv("LLM_TOP_P", "0.95")))
    llm_repeat_penalty: float = field(
        default_factory=lambda: float(os.getenv("LLM_REPEAT_PENALTY", "1.15"))
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
    llm_max_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS", "80")))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "20")))

    # --- Google/Gemma Online-Profil ---
    google_llm_url: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_LLM_SERVER_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
    )
    google_llm_model: str = field(
        default_factory=lambda: os.getenv("GOOGLE_LLM_MODEL", "gemma-4-31b-it")
    )
    google_llm_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("GOOGLE_LLM_MAX_TOKENS", "120"))
    )
    google_llm_timeout: float = field(
        default_factory=lambda: float(os.getenv("GOOGLE_LLM_TIMEOUT", "30"))
    )
    google_api_key: str | None = field(
        default_factory=lambda: _first_env("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_LLM_API_KEY")
    )

    # --- Verhalten ---
    history_length: int = field(default_factory=lambda: int(os.getenv("HISTORY_LENGTH", "12")))
    idle_threshold: int = field(default_factory=lambda: int(os.getenv("IDLE_THRESHOLD", "900")))
    idle_max_solo_messages: int = field(
        default_factory=lambda: int(os.getenv("IDLE_MAX_SOLO_MESSAGES", "1"))
    )
    context_ttl: int = field(default_factory=lambda: int(os.getenv("CONTEXT_TTL", "120")))
    max_message_length: int = 480  # Twitch-Limit ist 500; etwas Puffer.
    user_memory_dir: str = field(
        default_factory=lambda: os.getenv("USER_MEMORY_DIR", "user_memories")
    )
    user_memory_enabled: bool = field(
        default_factory=lambda: _env_bool("USER_MEMORY_ENABLED", True)
    )

    def _normalized_google_model(self) -> str:
        """Normalisiert häufige Vertipper/Aliase für das Google-Online-Profil."""
        model = self.google_llm_model.strip()
        aliases = {
            # Die API-Modell-ID heißt Gemma, auch wenn sie über die Gemini API läuft.
            "gemini-4-31b-it": "gemma-4-31b-it",
            "google/gemma-4-31b-it": "gemma-4-31b-it",
        }
        return aliases.get(model.lower(), model)

    def apply_llm_backend(self, backend: str) -> None:
        """Aktiviert zur Laufzeit das lokale oder Google/Gemma-LLM-Profil."""
        normalized = backend.strip().lower()
        if normalized in ("1", "l", "local", "lokal", "llama", "llama-server"):
            self.llm_backend = "local"
            self.llm_backend_label = f"lokaler llama-server ({self.llm_model} @ {self.llm_url})"
            return

        if normalized in ("2", "o", "online", "google", "gemini", "gemma"):
            api_key = self.google_api_key or self.llm_api_key
            if not api_key:
                raise RuntimeError(
                    "Online-LLM gewählt, aber kein Google API Key gefunden. "
                    "Setze GOOGLE_API_KEY oder GEMINI_API_KEY in deiner .env."
                )
            self.llm_backend = "online"
            self.google_llm_model = self._normalized_google_model()
            self.llm_backend_label = f"Google Gemini API ({self.google_llm_model})"
            self.llm_url = self.google_llm_url
            self.llm_model = self.google_llm_model
            self.llm_api_key = api_key
            self.llm_max_tokens = self.google_llm_max_tokens
            self.llm_timeout = self.google_llm_timeout
            self.llm_send_repeat_penalty = False
            self.llm_send_llama_extras = False
            return

        raise ValueError("LLM_BACKEND muss 'ask', 'local' oder 'online' sein.")


settings = Settings()
