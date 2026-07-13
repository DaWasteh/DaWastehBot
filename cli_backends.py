"""CLI backend integration for PandaBot.

Provides command building and response parsing for official coding-agent CLIs:

- Claude Code (``claude``)
- OpenAI Codex (``codex``)
- Google Gemini CLI (``gemini``)
- GitHub Copilot CLI (``copilot``)

Also provides SSH-tunnel command building and model-list parsing helpers.

**Security**: All CLI invocations use ``asyncio.create_subprocess_exec`` (never
``shell=True``).  For Twitch chat responses every CLI is called with maximum
read-only / tool-disabled flags so no autonomous file-write or shell actions
can occur.  API keys are never passed to CLI invocations -- the CLIs use their
own pre-authenticated session state.

This module contains pure logic (no GUI imports) so it can be unit-tested.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Transport identifiers (mirrors llm_profiles for self-containment)
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger("pandabot.cli")

T_CLAUDE = "claude_cli"
T_CODEX = "codex_cli"
T_GEMINI = "gemini_cli"
T_COPILOT = "copilot_cli"

ALL_CLI_TRANSPORTS = frozenset({T_CLAUDE, T_CODEX, T_GEMINI, T_COPILOT})

# Transports whose CLI reads the prompt from stdin. Chat text NEVER goes on the
# command line for these: on Windows npm installs .cmd shims, and cmd.exe can
# re-interpret argv metacharacters ("BatBadBut") -- with untrusted Twitch chat
# in the prompt that would be a command-injection vector.
STDIN_PROMPT_TRANSPORTS = frozenset({T_CLAUDE, T_CODEX, T_GEMINI})

# --------------------------------------------------------------------------- #
#  Static model suggestions for dropdowns (always editable in the GUI)
# --------------------------------------------------------------------------- #
CLAUDE_ALIASES = ["sonnet", "opus", "haiku", "fable"]
GEMINI_CLI_MODELS = ["auto", "pro", "flash", "flash-lite"]
COPILOT_MODELS = ["gpt-5.5", "gpt-5.4-mini", "o4-mini"]

# --------------------------------------------------------------------------- #
#  Prompt packing: flatten system + turns into a single text prompt for CLIs
# --------------------------------------------------------------------------- #


def pack_prompt(system_prompt: str, turns: Sequence[tuple[str, str]]) -> str:
    """Flatten a system prompt and conversation turns into a single text block.

    CLIs take a single text prompt, not structured messages.  We embed the
    system instructions and conversation history into one prompt, then ask for
    a short, natural reply (matching Twitch chat expectations).
    """
    lines: list[str] = []
    if system_prompt.strip():
        lines.append(system_prompt.strip())
        lines.append("")
    if turns:
        lines.append("--- Conversation so far ---")
        for role, content in turns:
            speaker = "Assistant" if role == "assistant" else "User"
            lines.append(f"{speaker}: {content}")
        lines.append("--- End conversation ---")
        lines.append("")
    lines.append(
        "Respond naturally in 1-3 short sentences. No markdown, no prefixes, no meta-commentary. "
        "Do not use tools, inspect files, run commands, or access external account data."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  CLI executable discovery
# --------------------------------------------------------------------------- #


def find_cli(name: str) -> str | None:
    """Find an installed CLI, preferring stable user install locations.

    On Windows, editor extensions can put placeholder wrappers before npm's
    real global binaries on PATH. Prefer npm/WinGet/native user locations and
    ignore the known VS Code Copilot placeholder.
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        suffixes = (".exe", ".cmd", ".bat", "")
        roots = [
            Path(os.getenv("APPDATA", "")) / "npm",
            Path.home() / ".local" / "bin",
            Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
        ]
        for root in roots:
            if str(root) not in {".", ""}:
                candidates.extend(root / f"{name}{suffix}" for suffix in suffixes)
    else:
        candidates.append(Path.home() / ".local" / "bin" / name)

    discovered = shutil.which(name)
    if discovered:
        candidates.append(Path(discovered))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(str(candidate.resolve(strict=False)))
        comparison_key = normalized.casefold()
        if comparison_key in seen:
            continue
        seen.add(comparison_key)
        if "github.copilot-chat\\copilotcli" in comparison_key.replace("/", "\\"):
            continue
        if candidate.is_file():
            return str(candidate)
    return None


