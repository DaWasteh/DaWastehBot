"""Tests for cli_backends.py: command building, response parsing, SSH, models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli_backends as cb

# --------------------------------------------------------------------------- #
#  Prompt packing
# --------------------------------------------------------------------------- #


class TestPackPrompt:
    def test_includes_system_prompt(self) -> None:
        packed = cb.pack_prompt("Du bist PandaBot.", [("user", "Hallo!")])
        assert "Du bist PandaBot." in packed

    def test_includes_conversation(self) -> None:
        packed = cb.pack_prompt("System", [("user", "Alice: hi"), ("assistant", "Servus!")])
        assert "Alice: hi" in packed
        assert "Servus!" in packed
        assert "Assistant: Servus!" in packed

    def test_includes_response_instruction(self) -> None:
        packed = cb.pack_prompt("S", [("user", "hi")])
        assert "1-3 short sentences" in packed

    def test_empty_turns(self) -> None:
        packed = cb.pack_prompt("System", [])
        assert "System" in packed


# --------------------------------------------------------------------------- #
#  Response parsers
# --------------------------------------------------------------------------- #


class TestParseClaudeResponse:
    def test_valid_json(self) -> None:
        raw = json.dumps({"result": "Hallo!", "other": "x"})
        assert cb.parse_claude_response(raw) == "Hallo!"

    def test_plain_text_fallback(self) -> None:
        assert cb.parse_claude_response("Nur Text") == "Nur Text"

    def test_empty(self) -> None:
        assert cb.parse_claude_response("") is None

    def test_empty_result(self) -> None:
        raw = json.dumps({"result": ""})
        assert cb.parse_claude_response(raw) is None


class TestParseCodexResponse:
    def test_jsonl_with_message(self) -> None:
        lines = [
            json.dumps({"type": "start"}),
            json.dumps({"type": "result", "message": "Final answer!"}),
        ]
        assert cb.parse_codex_response("\n".join(lines)) == "Final answer!"

    def test_jsonl_with_content_blocks(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": [
                        {"type": "text", "text": "Part 1 "},
                        {"type": "text", "text": "Part 2"},
                    ],
                }
            ),
        ]
        assert cb.parse_codex_response("\n".join(lines)) == "Part 1 Part 2"

    def test_nested_agent_message(self) -> None:
        raw = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Nested final answer"},
            }
        )
        assert cb.parse_codex_response(raw) == "Nested final answer"

    def test_empty(self) -> None:
        assert cb.parse_codex_response("") is None

    def test_only_invalid_lines(self) -> None:
        assert cb.parse_codex_response("not json\nalso not json") is None


class TestParseGeminiResponse:
    def test_valid_json(self) -> None:
        raw = json.dumps({"response": "Hallo von Gemini!"})
        assert cb.parse_gemini_response(raw) == "Hallo von Gemini!"

    def test_plain_text(self) -> None:
        assert cb.parse_gemini_response("Just text") == "Just text"

    def test_empty(self) -> None:
        assert cb.parse_gemini_response("") is None


class TestParseCopilotResponse:
    def test_plain_text(self) -> None:
        assert cb.parse_copilot_response("  Hello there!  ") == "Hello there!"

    def test_empty(self) -> None:
        assert cb.parse_copilot_response("") is None


class TestParseResponseForTransport:
    def test_dispatches(self) -> None:
        assert cb.parse_response_for_transport("claude_cli", '{"result": "hi"}') == "hi"
        assert cb.parse_response_for_transport("copilot_cli", "text") == "text"

    def test_unknown_transport_raises(self) -> None:
        with pytest.raises(ValueError, match="Unbekannter CLI-Transport"):
            cb.parse_response_for_transport("unknown", "x")


# --------------------------------------------------------------------------- #
#  Model-list parsers
# --------------------------------------------------------------------------- #


class TestParseOpenAIModels:
    def test_standard(self) -> None:
        raw = json.dumps(
            {
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "gpt-4o-mini"},
                ]
            }
        )
        assert cb.parse_openai_models(raw) == ["gpt-4o", "gpt-4o-mini"]

    def test_empty(self) -> None:
        assert cb.parse_openai_models(json.dumps({"data": []})) == []

    def test_invalid_json(self) -> None:
        assert cb.parse_openai_models("not json") == []


class TestParseGoogleModels:
    def test_filters_generate_content(self) -> None:
        raw = json.dumps(
            {
                "models": [
                    {
                        "name": "models/gemma-4-31b-it",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            }
        )
        result = cb.parse_google_models(raw)
        assert result == ["gemma-4-31b-it"]

    def test_strips_models_prefix(self) -> None:
        raw = json.dumps(
            {
                "models": [
                    {
                        "name": "models/gemma-4-26b-a4b-it",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert cb.parse_google_models(raw) == ["gemma-4-26b-a4b-it"]

    def test_invalid_json(self) -> None:
        assert cb.parse_google_models("xxx") == []


class TestParseCodexModelCatalog:
    def test_extracts_model_ids(self) -> None:
        raw = json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "label": "GPT 5.5"},
                    {"id": "o4-mini", "label": "O4 Mini"},
                ]
            }
        )
        result = cb.parse_codex_model_catalog(raw)
        assert "gpt-5.5" in result
        assert "o4-mini" in result

    def test_invalid_json(self) -> None:
        assert cb.parse_codex_model_catalog("xxx") == []


# --------------------------------------------------------------------------- #
#  Models-request builder
# --------------------------------------------------------------------------- #


class TestBuildModelsRequest:
    def test_openai(self) -> None:
        url, headers = cb.build_models_request(
            "https://api.openai.com/v1/chat/completions", "sk-test", "openai"
        )
        assert url == "https://api.openai.com/v1/models"
        assert headers == {"Authorization": "Bearer sk-test"}

    def test_google_native(self) -> None:
        url, headers = cb.build_models_request(
            "https://generativelanguage.googleapis.com/v1beta", "AIza-test", "google_native"
        )
        assert url == "https://generativelanguage.googleapis.com/v1beta/models"
        assert headers == {"x-goog-api-key": "AIza-test"}

    def test_openai_no_key(self) -> None:
        url, headers = cb.build_models_request(
            "http://localhost:1235/v1/chat/completions", "", "openai"
        )
        assert headers == {}


# --------------------------------------------------------------------------- #
#  SSH tunnel
# --------------------------------------------------------------------------- #


class TestSSHTunnel:
    def test_validate_ok(self) -> None:
        cfg = cb.SSHTunnelConfig(
            host="example.com", user="root", local_port=8080, remote_port=11434
        )
        assert cfg.validate() is None

    def test_validate_missing_host(self) -> None:
        cfg = cb.SSHTunnelConfig(user="root")
        assert "Host" in (cfg.validate() or "")

    def test_validate_bad_port(self) -> None:
        cfg = cb.SSHTunnelConfig(host="h", user="u", local_port=0)
        assert "Port" in (cfg.validate() or "")

    def test_local_endpoint(self) -> None:
        cfg = cb.SSHTunnelConfig(local_port=9090)
        assert cfg.local_endpoint() == "http://127.0.0.1:9090/v1/chat/completions"

    def test_build_command_structure(self) -> None:
        """Build a tunnel command (ssh must be available on CI)."""
        cfg = cb.SSHTunnelConfig(
            host="example.com",
            user="bot",
            ssh_port=2222,
            identity_file="/home/user/.ssh/id_ed25519",
            local_port=8080,
            remote_host="127.0.0.1",
            remote_port=11434,
        )
        try:
            args = cb.build_ssh_tunnel_command(cfg)
        except FileNotFoundError:
            pytest.skip("ssh not available on this system")

        assert "ssh" in args[0].lower()
        assert "-N" in args
        assert "127.0.0.1:8080:127.0.0.1:11434" in args
        assert "-p" in args
        assert "2222" in args
        assert "-i" in args
        assert "/home/user/.ssh/id_ed25519" in args
        assert "bot@example.com" in args
        assert any(
            "BatchMode=yes" in a or "BatchMode=yes" == a.split("=")[0] + "=" + a.split("=")[1]
            for a in args
            if "=" in a
        )
        # ExitOnForwardFailure=yes should be in the args
        assert any("ExitOnForwardFailure=yes" == a for a in args)

    def test_build_command_validation_error(self) -> None:
        cfg = cb.SSHTunnelConfig(host="", user="")  # invalid
        with pytest.raises(ValueError):
            cb.build_ssh_tunnel_command(cfg)


# --------------------------------------------------------------------------- #
#  CLI command builders (mock shutil.which since CLIs may not be installed)
# --------------------------------------------------------------------------- #


class TestCommandBuilders:
    @pytest.fixture(autouse=True)
    def mock_clis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock CLI executables so command builders work without real installs."""
        monkeypatch.setattr(cb, "find_cli", lambda name: f"/usr/bin/{name}")

    def test_claude_command(self) -> None:
        args = cb.build_claude_command("sonnet")
        assert "--bare" in args
        assert "-p" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--model" in args
        assert "sonnet" in args
        assert "--tools" in args
        assert "" in args  # empty tools string

    def test_codex_command(self) -> None:
        args = cb.build_codex_command("gpt-5.5")
        assert "exec" in args
        assert "--sandbox" in args
        assert "read-only" in args
        assert "--skip-git-repo-check" in args
        assert "--json" in args
        assert args[-1] == "-"  # Prompt kommt über stdin

    def test_gemini_command(self) -> None:
        args = cb.build_gemini_command("auto")
        assert "-p" not in args  # Prompt kommt über stdin, nie als argv
        assert "--output-format" in args
        assert "json" in args
        assert "--sandbox" in args
        assert "plan" in args

    def test_copilot_command(self) -> None:
        args = cb.build_copilot_command("gpt-5.5")
        assert args[-1] == "-p"  # run_cli appends the prompt value
        assert "--model" in args
        assert "--deny-tool=write" in args
        assert "--deny-tool=shell" in args

    def test_dispatch_build_command(self) -> None:
        for transport in ("claude_cli", "codex_cli", "gemini_cli", "copilot_cli"):
            args = cb.build_command_for_transport(transport, "test-model")
            assert isinstance(args, list)
            assert len(args) > 2

    def test_dispatch_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            cb.build_command_for_transport("unknown_cli", "x")

    def test_stdin_transports(self) -> None:
        """Chat-Prompts gehen bei diesen CLIs über stdin, nie über argv."""
        assert cb.STDIN_PROMPT_TRANSPORTS == {"claude_cli", "codex_cli", "gemini_cli"}


