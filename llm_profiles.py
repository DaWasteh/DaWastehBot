"""LLM profile management for PandaBot.

Provides provider presets, profile storage (atomic, restricted permissions),
model-ID normalisation and a control-file mechanism for runtime profile
switching without restarting the bot.

Design notes
------------
- Secrets live only in a gitignored local JSON file (``llm_profiles.json``),
  never in version control.  Environment variables from ``.env`` serve as a
  fallback for API keys.
- The control file (``.pandabot_llm_control.json``) is written atomically by
  the GUI and polled by the running bot before each LLM request so that an
  activated profile takes effect on the *next* request.
- Google models use the *native* ``generateContent`` transport (not the
  OpenAI-compatibility shim) because the MoE variant ``gemma-4-26b-a4b-it``
  currently returns HTTP 500 on the OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
#  Provider presets
# --------------------------------------------------------------------------- #

# Transport identifiers -- stored in profiles and control files.
TRANSPORT_OPENAI = "openai"
TRANSPORT_GOOGLE_NATIVE = "google_native"
# CLI transports use official coding-agent CLIs headless/read-only.
TRANSPORT_CLAUDE_CLI = "claude_cli"
TRANSPORT_CODEX_CLI = "codex_cli"
TRANSPORT_GEMINI_CLI = "gemini_cli"
TRANSPORT_COPILOT_CLI = "copilot_cli"

CLI_TRANSPORTS = frozenset(
    {TRANSPORT_CLAUDE_CLI, TRANSPORT_CODEX_CLI, TRANSPORT_GEMINI_CLI, TRANSPORT_COPILOT_CLI}
)


@dataclass(frozen=True)
class ProviderPreset:
    """Static defaults for a well-known LLM provider."""

    id: str
    label: str
    transport: str
    base_url: str
    supports_system_role: bool = True
    send_repeat_penalty: bool = False
    send_llama_extras: bool = False
    max_tokens_default: int = 512
    timeout_default: float = 30.0
    # Environment variable names consulted as API-key fallback.
    key_env_names: tuple[str, ...] = ()

    def models_url(self) -> str:
        """Endpoint for listing available models.

        For OpenAI-compatible providers this is ``{base}/models`` where *base*
        is the chat/completions URL with the last path segment removed.
        Google uses the native ``/v1beta/models`` list endpoint.
        """
        if self.transport == TRANSPORT_GOOGLE_NATIVE:
            # base_url already ends with /v1beta
            return self.base_url.rstrip("/") + "/models"
        # OpenAI-compatible: replace trailing /chat/completions with /models
        url = self.base_url.rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if url.endswith(suffix):
                return url[: -len(suffix)] + "/models"
        return url + "/models"

    def generate_url(self, model: str) -> str:
        """Full request URL for a single completion (Google native only)."""
        clean = model
        if clean.startswith("models/"):
            clean = clean[len("models/") :]
        return self.base_url.rstrip("/") + f"/models/{clean}:generateContent"


# Registry of built-in providers.  ``Custom`` lets the user point at any
# OpenAI-compatible endpoint.
PROVIDERS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
        ProviderPreset(
            id="local",
            label="Local llama.cpp",
            transport=TRANSPORT_OPENAI,
            base_url="http://127.0.0.1:1235/v1/chat/completions",
            supports_system_role=True,
            send_repeat_penalty=True,
            send_llama_extras=True,
            max_tokens_default=80,
            timeout_default=20.0,
        ),
        ProviderPreset(
            id="google_native",
            label="Google Gemini (native generateContent)",
            transport=TRANSPORT_GOOGLE_NATIVE,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            supports_system_role=True,
            send_repeat_penalty=False,
            send_llama_extras=False,
            max_tokens_default=1024,
            timeout_default=45.0,
            key_env_names=("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_LLM_API_KEY"),
        ),
        ProviderPreset(
            id="openai",
            label="OpenAI",
            transport=TRANSPORT_OPENAI,
            base_url="https://api.openai.com/v1/chat/completions",
            max_tokens_default=300,
            timeout_default=30.0,
            key_env_names=("OPENAI_API_KEY",),
        ),
        ProviderPreset(
            id="openrouter",
            label="OpenRouter",
            transport=TRANSPORT_OPENAI,
            base_url="https://openrouter.ai/api/v1/chat/completions",
            max_tokens_default=300,
            timeout_default=30.0,
            key_env_names=("OPENROUTER_API_KEY",),
        ),
        ProviderPreset(
            id="groq",
            label="Groq",
            transport=TRANSPORT_OPENAI,
            base_url="https://api.groq.com/openai/v1/chat/completions",
            max_tokens_default=300,
            timeout_default=30.0,
            key_env_names=("GROQ_API_KEY",),
        ),
        ProviderPreset(
            id="mistral",
            label="Mistral AI",
            transport=TRANSPORT_OPENAI,
            base_url="https://api.mistral.ai/v1/chat/completions",
            max_tokens_default=300,
            timeout_default=30.0,
            key_env_names=("MISTRAL_API_KEY",),
        ),
        ProviderPreset(
            id="xai",
            label="xAI (Grok)",
            transport=TRANSPORT_OPENAI,
            base_url="https://api.x.ai/v1/chat/completions",
            max_tokens_default=300,
            timeout_default=30.0,
            key_env_names=("XAI_API_KEY",),
        ),
        ProviderPreset(
            id="custom",
            label="Custom OpenAI-compatible",
            transport=TRANSPORT_OPENAI,
            base_url="",
            max_tokens_default=300,
            timeout_default=30.0,
        ),
        # --- CLI / Abo providers (official CLIs, headless, read-only) ---
        ProviderPreset(
            id="claude_cli",
            label="Claude Code CLI (Abo)",
            transport=TRANSPORT_CLAUDE_CLI,
            base_url="",
            supports_system_role=False,
            max_tokens_default=300,
            timeout_default=45.0,
        ),
        ProviderPreset(
            id="codex_cli",
            label="OpenAI Codex CLI (Abo)",
            transport=TRANSPORT_CODEX_CLI,
            base_url="",
            supports_system_role=False,
            max_tokens_default=300,
            timeout_default=45.0,
        ),
        ProviderPreset(
            id="gemini_cli",
            label="Google Gemini CLI (Abo)",
            transport=TRANSPORT_GEMINI_CLI,
            base_url="",
            supports_system_role=False,
            max_tokens_default=300,
            timeout_default=45.0,
        ),
        ProviderPreset(
            id="copilot_cli",
            label="GitHub Copilot CLI (Abo)",
            transport=TRANSPORT_COPILOT_CLI,
            base_url="",
            supports_system_role=False,
            max_tokens_default=300,
            timeout_default=45.0,
        ),
    )
}


# --------------------------------------------------------------------------- #
#  Model-ID normalisation
# --------------------------------------------------------------------------- #

# Known Google/Gemma model aliases.  The correct IDs per the official docs
# (2026-07-02) are ``gemma-4-31b-it`` and ``gemma-4-26b-a4b-it``.
GOOGLE_MODEL_ALIASES: dict[str, str] = {
    "gemini-4-31b-it": "gemma-4-31b-it",
    "google/gemma-4-31b-it": "gemma-4-31b-it",
    "gemma-4-26b-a4b": "gemma-4-26b-a4b-it",
    "gemini-4-26b-a4b": "gemma-4-26b-a4b-it",
    "gemini-4-26b-a4b-it": "gemma-4-26b-a4b-it",
    "google/gemma-4-26b-a4b": "gemma-4-26b-a4b-it",
    "google/gemma-4-26b-a4b-it": "gemma-4-26b-a4b-it",
    "gemma-4-26b-it-a4b": "gemma-4-26b-a4b-it",
    "gemma-4-26b-a4b-it-a4b": "gemma-4-26b-a4b-it",
}


def normalize_model_id(model: str, *, provider: str = "") -> str:
    """Normalise a model ID for requests.

    - Strips a ``models/`` prefix (Google list endpoint returns IDs like
      ``models/gemma-4-31b-it``).
    - Fixes common Gemma typos for the Google provider.
    """
    model = model.strip()
    # Strip Google list-endpoint prefix.
    if model.startswith("models/"):
        model = model[len("models/") :]
    if provider in ("google_native", "google"):
        model = GOOGLE_MODEL_ALIASES.get(model.lower(), model)
    return model


# --------------------------------------------------------------------------- #
#  Local .env fallback (the standalone GUI does not import config.py)
# --------------------------------------------------------------------------- #


def _dotenv_first(*names: str) -> str:
    """Read the first named value from the repo ``.env`` without logging it."""
    path = Path(os.getenv("PANDABOT_ENV_FILE", Path(__file__).resolve().parent / ".env"))
    if not path.is_file():
        return ""
    wanted = set(names)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return next((values[name] for name in names if values.get(name)), "")


# --------------------------------------------------------------------------- #
#  LLM profile dataclass
# --------------------------------------------------------------------------- #


@dataclass
class LLMProfile:
    """A complete, self-contained LLM configuration."""

    name: str = ""
    provider: str = "custom"
    api_key: str = ""
    endpoint: str = ""
    model: str = ""
    max_tokens: int = 512
    temperature: float = 0.8
    top_p: float = 0.95
    timeout: float = 30.0
    use_system_role: bool = True
    send_repeat_penalty: bool = False
    send_llama_extras: bool = False
    repeat_penalty: float = 1.15

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Normalise model ID on serialization so stale data self-heals.
        d["model"] = normalize_model_id(self.model, provider=self.provider)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMProfile:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        prof = cls(**filtered)
        prof.model = normalize_model_id(prof.model, provider=prof.provider)
        return prof

    @classmethod
    def from_preset(cls, name: str, provider_id: str) -> LLMProfile:
        """Create a profile pre-filled from a provider preset."""
        preset = PROVIDERS.get(provider_id, PROVIDERS["custom"])
        return cls(
            name=name,
            provider=preset.id,
            endpoint=preset.base_url,
            max_tokens=preset.max_tokens_default,
            timeout=preset.timeout_default,
            use_system_role=preset.supports_system_role,
            send_repeat_penalty=preset.send_repeat_penalty,
            send_llama_extras=preset.send_llama_extras,
            temperature=0.8,
            top_p=0.95,
            repeat_penalty=1.15,
        )

    def resolve_api_key(self) -> str:
        """Return the stored key, then environment or local ``.env`` fallback."""
        if self.api_key:
            return self.api_key
        preset = PROVIDERS.get(self.provider)
        if not preset:
            return ""
        for env_name in preset.key_env_names:
            val = os.getenv(env_name)
            if val:
                return val
        return _dotenv_first(*preset.key_env_names)

    def effective_endpoint(self) -> str:
        """The endpoint to use: profile override or provider default."""
        endpoint = self.endpoint or (
            PROVIDERS.get(self.provider).base_url if self.provider in PROVIDERS else ""
        )
        if self.transport() == TRANSPORT_GOOGLE_NATIVE:
            clean = endpoint.rstrip("/")
            for suffix in ("/openai/chat/completions", "/openai"):
                if clean.endswith(suffix):
                    return clean[: -len(suffix)]
        return endpoint

    def transport(self) -> str:
        """Transport type for this profile's provider."""
        preset = PROVIDERS.get(self.provider)
        return preset.transport if preset else TRANSPORT_OPENAI