@dataclass(frozen=True)
class CLIDescriptor:
    """Static metadata for an official subscription CLI."""

    transport: str
    executable: str
    label: str
    npm_package: str
    login_hint: str  # what the user does in the interactive terminal


CLI_REGISTRY: dict[str, CLIDescriptor] = {
    T_CLAUDE: CLIDescriptor(
        transport=T_CLAUDE,
        executable="claude",
        label="Claude Code",
        npm_package="@anthropic-ai/claude-code",
        login_hint='Starte "claude" und folge dem Browser-Login.',
    ),
    T_CODEX: CLIDescriptor(
        transport=T_CODEX,
        executable="codex",
        label="OpenAI Codex",
        npm_package="@openai/codex",
        login_hint='Führe "codex login" aus (öffnet den Browser für OAuth).',
    ),
    T_GEMINI: CLIDescriptor(
        transport=T_GEMINI,
        executable="gemini",
        label="Google Gemini CLI",
        npm_package="@google/gemini-cli",
        login_hint='Starte "gemini" und wähle "Sign in with Google".',
    ),
    T_COPILOT: CLIDescriptor(
        transport=T_COPILOT,
        executable="copilot",
        label="GitHub Copilot",
        npm_package="@github/copilot",
        login_hint='Starte "copilot", dann "/login" um dich mit GitHub anzumelden.',
    ),
}


# --------------------------------------------------------------------------- #
#  Command builders  (return argument lists, never shell strings)
# --------------------------------------------------------------------------- #


def build_claude_command(model: str, timeout: float = 45) -> list[str]:
    """Build a headless, tool-disabled Claude Code command.

    ``--bare`` skips hooks/skills/MCP for fastest scripted startup.
    ``--tools ""`` removes ALL built-in tools so Claude cannot read/write files
    or run shell commands -- it can only produce text.
    ``--no-session-persistence`` prevents disk-stored sessions.
    The prompt itself is piped via stdin (``claude -p`` without a positional
    prompt reads stdin), never placed on the command line.
    """
    exe = find_cli("claude")
    if not exe:
        raise FileNotFoundError("Claude Code CLI ('claude') nicht gefunden.")
    return [
        exe,
        "--bare",
        "-p",  # print/headless mode; prompt kommt über stdin
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",  # disable ALL tools
        "--no-session-persistence",
    ]


def build_codex_command(model: str, timeout: float = 45) -> list[str]:
    """Build a headless, read-only Codex exec command.

    ``--sandbox read-only`` prevents file writes and shell execution.
    ``--json`` gives newline-delimited JSON events for parsing.
    The prompt is piped via stdin (positional ``-`` = read prompt from stdin).
    """
    exe = find_cli("codex")
    if not exe:
        raise FileNotFoundError("OpenAI Codex CLI ('codex') nicht gefunden.")
    return [
        exe,
        "exec",
        "--model",
        model,
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "-",  # Prompt aus stdin lesen
    ]


def build_gemini_command(model: str, timeout: float = 45) -> list[str]:
    """Build a headless Gemini CLI command.

    Piped stdin puts the CLI into non-interactive mode automatically; the
    prompt is delivered via stdin, never as a command-line argument.
    ``--output-format json`` returns structured JSON with a ``response`` field.
    """
    exe = find_cli("gemini")
    if not exe:
        raise FileNotFoundError("Google Gemini CLI ('gemini') nicht gefunden.")
    return [
        exe,
        "--model",
        model,
        "--output-format",
        "json",
        "--sandbox",
        "--approval-mode",
        "plan",
    ]


def build_copilot_command(model: str, timeout: float = 45) -> list[str]:
    """Build a headless, tool-restricted Copilot CLI command.

    ``-p`` is the programmatic/prompt mode.
    Tools are explicitly denied to prevent autonomous file/shell actions.
    Never uses ``--allow-all-tools``.
    Copilot reads the prompt as argv value of ``-p``; because npm installs a
    ``.cmd`` shim on Windows, ``run_cli`` strips cmd.exe metacharacters from
    the prompt in that case (see ``_argv_safe_prompt``).
    """
    exe = find_cli("copilot")
    if not exe:
        raise FileNotFoundError("GitHub Copilot CLI ('copilot') nicht gefunden.")
    return [
        exe,
        "--model",
        model,
        "--deny-tool=write",
        "--deny-tool=shell",
        "-p",  # prompt value is appended by run_cli
    ]