class TestArgvSafePrompt:
    def test_exe_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb.sys, "platform", "win32")
        assert cb._argv_safe_prompt("C:\\bin\\copilot.exe", 'a & b | "c" %x%') == 'a & b | "c" %x%'

    def test_cmd_shim_strips_metachars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb.sys, "platform", "win32")
        result = cb._argv_safe_prompt("C:\\npm\\copilot.CMD", 'hi & del *.* | "x" %PATH% ^!')
        for ch in '&|"%^!<>':
            assert ch not in result
        assert "hi" in result

    def test_cmd_shim_folds_newlines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb.sys, "platform", "win32")
        result = cb._argv_safe_prompt("copilot.cmd", "Zeile1\r\nZeile2")
        assert "\n" not in result and "\r" not in result
        assert "Zeile1" in result and "Zeile2" in result

    def test_non_windows_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb.sys, "platform", "linux")
        assert cb._argv_safe_prompt("/usr/bin/copilot", "a & b\nc") == "a & b\nc"


# --------------------------------------------------------------------------- #
#  Login command builder
# --------------------------------------------------------------------------- #


class TestLoginCommand:
    @pytest.fixture(autouse=True)
    def mock_clis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb, "find_cli", lambda name: f"/usr/bin/{name}")

    def test_returns_title_and_args(self) -> None:
        title, args = cb.build_login_command("claude_cli")
        assert title == "Claude Code"
        assert args[0] == "/usr/bin/claude"

    def test_codex_uses_official_login_subcommand(self) -> None:
        _title, args = cb.build_login_command("codex_cli")
        assert args == ["/usr/bin/codex", "login"]

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            cb.build_login_command("unknown")

    def test_missing_cli_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cb, "find_cli", lambda name: None)
        with pytest.raises(FileNotFoundError):
            cb.build_login_command("codex_cli")


