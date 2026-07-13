"""Tests for llm_profiles.py: profile store, normalisation, control file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_profiles import (
    GOOGLE_MODEL_ALIASES,
    PROVIDERS,
    LLMProfile,
    ProfileStore,
    normalize_model_id,
    read_control_file,
    resolve_control_api_key,
    write_control_file,
)

# --------------------------------------------------------------------------- #
#  Model-ID normalisation
# --------------------------------------------------------------------------- #


class TestNormalizeModelId:
    def test_strips_models_prefix(self) -> None:
        assert normalize_model_id("models/gemma-4-31b-it") == "gemma-4-31b-it"

    def test_google_alias_a4b(self) -> None:
        assert (
            normalize_model_id("gemma-4-26b-a4b", provider="google_native") == "gemma-4-26b-a4b-it"
        )

    def test_google_alias_gemini_typo(self) -> None:
        assert normalize_model_id("gemini-4-31b-it", provider="google") == "gemma-4-31b-it"

    def test_non_google_unchanged(self) -> None:
        assert normalize_model_id("gpt-4o", provider="openai") == "gpt-4o"

    def test_empty_strips(self) -> None:
        assert normalize_model_id("  gpt-4o  ") == "gpt-4o"

    def test_known_aliases_all_normalize(self) -> None:
        for alias, expected in GOOGLE_MODEL_ALIASES.items():
            assert normalize_model_id(alias, provider="google_native") == expected


# --------------------------------------------------------------------------- #
#  Profile store
# --------------------------------------------------------------------------- #


class TestProfileStore:
    def test_load_empty(self, tmp_path: Path) -> None:
        store = ProfileStore.load(tmp_path / "nonexistent.json")
        assert store.profiles == {}
        assert store.active == ""

    def test_save_and_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.json"
        store = ProfileStore(path=path)
        prof = LLMProfile(
            name="Test",
            provider="openai",
            api_key="sk-test",
            endpoint="https://api.openai.com/v1/chat/completions",
            model="gpt-4o",
        )
        store.upsert(prof)
        store.active = "Test"
        store.save()

        # File should exist with restricted perms
        assert path.exists()

        reloaded = ProfileStore.load(path)
        assert "Test" in reloaded.profiles
        assert reloaded.active == "Test"
        assert reloaded.profiles["Test"].api_key == "sk-test"
        assert reloaded.profiles["Test"].model == "gpt-4o"

    def test_delete(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.json"
        store = ProfileStore(path=path)
        store.upsert(LLMProfile(name="A", provider="custom"))
        store.upsert(LLMProfile(name="B", provider="custom"))
        store.active = "A"
        store.delete("A")
        assert "A" not in store.profiles
        assert store.active == ""  # active cleared
        assert "B" in store.profiles

    def test_get_active(self, tmp_path: Path) -> None:
        store = ProfileStore(path=tmp_path / "x.json")
        store.upsert(LLMProfile(name="X", provider="custom"))
        store.active = "X"
        active = store.get_active()
        assert active is not None
        assert active.name == "X"

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        store = ProfileStore.load(path)
        assert store.profiles == {}

    def test_names_sorted(self, tmp_path: Path) -> None:
        store = ProfileStore(path=tmp_path / "x.json")
        store.upsert(LLMProfile(name="Zeta", provider="custom"))
        store.upsert(LLMProfile(name="Alpha", provider="custom"))
        assert store.names() == ["Alpha", "Zeta"]

    def test_load_stale_active_cleared(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.json"
        path.write_text(json.dumps({"active": "Ghost", "profiles": {}}), encoding="utf-8")
        store = ProfileStore.load(path)
        assert store.active == ""


# --------------------------------------------------------------------------- #
#  LLMProfile
# --------------------------------------------------------------------------- #


class TestLLMProfile:
    def test_from_preset_custom(self) -> None:
        prof = LLMProfile.from_preset("My", "custom")
        assert prof.provider == "custom"
        assert prof.endpoint == ""  # custom has no base_url
        assert prof.max_tokens == 300

    def test_from_preset_google(self) -> None:
        prof = LLMProfile.from_preset("Google", "google_native")
        assert prof.provider == "google_native"
        assert "v1beta" in prof.endpoint
        assert prof.use_system_role is True

    def test_resolve_api_key_stored(self) -> None:
        prof = LLMProfile(name="X", provider="custom", api_key="key123")
        assert prof.resolve_api_key() == "key123"

    def test_resolve_api_key_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        prof = LLMProfile(name="X", provider="openai", api_key="")
        assert prof.resolve_api_key() == "env-key"

    def test_resolve_api_key_dotenv_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('OPENAI_API_KEY="dotenv-key"\n', encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("PANDABOT_ENV_FILE", str(env_file))
        prof = LLMProfile(name="X", provider="openai", api_key="")
        assert prof.resolve_api_key() == "dotenv-key"

    def test_old_google_openai_endpoint_is_migrated(self) -> None:
        prof = LLMProfile(
            name="Google",
            provider="google_native",
            endpoint=("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        )
        assert prof.effective_endpoint() == "https://generativelanguage.googleapis.com/v1beta"

    def test_transport(self) -> None:
        prof = LLMProfile(name="X", provider="google_native")
        assert prof.transport() == "google_native"

    def test_to_dict_normalizes_model(self) -> None:
        prof = LLMProfile(name="X", provider="google_native", model="gemma-4-26b-a4b")
        d = prof.to_dict()
        assert d["model"] == "gemma-4-26b-a4b-it"

    def test_from_dict_filters_unknown_keys(self) -> None:
        prof = LLMProfile.from_dict(
            {
                "name": "X",
                "provider": "custom",
                "unknown_field": "ignored",
            }
        )
        assert prof.name == "X"


# --------------------------------------------------------------------------- #
#  Control file
# --------------------------------------------------------------------------- #


class TestControlFile:
    def test_write_and_read(self, tmp_path: Path) -> None:
        path = tmp_path / "control.json"
        prof = LLMProfile(
            name="Online",
            provider="google_native",
            api_key="secret",
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            model="gemma-4-31b-it",
            max_tokens=512,
        )
        write_control_file(prof, path)
        data = read_control_file(path)
        assert data is not None
        assert data["transport"] == "google_native"
        assert data["model"] == "gemma-4-31b-it"
        # Secrets gehören NICHT in die Control-Datei.
        assert "secret" not in path.read_text(encoding="utf-8")
        assert not data.get("api_key")

    def test_resolve_key_from_profile_store(self, tmp_path: Path) -> None:
        """Der Bot löst den Key über llm_profiles.json auf, nicht aus der Control-Datei."""
        profiles_path = tmp_path / "llm_profiles.json"
        store = ProfileStore(path=profiles_path)
        store.upsert(LLMProfile(name="Online", provider="openai", api_key="sk-stored"))
        store.save()
        data = {"profile_name": "Online", "provider": "openai"}
        assert resolve_control_api_key(data, profiles_path) == "sk-stored"

    def test_resolve_key_env_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZAI_API_KEY", "zai-env-key")
        monkeypatch.setenv("PANDABOT_ENV_FILE", str(tmp_path / "no.env"))
        data = {"profile_name": "Unbekannt", "provider": "zai"}
        assert resolve_control_api_key(data, tmp_path / "missing.json") == "zai-env-key"

    def test_resolve_key_legacy_inline(self, tmp_path: Path) -> None:
        """Alt-Format mit Key in der Control-Datei bleibt lesbar."""
        data = {"profile_name": "X", "provider": "openai", "api_key": "inline"}
        assert resolve_control_api_key(data, tmp_path / "missing.json") == "inline"

    def test_read_missing(self, tmp_path: Path) -> None:
        assert read_control_file(tmp_path / "missing.json") is None

    def test_read_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("xxx", encoding="utf-8")
        assert read_control_file(path) is None


# --------------------------------------------------------------------------- #
#  Provider presets
# --------------------------------------------------------------------------- #


class TestProviderPresets:
    def test_all_providers_have_unique_ids(self) -> None:
        ids = [p.id for p in PROVIDERS.values()]
        assert len(ids) == len(set(ids))

    def test_google_models_url(self) -> None:
        p = PROVIDERS["google_native"]
        assert p.models_url() == "https://generativelanguage.googleapis.com/v1beta/models"

    def test_openai_models_url(self) -> None:
        p = PROVIDERS["openai"]
        assert p.models_url() == "https://api.openai.com/v1/models"

    def test_custom_models_url_empty_base(self) -> None:
        p = PROVIDERS["custom"]
        # Empty base_url -> just /models appended
        assert p.models_url() == "/models"

    def test_cli_presets_exist(self) -> None:
        for cid in ("claude_cli", "codex_cli", "gemini_cli", "copilot_cli"):
            assert cid in PROVIDERS
            assert PROVIDERS[cid].transport.endswith("_cli")

    def test_zai_presets(self) -> None:
        api = PROVIDERS["zai"]
        assert api.transport == "openai"
        assert api.base_url == "https://api.z.ai/api/paas/v4/chat/completions"
        assert "ZAI_API_KEY" in api.key_env_names
        assert "glm-4.7" in api.model_suggestions

        coding = PROVIDERS["zai_coding"]
        assert coding.transport == "openai"
        assert coding.base_url == "https://api.z.ai/api/coding/paas/v4/chat/completions"
        assert "ZAI_API_KEY" in coding.key_env_names

    def test_zai_models_url(self) -> None:
        assert PROVIDERS["zai"].models_url() == "https://api.z.ai/api/paas/v4/models"