def build_command_for_transport(transport: str, model: str) -> list[str]:
    """Dispatch to the correct command builder for a CLI transport."""
    builders = {
        T_CLAUDE: build_claude_command,
        T_CODEX: build_codex_command,
        T_GEMINI: build_gemini_command,
        T_COPILOT: build_copilot_command,
    }
    builder = builders.get(transport)
    if not builder:
        raise ValueError(f"Unbekannter CLI-Transport: {transport}")
    return builder(model)


# --------------------------------------------------------------------------- #
#  Login command builders (open an interactive terminal)
# --------------------------------------------------------------------------- #


def build_login_command(transport: str) -> tuple[str, list[str]]:
    """Return (terminal_title, command_args) for an interactive login session.

    The command opens the CLI interactively so the user can authenticate via
    the official flow (browser OAuth, GitHub login, etc.).
    """
    desc = CLI_REGISTRY.get(transport)
    if not desc:
        raise ValueError(f"Unbekannter CLI-Transport: {transport}")
    exe = find_cli(desc.executable)
    if not exe:
        raise FileNotFoundError(
            f"{desc.label} CLI ('{desc.executable}') nicht im PATH gefunden. "
            f"Bitte installiere die offizielle CLI zuerst."
        )
    if transport == T_CODEX:
        return desc.label, [exe, "login"]
    # Claude, Gemini and Copilot expose their login selection inside the TUI.
    return desc.label, [exe]


# --------------------------------------------------------------------------- #
#  Response parsers
# --------------------------------------------------------------------------- #


def parse_claude_response(raw: str) -> str | None:
    """Extract the result text from a Claude Code JSON response.

    Claude ``--output-format json`` returns ``{"result": "...", ...}``.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: maybe partial JSON or plain text.
        return raw if raw else None
    if not isinstance(data, dict):
        return None
    # Fehlermeldungen ("Not logged in - Please run /login") nie als Chat-Antwort
    # durchreichen, auch wenn der Prozess mit Exit-Code 0 endet.
    if data.get("is_error"):
        return None
    result = data.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def parse_codex_response(raw: str) -> str | None:
    """Extract the final message from a Codex JSONL event stream.

    Codex ``--json`` emits newline-delimited JSON.  We look for the last event
    with a ``message`` or ``result`` field containing text content.
    """
    last_text: str | None = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # Codex events may carry the final message in different shapes.
        # Common patterns: {"type": "result", "message": "..."}
        #                  {"type": "message", "content": "..."}
        #                  {"type": "assistant", "message": "..."}
        msg = event.get("message") or event.get("content") or event.get("text")
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in {"agent_message", "message"}:
            msg = item.get("text") or item.get("content") or msg
        if isinstance(msg, str) and msg.strip():
            last_text = msg.strip()
        elif isinstance(msg, list):
            # Content-block array (OpenAI-style)
            texts = [
                block.get("text", "")
                for block in msg
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "".join(texts).strip()
            if joined:
                last_text = joined
    return last_text


def parse_gemini_response(raw: str) -> str | None:
    """Extract the response text from a Gemini CLI JSON output.

    Gemini ``--output-format json`` returns ``{"response": "...", ...}``.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw if raw else None
    if not isinstance(data, dict):
        return None
    response = data.get("response")
    if isinstance(response, str) and response.strip():
        return response.strip()
    return None


def parse_copilot_response(raw: str) -> str | None:
    """Extract the text from Copilot CLI plain-text output.

    Copilot ``-p`` returns plain text on stdout.  We strip common artifacts.
    """
    text = raw.strip()
    if not text:
        return None
    return text


def parse_response_for_transport(transport: str, raw: str) -> str | None:
    """Dispatch to the correct response parser for a CLI transport."""
    parsers = {
        T_CLAUDE: parse_claude_response,
        T_CODEX: parse_codex_response,
        T_GEMINI: parse_gemini_response,
        T_COPILOT: parse_copilot_response,
    }
    parser = parsers.get(transport)
    if not parser:
        raise ValueError(f"Unbekannter CLI-Transport: {transport}")
    return parser(raw)