# --------------------------------------------------------------------------- #
#  CLI discovery
# --------------------------------------------------------------------------- #


class TestCLIDiscovery:
    def test_windows_prefers_real_npm_binary_over_editor_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appdata = tmp_path / "AppData" / "Roaming"
        npm_dir = appdata / "npm"
        npm_dir.mkdir(parents=True)
        real = npm_dir / "copilot.cmd"
        real.write_text("@echo off\n", encoding="utf-8")
        wrapper = tmp_path / "github.copilot-chat" / "copilotCli" / "copilot"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("wrapper", encoding="utf-8")
        monkeypatch.setattr(cb.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(cb.shutil, "which", lambda _name: str(wrapper))

        assert cb.find_cli("copilot") == str(real)

    def test_windows_ignores_vscode_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper = tmp_path / "github.copilot-chat" / "copilotCli" / "copilot"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("wrapper", encoding="utf-8")
        monkeypatch.setattr(cb.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path / "EmptyAppData"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "EmptyLocal"))
        monkeypatch.setattr(cb.shutil, "which", lambda _name: str(wrapper))

        assert cb.find_cli("copilot") is None


# --------------------------------------------------------------------------- #
#  CLI registry
# --------------------------------------------------------------------------- #


class TestCLIRegistry:
    def test_all_transports_present(self) -> None:
        for t in cb.ALL_CLI_TRANSPORTS:
            assert t in cb.CLI_REGISTRY

    def test_login_hints_exist(self) -> None:
        for desc in cb.CLI_REGISTRY.values():
            assert desc.login_hint
            assert desc.executable
            assert desc.npm_package.startswith("@")


