#!/usr/bin/env python3
"""PandaBot LLM Control GUI.

Cross-platform (Windows/Linux/macOS) tkinter GUI for:

- Managing **API profiles** (provider, key, endpoint, model dropdown, params).
- **On-the-fly model switching**: the running bot picks up a new profile via
  an atomic control file (``PANDABOT_GUI_CONTROL=1``).
- **Abo/CLI logins**: opens an interactive terminal with the official CLI
  (Claude Code, Codex, Gemini CLI, Copilot) for authentication.
- **SSH tunnels**: local port-forward to a remote OpenAI-compatible endpoint.
- **Bot start/stop** with live stdout/stderr log display.

Usage::

    python pandabot_gui.py            # uses repo .venv if available
    python pandabot_gui.py --python /custom/python

The GUI itself only needs Python stdlib (tkinter).  On some Linux distros
install ``python3-tk`` separately.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

try:
    import sv_ttk  # type: ignore[import-not-found]
except ModuleNotFoundError:
    sv_ttk = None

if TYPE_CHECKING:
    import subprocess as sp

# --- Local imports ---
from cli_backends import (
    CLAUDE_ALIASES,
    CLI_REGISTRY,
    COPILOT_MODELS,
    GEMINI_CLI_MODELS,
    SSHTunnelConfig,
    build_login_command,
    build_models_request,
    build_ssh_tunnel_command,
    find_cli,
    parse_codex_model_catalog,
    parse_models_for_transport,
)
from llm_profiles import (
    DEFAULT_CONTROL_PATH,
    PROVIDERS,
    LLMProfile,
    ProfileStore,
    normalize_model_id,
    write_control_file,
)

REPO_ROOT = Path(__file__).resolve().parent
PROFILES_PATH = REPO_ROOT / "llm_profiles.json"
CONTROL_PATH = REPO_ROOT / str(DEFAULT_CONTROL_PATH)


# --------------------------------------------------------------------------- #
#  Cross-platform process helpers
# --------------------------------------------------------------------------- #


def find_bot_python(custom: str | None = None) -> str:
    """Find the Python executable for running the bot.

    Preference: explicit ``--python`` arg > repo ``.venv`` > current interpreter.
    """
    if custom:
        p = Path(custom)
        if p.is_file():
            return str(p.resolve())
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = REPO_ROOT / rel
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def open_terminal(command_args: list[str], title: str = "") -> bool:
    """Open a new terminal window running *command_args*.

    Returns ``True`` if a terminal was launched.  Cross-platform best-effort:
    no exception on failure -- the caller shows a message box.
    """
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/k"] + command_args,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=str(REPO_ROOT),
            )
            return True
        if sys.platform == "darwin":
            joined = " ".join(shlex.quote(arg) for arg in command_args)
            escaped = joined.replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "Terminal" to do script "{escaped}"'
            subprocess.Popen(["osascript", "-e", script])
            return True
        # Linux: try common terminal emulators.
        terminals = [
            ["x-terminal-emulator", "-e"],
            ["gnome-terminal", "--"],
            ["konsole", "-e"],
            ["xterm", "-e"],
            ["alacritty", "-e"],
            ["kitty", "--"],
        ]
        for term_cmd in terminals:
            if shutil.which(term_cmd[0]):
                subprocess.Popen(term_cmd + command_args, cwd=str(REPO_ROOT))
                return True
        return False
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  Main GUI application
# --------------------------------------------------------------------------- #


class PandaBotGUI:
    """Main application window."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PandaBot Control Center")
        self.root.geometry("1080x780")
        self.root.minsize(920, 680)
        self._dark_mode = True
        self._configure_theme()

        # State
        self.store = ProfileStore.load(PROFILES_PATH)
        self._bot_proc: sp.Popen[str] | None = None
        self._bot_log_thread: threading.Thread | None = None
        self._bot_running = False
        self._ssh_proc: sp.Popen[str] | None = None
        self._monitor_thread: threading.Thread | None = None
        self._custom_python: str | None = None
        self._install_running = False

        self._build_ui()
        self._refresh_profile_list()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- UI construction ------------------------------------------------ #

    def _configure_theme(self) -> None:
        """Apply a modern Sun Valley theme, with a styled fallback."""
        if sv_ttk is not None:
            sv_ttk.set_theme("dark")
        else:
            style = ttk.Style(self.root)
            style.theme_use("clam")
            style.configure("TButton", padding=(12, 7))
            style.configure("TNotebook.Tab", padding=(14, 8))
        style = ttk.Style(self.root)
        style.configure("Header.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Subheader.TLabel", font=("TkDefaultFont", 10))
        style.configure("TLabelframe", padding=6)
        style.configure("TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 12, 18, 4))
        header.pack(fill=tk.X)
        title_box = ttk.Frame(header)
        title_box.pack(side=tk.LEFT)
        ttk.Label(title_box, text="PandaBot Control Center", style="Header.TLabel").pack(
            anchor=tk.W
        )
        ttk.Label(
            title_box,
            text="Modelle, Anbieter, Abos und Bot-Laufzeit an einem Ort",
            style="Subheader.TLabel",
        ).pack(anchor=tk.W)
        ttk.Button(header, text="Hell / Dunkel", command=self._toggle_theme).pack(side=tk.RIGHT)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(8, 12))

        self._build_api_tab(notebook)
        self._build_cli_tab(notebook)
        self._build_ssh_tab(notebook)
        self._build_bot_tab(notebook)

        # Status bar
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor=tk.W,
            padding=(14, 8),
        ).pack(fill=tk.X, side=tk.BOTTOM)
        self._apply_native_widget_colors()

    def _toggle_theme(self) -> None:
        self._dark_mode = not self._dark_mode
        if sv_ttk is not None:
            sv_ttk.set_theme("dark" if self._dark_mode else "light")
        self._apply_native_widget_colors()

    def _apply_native_widget_colors(self) -> None:
        """Match classic tk Listbox/Text widgets to the ttk theme."""
        if self._dark_mode:
            bg, field, fg, select = "#15171a", "#1f2227", "#f2f3f5", "#176b9c"
        else:
            bg, field, fg, select = "#f5f6f8", "#ffffff", "#202124", "#3b82b6"
        self.root.configure(bg=bg)
        if hasattr(self, "profile_listbox"):
            self.profile_listbox.configure(
                bg=field,
                fg=fg,
                selectbackground=select,
                selectforeground="#ffffff",
                relief=tk.FLAT,
                highlightthickness=0,
            )
        if hasattr(self, "_log_text"):
            self._log_text.configure(
                bg=field,
                fg=fg,
                insertbackground=fg,
                relief=tk.FLAT,
                highlightthickness=0,
            )

    # --- Tab: API Profiles ----------------------------------------------- #

    def _build_api_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="API & Modelle")

        # Left: profile list
        left = ttk.Frame(frame, width=250)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=8)
        left.pack_propagate(False)

        ttk.Label(left, text="Profile:").pack(anchor=tk.W)
        self.profile_listbox = tk.Listbox(left, height=20)
        self.profile_listbox.pack(fill=tk.Y, expand=True, pady=4)
        self.profile_listbox.bind("<<ListboxSelect>>", self._on_profile_select)

        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Neu", command=self._new_profile).pack(
            side=tk.LEFT, expand=True, fill=tk.X
        )
        ttk.Button(btn_frame, text="Löschen", command=self._delete_profile).pack(
            side=tk.LEFT, expand=True, fill=tk.X
        )

        # Right: profile editor
        right = ttk.Frame(frame)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=8)

        # Form fields
        self._profile_vars: dict[str, tk.StringVar | tk.BooleanVar] = {}
        fields = [
            ("name", "Name:", "entry"),
            ("provider", "Provider:", "provider"),
            ("api_key", "API-Key:", "password"),
            ("endpoint", "Endpoint:", "entry"),
            ("model", "Modell:", "model_combo"),
            ("temperature", "Temperatur:", "entry"),
            ("top_p", "Top-P:", "entry"),
            ("max_tokens", "Max Tokens:", "entry"),
            ("timeout", "Timeout (s):", "entry"),
        ]
        for key, label, widget_type in fields:
            row = ttk.Frame(right)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            var = tk.StringVar()
            self._profile_vars[key] = var

            if widget_type == "provider":
                combo = ttk.Combobox(row, textvariable=var, state="readonly", width=40)
                combo["values"] = [f"{p.id}  ({p.label})" for p in PROVIDERS.values()]
                combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
                combo.bind("<<ComboboxSelected>>", self._on_provider_change)
            elif widget_type == "password":
                self._api_key_entry = ttk.Entry(row, textvariable=var, show="*", width=42)
                self._api_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
                # Toggle visibility checkbox
                self._show_key_var = tk.BooleanVar(value=False)
                ttk.Checkbutton(
                    row,
                    text="👁",
                    variable=self._show_key_var,
                    command=self._toggle_key_visibility,
                ).pack(side=tk.LEFT)
            elif widget_type == "model_combo":
                self._model_combo = ttk.Combobox(row, textvariable=var, width=42)
                self._model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            else:
                ttk.Entry(row, textvariable=var, width=42).pack(
                    side=tk.LEFT, fill=tk.X, expand=True
                )

        # System role checkbox
        sys_row = ttk.Frame(right)
        sys_row.pack(fill=tk.X, pady=2)
        self._profile_vars["use_system_role"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sys_row,
            text="System-Rolle verwenden",
            variable=self._profile_vars["use_system_role"],
        ).pack(anchor=tk.W)

        # Buttons
        btn_row = ttk.Frame(right)
        btn_row.pack(fill=tk.X, pady=8)
        ttk.Button(btn_row, text="Speichern", command=self._save_profile).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Modelle laden", command=self._load_models_threaded).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(
            btn_row,
            text="Profil aktivieren",
            command=self._activate_profile,
            style="Accent.TButton",
        ).pack(side=tk.LEFT, padx=4)

        # Info label
        info = ttk.Label(
            right,
            text=(
                "„Modelle laden“ ruft /models mit deinem API-Key ab und befüllt das "
                "Modell-Dropdown. Das Dropdown bleibt editierbar.\n"
                "„Profil aktivieren“ schreibt eine Control-Datei – der laufende Bot "
                "übernimmt das Profil vor dem nächsten Request (PANDABOT_GUI_CONTROL=1)."
            ),
            justify=tk.LEFT,
            foreground="gray50",
            wraplength=550,
        )
        info.pack(anchor=tk.W, pady=8)

    # --- Tab: Abo / CLI -------------------------------------------------- #

    def _build_cli_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Abos & Login")

        top_actions = ttk.Frame(frame, padding=(12, 12, 12, 0))
        top_actions.pack(fill=tk.X)
        ttk.Button(
            top_actions,
            text="Alle offiziellen CLIs installieren",
            command=self._install_all_clis,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)

        ttk.Label(
            frame,
            text=(
                "Abo-Modelle laufen über die offiziellen, bereits installierten CLIs. "
                "Consumer-Abo ≠ API – Authentifizierung, Verfügbarkeit und Modellauswahl "
                "hängen vom jeweiligen Abo ab."
            ),
            justify=tk.LEFT,
            wraplength=800,
            foreground="gray50",
        ).pack(anchor=tk.W, padx=12, pady=(12, 8))

        self._cli_model_vars: dict[str, tk.StringVar] = {}
        self._cli_model_combos: dict[str, ttk.Combobox] = {}
        self._cli_status_vars: dict[str, tk.StringVar] = {}

        for transport, desc in CLI_REGISTRY.items():
            section = ttk.LabelFrame(frame, text=desc.label, padding=8)
            section.pack(fill=tk.X, padx=12, pady=4)

            row = ttk.Frame(section)
            row.pack(fill=tk.X)

            # Model dropdown
            ttk.Label(row, text="Modell:").pack(side=tk.LEFT)
            suggestions: list[str]
            if transport == "claude_cli":
                suggestions = CLAUDE_ALIASES
            elif transport == "gemini_cli":
                suggestions = GEMINI_CLI_MODELS
            elif transport == "copilot_cli":
                suggestions = COPILOT_MODELS
            else:
                suggestions = []
            model_var = tk.StringVar(value=suggestions[0] if suggestions else "")
            self._cli_model_vars[transport] = model_var
            combo = ttk.Combobox(row, textvariable=model_var, width=30)
            combo["values"] = suggestions
            combo.pack(side=tk.LEFT, padx=8)
            self._cli_model_combos[transport] = combo

            ttk.Button(
                row,
                text="Installieren",
                command=lambda t=transport: self._install_cli(t),
            ).pack(side=tk.LEFT, padx=4)
            ttk.Button(row, text="Login", command=lambda t=transport: self._cli_login(t)).pack(
                side=tk.LEFT, padx=4
            )
            if transport == "codex_cli":
                ttk.Button(
                    row,
                    text="Modelle laden",
                    command=self._load_codex_models_threaded,
                ).pack(side=tk.LEFT, padx=4)
            ttk.Button(
                row,
                text="Aktivieren",
                command=lambda t=transport: self._activate_cli_profile(t),
                style="Accent.TButton",
            ).pack(side=tk.LEFT, padx=4)

            # Status
            status_var = tk.StringVar()
            self._cli_status_vars[transport] = status_var
            exe = find_cli(desc.executable)
            if exe:
                status_var.set(f"✓ {desc.executable} gefunden: {exe}")
            else:
                status_var.set(f"✗ {desc.executable} nicht gefunden. Bitte installieren.")
            ttk.Label(section, textvariable=status_var, foreground="gray50").pack(
                anchor=tk.W, pady=(4, 0)
            )
            ttk.Label(section, text=desc.login_hint, foreground="gray60").pack(anchor=tk.W)

        # Security note
        ttk.Label(
            frame,
            text=(
                "Sicherheitshinweis: CLI-Requests laufen in einem leeren temporären "
                "Arbeitsordner mit tool-disabled/read-only/plan/deny-Flags. Coding-CLIs "
                "bleiben dennoch Agenten; globale MCP-/Abo-Tools des jeweiligen CLI "
                "solltest du zusätzlich deaktivieren."
            ),
            justify=tk.LEFT,
            foreground="gray40",
            wraplength=800,
        ).pack(anchor=tk.W, padx=12, pady=8)

    # --- Tab: SSH Tunnel ------------------------------------------------- #

    def _build_ssh_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="SSH-Tunnel")

        self._ssh_vars: dict[str, tk.StringVar] = {}
        ssh_fields = [
            ("host", "Host:", ""),
            ("user", "User:", ""),
            ("ssh_port", "SSH Port:", "22"),
            ("identity_file", "Identity File:", ""),
            ("local_port", "Local Port:", "8080"),
            ("remote_host", "Remote Host:", "127.0.0.1"),
            ("remote_port", "Remote Port:", "8080"),
        ]
        for key, label, default in ssh_fields:
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, padx=12, pady=3)
            ttk.Label(row, text=label, width=16).pack(side=tk.LEFT)
            var = tk.StringVar(value=default)
            self._ssh_vars[key] = var
            entry = ttk.Entry(row, textvariable=var, width=40)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            if key == "identity_file":
                ttk.Button(row, text="…", width=3, command=self._browse_identity).pack(
                    side=tk.LEFT, padx=4
                )

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, padx=12, pady=8)
        ttk.Button(btn_row, text="Tunnel starten", command=self._ssh_start).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_row, text="Tunnel stoppen", command=self._ssh_stop).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_row, text="Test", command=self._ssh_test).pack(side=tk.LEFT, padx=4)

        self._ssh_status_var = tk.StringVar(value="Tunnel: nicht aktiv.")
        ttk.Label(frame, textvariable=self._ssh_status_var).pack(anchor=tk.W, padx=12, pady=4)

        ttk.Label(
            frame,
            text=(
                "Der Tunnel forwarded einen lokalen Port an einen entfernten "
                "OpenAI-kompatiblen Endpoint. BatchMode=yes verhindert "
                "Passwort-Prompts (nur Key-Auth). Privater Schlüssel wird nicht "
                "gespeichert, nur der Pfad."
            ),
            justify=tk.LEFT,
            foreground="gray50",
            wraplength=800,
        ).pack(anchor=tk.W, padx=12, pady=8)

    # --- Tab: Bot Control ------------------------------------------------ #

    def _build_bot_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="Bot")

        top = ttk.Frame(frame)
        top.pack(fill=tk.X, padx=12, pady=8)

        self._bot_start_btn = ttk.Button(
            top, text="Bot starten", command=self._start_bot, style="Accent.TButton"
        )
        self._bot_start_btn.pack(side=tk.LEFT, padx=4)

        self._bot_stop_btn = ttk.Button(
            top, text="Bot stoppen", command=self._stop_bot, state=tk.DISABLED
        )
        self._bot_stop_btn.pack(side=tk.LEFT, padx=4)

        self._bot_status_var = tk.StringVar(value="Bot: gestoppt.")
        ttk.Label(top, textvariable=self._bot_status_var).pack(side=tk.LEFT, padx=8)

        ttk.Label(
            frame,
            text=(
                "Der Bot wird mit PANDABOT_GUI_CONTROL=1 gestartet und übernimmt "
                "Profil-Wechsel aus der Control-Datei vor dem nächsten Request."
            ),
            justify=tk.LEFT,
            foreground="gray50",
        ).pack(anchor=tk.W, padx=12)

        # Log display
        ttk.Label(frame, text="Bot-Logs:").pack(anchor=tk.W, padx=12, pady=(8, 0))
        self._log_text = tk.Text(frame, height=18, state=tk.DISABLED, wrap=tk.WORD)
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

    # ----- API Profile actions ------------------------------------------- #

    def _refresh_profile_list(self) -> None:
        self.profile_listbox.delete(0, tk.END)
        for name in self.store.names():
            marker = " ★" if name == self.store.active else ""
            self.profile_listbox.insert(tk.END, f"{name}{marker}")

    def _on_profile_select(self, _event: object) -> None:
        sel = self.profile_listbox.curselection()
        if not sel:
            return
        display = self.profile_listbox.get(sel[0])
        name = display.rstrip(" ★")
        prof = self.store.profiles.get(name)
        if not prof:
            return
        self._profile_vars["name"].set(prof.name)
        preset = PROVIDERS.get(prof.provider)
        if preset:
            self._profile_vars["provider"].set(f"{preset.id}  ({preset.label})")
        else:
            self._profile_vars["provider"].set(prof.provider)
        self._profile_vars["api_key"].set(prof.api_key)
        self._profile_vars["endpoint"].set(prof.endpoint)
        self._profile_vars["model"].set(prof.model)
        self._profile_vars["temperature"].set(str(prof.temperature))
        self._profile_vars["top_p"].set(str(prof.top_p))
        self._profile_vars["max_tokens"].set(str(prof.max_tokens))
        self._profile_vars["timeout"].set(str(prof.timeout))
        self._profile_vars["use_system_role"].set(prof.use_system_role)

    def _get_provider_id(self) -> str:
        """Extract the provider ID from the 'id  (label)' combo string."""
        raw = self._profile_vars["provider"].get()
        if "  (" in raw:
            return raw.split("  (")[0]
        return raw

    def _on_provider_change(self, _event: object) -> None:
        provider_id = self._get_provider_id()
        preset = PROVIDERS.get(provider_id)
        if not preset:
            return
        # Pre-fill endpoint from preset if empty or mismatched.
        if preset.base_url:
            self._profile_vars["endpoint"].set(preset.base_url)
        # Set sensible defaults.
        self._profile_vars["max_tokens"].set(str(preset.max_tokens_default))
        self._profile_vars["timeout"].set(str(preset.timeout_default))
        self._profile_vars["use_system_role"].set(preset.supports_system_role)

    def _toggle_key_visibility(self) -> None:
        self._api_key_entry.configure(show="" if self._show_key_var.get() else "*")

    def _new_profile(self) -> None:
        """Create a blank profile form pre-filled from the Custom preset."""
        prof = LLMProfile.from_preset("Neues Profil", "custom")
        self._profile_vars["name"].set(prof.name)
        preset = PROVIDERS["custom"]
        self._profile_vars["provider"].set(f"{preset.id}  ({preset.label})")
        self._profile_vars["api_key"].set("")
        self._profile_vars["endpoint"].set("")
        self._profile_vars["model"].set("")
        self._profile_vars["temperature"].set("0.8")
        self._profile_vars["top_p"].set("0.95")
        self._profile_vars["max_tokens"].set("300")
        self._profile_vars["timeout"].set("30")
        self._profile_vars["use_system_role"].set(True)
        self.profile_listbox.selection_clear(0, tk.END)

    def _collect_profile(self) -> LLMProfile | None:
        """Read form fields into an LLMProfile. Returns None on validation error."""
        name = self._profile_vars["name"].get().strip()
        if not name:
            messagebox.showerror("Fehler", "Profil-Name darf nicht leer sein.")
            return None
        provider_id = self._get_provider_id()
        try:
            temp = float(self._profile_vars["temperature"].get())
            top_p = float(self._profile_vars["top_p"].get())
            max_tok = int(self._profile_vars["max_tokens"].get())
            timeout = float(self._profile_vars["timeout"].get())
        except ValueError:
            messagebox.showerror("Fehler", "Numerische Felder müssen gültige Zahlen sein.")
            return None

        preset = PROVIDERS.get(provider_id, PROVIDERS["custom"])
        if preset.transport == "google_native":
            max_tok = max(512, max_tok)
            self._profile_vars["max_tokens"].set(str(max_tok))
        model = self._profile_vars["model"].get().strip()
        model = normalize_model_id(model, provider=provider_id) if model else model

        return LLMProfile(
            name=name,
            provider=preset.id,
            api_key=self._profile_vars["api_key"].get().strip(),
            endpoint=self._profile_vars["endpoint"].get().strip(),
            model=model,
            max_tokens=max_tok,
            temperature=temp,
            top_p=top_p,
            timeout=timeout,
            use_system_role=bool(self._profile_vars["use_system_role"].get()),
            send_repeat_penalty=preset.send_repeat_penalty,
            send_llama_extras=preset.send_llama_extras,
            repeat_penalty=1.15,
        )

    def _save_profile(self) -> None:
        prof = self._collect_profile()
        if prof is None:
            return
        self.store.upsert(prof)
        self.store.save()
        self._refresh_profile_list()
        self.status_var.set(f"Profil „{prof.name}“ gespeichert.")

    def _delete_profile(self) -> None:
        sel = self.profile_listbox.curselection()
        if not sel:
            return
        display = self.profile_listbox.get(sel[0])
        name = display.rstrip(" ★")
        if messagebox.askyesno("Löschen", f"Profil „{name}“ wirklich löschen?"):
            self.store.delete(name)
            self.store.save()
            self._refresh_profile_list()
            self.status_var.set(f"Profil „{name}“ gelöscht.")

    def _activate_profile(self) -> None:
        """Write the control file for the currently selected/edited profile."""
        prof = self._collect_profile()
        if prof is None:
            return
        # Also save it to the store.
        self.store.upsert(prof)
        self.store.active = prof.name
        self.store.save()
        write_control_file(prof, CONTROL_PATH)
        self._refresh_profile_list()
        self.status_var.set(
            f"Profil „{prof.name}“ aktiviert. Der Bot übernimmt es beim nächsten Request."
        )

    def _load_models_threaded(self) -> None:
        """Start a background thread to fetch models for the current profile."""
        prof = self._collect_profile()
        if prof is None:
            return
        api_key = prof.resolve_api_key()
        if not api_key and prof.provider != "local":
            messagebox.showwarning(
                "API-Key fehlt",
                "Kein API-Key gesetzt und kein Fallback in der .env gefunden.",
            )
            return
        endpoint = prof.effective_endpoint()
        if not endpoint:
            messagebox.showwarning("Endpoint fehlt", "Kein Endpoint gesetzt.")
            return
        transport = prof.transport()
        self.status_var.set("Lade Modelle...")
        self._model_combo["values"] = []
        thread = threading.Thread(
            target=self._fetch_models, args=(endpoint, api_key, transport), daemon=True
        )
        thread.start()

    def _fetch_models(self, endpoint: str, api_key: str, transport: str) -> None:
        """Background: fetch all model pages and update the combobox."""
        try:
            url, headers = build_models_request(endpoint, api_key, transport)
            models: list[str] = []
            page_token = ""
            while True:
                page_url = url
                if transport == "google_native":
                    query = {"pageSize": "1000"}
                    if page_token:
                        query["pageToken"] = page_token
                    page_url += "?" + urllib.parse.urlencode(query)
                req = urllib.request.Request(page_url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                models.extend(parse_models_for_transport(raw, transport))
                if transport != "google_native":
                    break
                payload = json.loads(raw)
                page_token = payload.get("nextPageToken", "")
                if not page_token:
                    break
            models = sorted(set(models))
        except (json.JSONDecodeError, urllib.error.URLError, OSError) as exc:
            self.root.after(0, self._models_error, str(exc))
            return
        self.root.after(0, self._models_loaded, models)

    def _models_loaded(self, models: list[str]) -> None:
        self._model_combo["values"] = models
        if models:
            self._model_combo.set(models[0])
        self.status_var.set(f"{len(models)} Modelle geladen.")

    def _models_error(self, msg: str) -> None:
        messagebox.showerror("Modelle laden fehlgeschlagen", msg[:300])
        self.status_var.set("Modelle laden fehlgeschlagen.")

    # ----- CLI actions --------------------------------------------------- #

    def _cli_login(self, transport: str) -> None:
        """Open a terminal with the official CLI for interactive login."""
        desc = CLI_REGISTRY.get(transport)
        if not desc:
            return
        try:
            title, cmd = build_login_command(transport)
        except FileNotFoundError:
            if messagebox.askyesno(
                "CLI installieren",
                f"{desc.label} ist noch nicht installiert.\n\n"
                f"Jetzt das offizielle Paket {desc.npm_package} installieren?",
            ):
                self._install_cli(transport)
            return
        if not open_terminal(cmd, title):
            messagebox.showerror(
                "Terminal fehlt",
                "Es konnte kein Terminal-Emulator gefunden werden.\n"
                "Bitte führe das CLI manuell in einem Terminal aus.",
            )

    def _install_cli(self, transport: str) -> None:
        self._begin_cli_install([transport])

    def _install_all_clis(self) -> None:
        self._begin_cli_install(list(CLI_REGISTRY))

    def _begin_cli_install(self, transports: list[str]) -> None:
        """Install official CLI packages asynchronously via npm."""
        if self._install_running:
            messagebox.showinfo(
                "Installation läuft", "Bitte warte, bis die Installation fertig ist."
            )
            return
        npm = find_cli("npm")
        if not npm:
            messagebox.showerror(
                "Node.js/npm fehlt",
                "npm wurde nicht gefunden. Installiere zuerst Node.js 22 oder neuer.",
            )
            return
        self._install_running = True
        for transport in transports:
            self._cli_status_vars[transport].set("Installation läuft …")
        self.status_var.set("Installiere offizielle Anbieter-CLIs …")
        threading.Thread(
            target=self._install_cli_worker,
            args=(npm, transports),
            daemon=True,
        ).start()

    def _install_cli_worker(self, npm: str, transports: list[str]) -> None:
        results: dict[str, str] = {}
        for transport in transports:
            desc = CLI_REGISTRY[transport]
            try:
                completed = subprocess.run(
                    [npm, "install", "-g", f"{desc.npm_package}@latest"],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    check=False,
                )
                if completed.returncode == 0:
                    results[transport] = ""
                else:
                    results[transport] = (completed.stderr or completed.stdout).strip()[-500:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                results[transport] = str(exc)
        self.root.after(0, self._cli_install_finished, transports, results)

    def _cli_install_finished(self, transports: list[str], results: dict[str, str]) -> None:
        self._install_running = False
        failed: list[str] = []
        for transport in transports:
            desc = CLI_REGISTRY[transport]
            executable = find_cli(desc.executable)
            error = results.get(transport, "")
            if not error and executable:
                self._cli_status_vars[transport].set(f"Bereit: {executable}")
            else:
                failed.append(desc.label)
                detail = error or "Installation beendet, Befehl aber noch nicht gefunden."
                self._cli_status_vars[transport].set(f"Fehler: {detail[:180]}")
        if failed:
            self.status_var.set("CLI-Installation teilweise fehlgeschlagen.")
            messagebox.showerror(
                "Installation",
                "Nicht erfolgreich: "
                + ", ".join(failed)
                + "\n\nDetails stehen im jeweiligen Status.",
            )
        else:
            self.status_var.set("Offizielle Anbieter-CLIs sind installiert.")
            messagebox.showinfo(
                "Installation fertig",
                "Installation erfolgreich. Klicke beim gewünschten Anbieter auf „Login“.",
            )

    def _activate_cli_profile(self, transport: str) -> None:
        """Activate an official CLI subscription with the selected model."""
        desc = CLI_REGISTRY[transport]
        if not find_cli(desc.executable):
            messagebox.showerror(
                "CLI fehlt", f"{desc.label} ('{desc.executable}') wurde nicht gefunden."
            )
            return
        model = self._cli_model_vars[transport].get().strip()
        if not model:
            messagebox.showerror("Modell fehlt", "Bitte zuerst ein Modell auswählen/eintragen.")
            return
        profile = LLMProfile.from_preset(f"Abo: {desc.label}", transport)
        profile.model = model
        self.store.upsert(profile)
        self.store.active = profile.name
        self.store.save()
        write_control_file(profile, CONTROL_PATH)
        self._refresh_profile_list()
        self.status_var.set(f"{desc.label} / {model} aktiviert; Wechsel beim nächsten Bot-Request.")

    def _load_codex_models_threaded(self) -> None:
        """Load the authenticated Codex model catalog without blocking tkinter."""
        exe = find_cli("codex")
        if not exe:
            messagebox.showerror("CLI fehlt", "OpenAI Codex CLI ('codex') nicht gefunden.")
            return
        self.status_var.set("Lade Codex-Modelle aus dem angemeldeten Abo...")
        threading.Thread(target=self._load_codex_models, args=(exe,), daemon=True).start()

    def _load_codex_models(self, exe: str) -> None:
        try:
            result = subprocess.run(
                [exe, "debug", "models"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or f"Exit-Code {result.returncode}")
            models = parse_codex_model_catalog(result.stdout)
            if not models:
                raise OSError("Codex lieferte keinen lesbaren Modellkatalog.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.root.after(0, self._models_error, str(exc))
            return
        self.root.after(0, self._codex_models_loaded, models)

    def _codex_models_loaded(self, models: list[str]) -> None:
        combo = self._cli_model_combos["codex_cli"]
        combo["values"] = models
        combo.set(models[0])
        self.status_var.set(f"{len(models)} Codex-Abo-Modelle geladen.")

    # ----- SSH actions --------------------------------------------------- #

    def _browse_identity(self) -> None:
        path = filedialog.askopenfilename(
            title="SSH Identity File wählen",
            filetypes=[("Alle Dateien", "*.*")],
        )
        if path:
            self._ssh_vars["identity_file"].set(path)

    def _ssh_config(self) -> SSHTunnelConfig | None:
        try:
            return SSHTunnelConfig(
                host=self._ssh_vars["host"].get().strip(),
                user=self._ssh_vars["user"].get().strip(),
                ssh_port=int(self._ssh_vars["ssh_port"].get() or 22),
                identity_file=self._ssh_vars["identity_file"].get().strip(),
                local_port=int(self._ssh_vars["local_port"].get() or 8080),
                remote_host=self._ssh_vars["remote_host"].get().strip() or "127.0.0.1",
                remote_port=int(self._ssh_vars["remote_port"].get() or 8080),
            )
        except ValueError:
            messagebox.showerror("Konfiguration", "SSH- und Forward-Ports müssen Zahlen sein.")
            return None

    def _ssh_start(self) -> None:
        if self._ssh_proc is not None:
            messagebox.showinfo("Tunnel", "Tunnel läuft bereits.")
            return
        cfg = self._ssh_config()
        if cfg is None:
            return
        error = cfg.validate()
        if error:
            messagebox.showerror("Konfiguration", error)
            return
        try:
            args = build_ssh_tunnel_command(cfg)
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror("SSH", str(exc))
            return
        try:
            if sys.platform == "win32":
                self._ssh_proc = subprocess.Popen(
                    args,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            else:
                self._ssh_proc = subprocess.Popen(
                    args,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
        except OSError as exc:
            messagebox.showerror("SSH", f"Tunnel konnte nicht gestartet werden:\n{exc}")
            return
        self._ssh_status_var.set(
            f"Tunnel aktiv: 127.0.0.1:{cfg.local_port} → "
            f"{cfg.remote_host}:{cfg.remote_port} (via {cfg.user}@{cfg.host})"
        )
        self.status_var.set("SSH-Tunnel gestartet.")
        proc = self._ssh_proc
        self._monitor_thread = threading.Thread(
            target=self._monitor_ssh_process, args=(proc,), daemon=True
        )
        self._monitor_thread.start()

    def _monitor_ssh_process(self, proc: sp.Popen[str]) -> None:
        """Report an SSH process that exits unexpectedly."""
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        returncode = proc.wait()
        self.root.after(0, self._ssh_process_ended, proc, returncode, stderr)

    def _ssh_process_ended(self, proc: sp.Popen[str], returncode: int, stderr: str) -> None:
        if self._ssh_proc is not proc:
            return
        self._ssh_proc = None
        detail = stderr.strip()[:300]
        self._ssh_status_var.set(f"Tunnel beendet (Exit {returncode}). {detail}")
        self.status_var.set("SSH-Tunnel wurde beendet.")

    def _ssh_stop(self) -> None:
        self._stop_ssh_process()
        self._ssh_status_var.set("Tunnel: gestoppt.")
        self.status_var.set("SSH-Tunnel gestoppt.")

    def _stop_ssh_process(self) -> None:
        if self._ssh_proc is None:
            return
        try:
            if sys.platform == "win32":
                self._ssh_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                import os

                os.killpg(os.getpgid(self._ssh_proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            self._ssh_proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._ssh_proc.kill()
            except OSError:
                pass
        self._ssh_proc = None

    def _ssh_test(self) -> None:
        """Quick connectivity test: try to start the tunnel and check exit code."""
        cfg = self._ssh_config()
        if cfg is None:
            return
        error = cfg.validate()
        if error:
            messagebox.showerror("Konfiguration", error)
            return
        try:
            args = build_ssh_tunnel_command(cfg)
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror("SSH", str(exc))
            return
        # Run with a short timeout; if it stays alive, the tunnel works.
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            messagebox.showerror("SSH", str(exc))
            return
        try:
            _stdout, stderr = proc.communicate(timeout=3)
            # If it exited quickly, the connection failed.
            messagebox.showerror(
                "SSH-Test",
                f"Verbindung fehlgeschlagen:\n{stderr.decode('utf-8', 'replace')[:300]}",
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            messagebox.showinfo(
                "SSH-Test", "Tunnel steht (Verbindung erfolgreich, Port-Forward aktiv)."
            )

    # ----- Bot actions --------------------------------------------------- #

    def _start_bot(self) -> None:
        if self._bot_running:
            return
        python = find_bot_python(self._custom_python)
        active = self.store.get_active()
        if active is not None:
            write_control_file(active, CONTROL_PATH)
        env = os.environ.copy()
        env["PANDABOT_GUI_CONTROL"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Start deterministically; the active GUI profile is applied before the
        # first real LLM request.
        env["LLM_BACKEND"] = "local"

        try:
            if sys.platform == "win32":
                self._bot_proc = subprocess.Popen(
                    [python, str(REPO_ROOT / "pandabot.py")],
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=str(REPO_ROOT),
                )
            else:
                self._bot_proc = subprocess.Popen(
                    [python, str(REPO_ROOT / "pandabot.py")],
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=str(REPO_ROOT),
                )
        except OSError as exc:
            messagebox.showerror("Bot starten", str(exc))
            return

        self._bot_running = True
        self._bot_start_btn.config(state=tk.DISABLED)
        self._bot_stop_btn.config(state=tk.NORMAL)
        self._bot_status_var.set(f"Bot läuft (PID {self._bot_proc.pid}).")
        self._bot_log_thread = threading.Thread(target=self._read_bot_output, daemon=True)
        self._bot_log_thread.start()

    def _read_bot_output(self) -> None:
        """Background thread: read bot stdout line by line and append to log."""
        if self._bot_proc is None or self._bot_proc.stdout is None:
            return
        for line in self._bot_proc.stdout:
            self.root.after(0, self._append_log, line.rstrip("\n\r"))
        # Process ended
        self.root.after(0, self._bot_ended)

    def _append_log(self, line: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, line + "\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _bot_ended(self) -> None:
        self._bot_running = False
        self._bot_start_btn.config(state=tk.NORMAL)
        self._bot_stop_btn.config(state=tk.DISABLED)
        self._bot_status_var.set("Bot: beendet.")
        self._bot_proc = None

    def _stop_bot(self) -> None:
        if self._bot_proc is None:
            return
        try:
            if sys.platform == "win32":
                self._bot_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                import os

                os.killpg(os.getpgid(self._bot_proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        # Wait up to 5s, then force kill.
        try:
            self._bot_proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._bot_proc.kill()
            except OSError:
                pass
        self._bot_status_var.set("Bot: gestoppt.")
        self._bot_running = False
        self._bot_start_btn.config(state=tk.NORMAL)
        self._bot_stop_btn.config(state=tk.DISABLED)

    # ----- Cleanup ------------------------------------------------------- #

    def _on_close(self) -> None:
        """Stop all child processes before closing."""
        if self._bot_running:
            self._stop_bot()
        self._stop_ssh_process()
        self.root.destroy()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    custom_python = None
    if len(sys.argv) > 1:
        for i, arg in enumerate(sys.argv):
            if arg == "--python" and i + 1 < len(sys.argv):
                custom_python = sys.argv[i + 1]

    root = tk.Tk()
    app = PandaBotGUI(root)
    app._custom_python = custom_python
    root.mainloop()


if __name__ == "__main__":
    main()
