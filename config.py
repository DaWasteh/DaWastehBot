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


@dataclass(frozen=True)
class Settings:
    # --- Twitch / App ---
    client_id: str = field(default_factory=lambda: _require("TWITCH_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _require("TWITCH_CLIENT_SECRET"))
    bot_id: str = field(default_factory=lambda: _require("TWITCH_BOT_ID"))
    owner_id: str = field(default_factory=lambda: _require("TWITCH_OWNER_ID"))

    channel_name: str = field(default_factory=lambda: os.getenv("TWITCH_CHANNEL", "dawasteh"))
    bot_name: str = field(default_factory=lambda: os.getenv("TWITCH_BOT_NAME", "PandaBot"))

    # --- LLM (llama-server, OpenAI-kompatibel) ---
    llm_url: str = field(
        default_factory=lambda: os.getenv(
            "LLM_SERVER_URL", "http://127.0.0.1:1235/v1/chat/completions"
        )
    )
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "local-model"))
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.7"))
    )
    llm_top_p: float = field(default_factory=lambda: float(os.getenv("LLM_TOP_P", "0.9")))
    llm_repeat_penalty: float = field(
        default_factory=lambda: float(os.getenv("LLM_REPEAT_PENALTY", "1.15"))
    )
    # repeat_penalty ist ein llama.cpp-Extra (nicht Teil der OpenAI-Spec).
    # Bei anderen Backends (z. B. vLLM) heißt der Parameter anders bzw. wird
    # nur über extra_body akzeptiert; auf "false" setzen, um ihn wegzulassen.
    llm_send_repeat_penalty: bool = field(
        default_factory=lambda: (
            os.getenv("LLM_SEND_REPEAT_PENALTY", "true").lower() not in ("0", "false", "no")
        )
    )
    llm_max_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS", "80")))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "20")))

    # --- Verhalten ---
    history_length: int = field(default_factory=lambda: int(os.getenv("HISTORY_LENGTH", "12")))
    idle_threshold: int = field(default_factory=lambda: int(os.getenv("IDLE_THRESHOLD", "300")))
    context_ttl: int = field(default_factory=lambda: int(os.getenv("CONTEXT_TTL", "120")))
    max_message_length: int = 480  # Twitch-Limit ist 500; etwas Puffer.
    user_memory_dir: str = field(default_factory=lambda: os.getenv("USER_MEMORY_DIR", "user_memories"))
    user_memory_enabled: bool = field(
        default_factory=lambda: (
            os.getenv("USER_MEMORY_ENABLED", "true").lower() not in ("0", "false", "no")
        )
    )


settings = Settings()