# --------------------------------------------------------------------------- #
#  Model-list parsers
# --------------------------------------------------------------------------- #


def parse_openai_models(raw_json: str) -> list[str]:
    """Parse an OpenAI-compatible ``GET /models`` response.

    Expected: ``{"data": [{"id": "model-id", ...}, ...]}``
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    models: list[str] = []
    for entry in data.get("data", []):
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if isinstance(model_id, str) and model_id.strip():
            models.append(model_id.strip())
    return sorted(models)


def parse_google_models(raw_json: str) -> list[str]:
    """Parse a Google ``GET /v1beta/models`` response.

    Expected: ``{"models": [{"name": "models/gemma-4-31b-it",
    "supportedGenerationMethods": ["generateContent"], ...}, ...]}``

    Only models supporting ``generateContent`` are included.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    models: list[str] = []
    for entry in data.get("models", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        methods = entry.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        # Strip "models/" prefix.
        clean = name.removeprefix("models/") if name.startswith("models/") else name
        if clean:
            models.append(clean)
    return sorted(models)


def parse_codex_model_catalog(raw_json: str) -> list[str]:
    """Parse ``codex debug models --json`` output.

    The catalog format may vary; we extract any string that looks like a model
    ID from the JSON structure.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    models: list[str] = []

    def _extract(obj: object) -> None:
        if isinstance(obj, dict):
            for key in ("id", "model", "slug", "model_slug"):
                value = obj.get(key)
                if (
                    isinstance(value, str)
                    and re.search(r"[a-z0-9]-[a-z0-9]", value)
                    and " " not in value
                    and len(value) < 100
                ):
                    models.append(value)
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    _extract(value)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)

    _extract(data)
    return sorted(set(models))


# --------------------------------------------------------------------------- #
#  Models-request URL/header builders (used by GUI model loading)
# --------------------------------------------------------------------------- #


def build_models_request(endpoint: str, api_key: str, transport: str) -> tuple[str, dict[str, str]]:
    """Build (url, headers) for a GET /models request.

    For OpenAI-compatible endpoints the base chat/completions URL is
    converted to a ``/models`` path.  Google native uses ``/v1beta/models``.
    """
    if transport == "google_native":
        url = endpoint.rstrip("/") + "/models"
        headers = {"x-goog-api-key": api_key}
    else:
        # OpenAI-compatible: strip /chat/completions, append /models.
        url = endpoint.rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if url.endswith(suffix):
                url = url[: -len(suffix)] + "/models"
                break
        else:
            url = url + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return url, headers


def parse_models_for_transport(raw: str, transport: str) -> list[str]:
    """Dispatch model-list parsing based on transport type."""
    if transport == "google_native":
        return parse_google_models(raw)
    return parse_openai_models(raw)


# --------------------------------------------------------------------------- #
#  SSH tunnel command builder
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SSHTunnelConfig:
    """Configuration for a local SSH port-forward tunnel."""

    host: str = ""
    user: str = ""
    ssh_port: int = 22
    identity_file: str = ""
    local_port: int = 8080
    remote_host: str = "127.0.0.1"
    remote_port: int = 8080

    def validate(self) -> str | None:
        """Return an error message if invalid, or ``None`` if OK."""
        if not self.host:
            return "SSH-Host fehlt."
        if not self.user:
            return "SSH-User fehlt."
        if self.local_port < 1 or self.local_port > 65535:
            return "Local Port muss zwischen 1 und 65535 liegen."
        if self.remote_port < 1 or self.remote_port > 65535:
            return "Remote Port muss zwischen 1 und 65535 liegen."
        if self.ssh_port < 1 or self.ssh_port > 65535:
            return "SSH Port muss zwischen 1 und 65535 liegen."
        return None

    def local_endpoint(self) -> str:
        """The local OpenAI-compatible chat/completions endpoint after tunnel."""
        return f"http://127.0.0.1:{self.local_port}/v1/chat/completions"


def build_ssh_tunnel_command(cfg: SSHTunnelConfig) -> list[str]:
    """Build an SSH local-forward command as an argument list.

    Uses ``-N`` (no remote command), ``BatchMode=yes`` (no password prompts),
    ``ExitOnForwardFailure=yes`` (fail fast if the port is taken), and
    ``ServerAliveInterval`` for keepalive.
    """
    exe = find_cli("ssh")
    if not exe:
        raise FileNotFoundError(
            "SSH ('ssh') nicht im PATH gefunden. Installiere einen OpenSSH-Client."
        )
    error = cfg.validate()
    if error:
        raise ValueError(error)

    args = [
        exe,
        "-N",  # no remote command, just forward
        "-L",
        f"127.0.0.1:{cfg.local_port}:{cfg.remote_host}:{cfg.remote_port}",
        "-p",
        str(cfg.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
    ]
    if cfg.identity_file:
        args.extend(["-i", cfg.identity_file])
    args.append(f"{cfg.user}@{cfg.host}")
    return args


# --------------------------------------------------------------------------- #
#  Async CLI runner (used by the bot's LLMClient for CLI transports)
# --------------------------------------------------------------------------- #


@dataclass
class CLIResult:
    """Result of a CLI invocation."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


def _argv_safe_prompt(executable: str, prompt: str) -> str:
    """Neutralise cmd.exe metacharacters when the target is a ``.cmd``/``.bat`` shim.

    Windows startet Batch-Dateien über cmd.exe, das Argumente NACH dem Quoting
    erneut interpretiert ("BatBadBut"). Da der Prompt ungefilterten Twitch-Chat
    enthält, werden für Batch-Shims alle cmd-Metazeichen entfernt und
    Zeilenumbrüche zu ``  |  `` (visueller Trenner) zusammengefaltet. Für echte
    ``.exe``-Binaries bleibt der Prompt unverändert.
    """
    if sys.platform == "win32" and executable.lower().endswith((".cmd", ".bat")):
        prompt = re.sub(r"[\r\n]+", "  /  ", prompt)
        prompt = re.sub(r'[%!^"<>&|]', "", prompt)
    return prompt


async def run_cli(
    args: list[str],
    prompt: str,
    *,
    timeout: float = 45,
    prompt_via_stdin: bool = False,
) -> CLIResult:
    """Run a CLI command, delivering the prompt via stdin or as final argument.

    Uses ``asyncio.create_subprocess_exec`` (never ``shell=True``).
    With ``prompt_via_stdin`` the prompt is piped to the process; otherwise it
    is appended as the final argument (metacharacter-stripped for ``.cmd``
    shims, see ``_argv_safe_prompt``). On timeout, the process is killed.
    """
    if prompt_via_stdin:
        full_args = list(args)
        stdin_data: bytes | None = prompt.encode("utf-8", errors="replace")
    else:
        full_args = args + [_argv_safe_prompt(args[0], prompt)]
        stdin_data = None
    # Never expose the repository or user files as the agent workspace. Even
    # read-only coding CLIs can discover project instructions or account tools.
    with tempfile.TemporaryDirectory(prefix="pandabot-cli-") as isolated_cwd:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=isolated_cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CLIResult(stdout="", stderr="timeout", returncode=-1, timed_out=True)

        return CLIResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
        )