# --------------------------------------------------------------------------- #
#  v1.2: Parser-Härtung gegen Nicht-Dict-JSON
# --------------------------------------------------------------------------- #


class TestParserHardening:
    def test_claude_response_non_dict_json(self) -> None:
        assert cb.parse_claude_response("[1, 2, 3]") is None
        assert cb.parse_claude_response('"nur ein string"') is None

    def test_gemini_response_non_dict_json(self) -> None:
        assert cb.parse_gemini_response("[]") is None
        assert cb.parse_gemini_response("42") is None

    def test_codex_response_ignores_non_dict_lines(self) -> None:
        raw = '[1,2]\n"text"\n{"type": "message", "content": "Servus!"}'
        assert cb.parse_codex_response(raw) == "Servus!"

    def test_openai_models_non_dict_entries(self) -> None:
        assert cb.parse_openai_models("[1, 2]") == []
        assert cb.parse_openai_models('{"data": ["nur-string", {"id": "m1"}]}') == ["m1"]

    def test_google_models_non_dict_entries(self) -> None:
        assert cb.parse_google_models('["x"]') == []
        raw = json.dumps(
            {
                "models": [
                    "kaputt",
                    {
                        "name": "models/gemma-4-31b-it",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert cb.parse_google_models(raw) == ["gemma-4-31b-it"]

    def test_claude_response_error_payload_is_dropped(self) -> None:
        raw = json.dumps(
            {"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}
        )
        assert cb.parse_claude_response(raw) is None