# --------------------------------------------------------------------------- #
#  Profile store  (atomic, restricted-permission JSON)
# --------------------------------------------------------------------------- #

DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent / "llm_profiles.json"


def _atomic_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically with best-effort 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Best-effort restrictive permissions before the rename.
        _try_restrict_perms(Path(tmp_name))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # Apply again after rename (some OSes reset on replace).
    _try_restrict_perms(path)


def _try_restrict_perms(path: Path) -> None:
    """Set 0600 on POSIX; on Windows rely on per-user ACLs (no-op here)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows chmod is advisory only; acceptable.
        pass


@dataclass
class ProfileStore:
    """Persistent collection of LLM profiles plus the active selection."""

    path: Path = DEFAULT_PROFILES_PATH
    profiles: dict[str, LLMProfile] = field(default_factory=dict)
    active: str = ""

    # ----- load / save -------------------------------------------------- #

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PROFILES_PATH) -> ProfileStore:
        p = Path(path)
        store = cls(path=p)
        if not p.exists():
            return store
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return store
        for name, pdict in raw.get("profiles", {}).items():
            store.profiles[name] = LLMProfile.from_dict(pdict)
        store.active = raw.get("active", "")
        if store.active and store.active not in store.profiles:
            store.active = ""
        return store

    def save(self) -> None:
        data = {
            "active": self.active,
            "profiles": {name: prof.to_dict() for name, prof in self.profiles.items()},
        }
        _atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    # ----- CRUD --------------------------------------------------------- #

    def upsert(self, profile: LLMProfile) -> None:
        self.profiles[profile.name] = profile

    def delete(self, name: str) -> None:
        if name in self.profiles:
            del self.profiles[name]
        if self.active == name:
            self.active = ""

    def get_active(self) -> LLMProfile | None:
        return self.profiles.get(self.active)

    def names(self) -> list[str]:
        return sorted(self.profiles.keys())


# --------------------------------------------------------------------------- #
#  Control file (runtime profile switching without bot restart)
# --------------------------------------------------------------------------- #

DEFAULT_CONTROL_PATH = Path(__file__).resolve().parent / ".pandabot_llm_control.json"


def _control_dict(profile: LLMProfile) -> dict[str, Any]:
    """Serialise a profile for the control file (consumed by the bot)."""
    return {
        "profile_name": profile.name,
        "provider": profile.provider,
        "transport": profile.transport(),
        "endpoint": profile.effective_endpoint(),
        "model": normalize_model_id(profile.model, provider=profile.provider),
        "api_key": profile.resolve_api_key(),
        "max_tokens": profile.max_tokens,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "timeout": profile.timeout,
        "use_system_role": profile.use_system_role,
        "send_repeat_penalty": profile.send_repeat_penalty,
        "send_llama_extras": profile.send_llama_extras,
        "repeat_penalty": profile.repeat_penalty,
        "timestamp": time.time(),
    }


def write_control_file(profile: LLMProfile, path: Path | str = DEFAULT_CONTROL_PATH) -> None:
    """Atomically write the active profile for the running bot to pick up."""
    _atomic_write(Path(path), json.dumps(_control_dict(profile), indent=2) + "\n")


def read_control_file(path: Path | str = DEFAULT_CONTROL_PATH) -> dict[str, Any] | None:
    """Read the control file; returns ``None`` if missing or invalid."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