async def cli_complete(
    transport: str,
    model: str,
    system_prompt: str,
    turns: Sequence[tuple[str, str]],
    *,
    timeout: float = 45,
) -> str | None:
    """Complete a chat turn via a CLI backend.

    Packs the system prompt and conversation into a single text prompt, runs
    the CLI headless, and parses the response.  Returns ``None`` on any error
    (the bot then falls back or stays silent).
    """
    try:
        base_args = build_command_for_transport(transport, model)
    except (FileNotFoundError, ValueError):
        return None

    prompt = pack_prompt(system_prompt, turns)
    result = await run_cli(
        base_args,
        prompt,
        timeout=timeout,
        prompt_via_stdin=transport in STDIN_PROMPT_TRANSPORTS,
    )

    if result.timed_out or result.returncode != 0:
        # Grund loggen (z. B. "Not logged in"), sonst wirkt ein fehlender
        # CLI-Login wie ein stumm kaputter Bot.
        detail = (result.stderr or result.stdout).strip()[:200]
        LOGGER.warning(
            "CLI-Backend %s fehlgeschlagen (Exit %s%s): %s",
            transport,
            result.returncode,
            ", Timeout" if result.timed_out else "",
            detail or "(keine Ausgabe)",
        )
        return None

    return parse_response_for_transport(transport, result.stdout)
