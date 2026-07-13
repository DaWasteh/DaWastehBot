# 🐼 PandaBot

Ein KI-Chatbot für Twitch. PandaBot verbindet deinen Kanal über
[TwitchIO 3](https://twitchio.dev) (EventSub) wahlweise mit einem lokalen
[llama-server](https://github.com/ggml-org/llama.cpp) oder online mit Google
Gemma 4 31B IT, folgt dem Chat, antwortet auf Erwähnungen und sorgt bei Stille
für Unterhaltung.

## Was kann der Bot?

- **Mitlesen & antworten:** Reagiert, wenn jemand den Bot beim Namen nennt – sowohl beim echten Account-Namen (z. B. `@dawastehbot`) als auch bei einem optionalen Spitznamen aus `TWITCH_BOT_NAME`.
- **Chatverlauf als echtes Gespräch:** Die letzten Nachrichten (inklusive der eigenen Antworten) gehen als richtige user/assistant-Turns ans Modell - nicht als Text-Blob. Das hält den Bot im Gesprächsfluss und verhindert, dass Modelle Prompt-Bausteine in den Chat zurückspiegeln.
- **Lokales Profil pro Chatter:** Für jeden Zuschauer legt PandaBot eine Markdown-Datei in `uservault/` an. Nach wenigen Interaktionen fasst ein Hintergrund-Task automatisch zusammen, **wer** die Person ist und **wie** der Bot mit ihr reden soll (Anrede, Interessen, Humor, Distanz, wiederkehrende Wünsche). Beim nächsten Ansprechen fließen diese Notizen unsichtbar in den Prompt ein.
- **Live-Streamkontext:** Aktuelles Spiel und Stream-Titel werden automatisch über die Twitch-API geholt und in den Prompt eingebaut.
- **Interaktiv statt fixer Zyklus:** PandaBot pollt nicht stur alle 60 Sekunden. Der Idle-Task ist **ereignisgesteuert** – er schläft bis die Stille wirklich `IDLE_THRESHOLD` erreicht (+ Jitter) und wird bei jeder echten Chat-Nachricht automatisch auf diesen Zeitpunkt neu gelegt.
- **Stille = Gesprächsstarter, nie Lurker-Callout:** Ist der Chat ruhig (und der Stream live), wirft der Bot von selbst ein konkretes Topic, eine Frage oder Beobachtung in den Raum. Er **spricht niemals Lurker an**, outet niemanden beim Mitlesen und kommentiert keine Zuschauerzahlen.
- **Kein Dauergeschwätz:** Höchstens `IDLE_MAX_SOLO_MESSAGES` eigene Beiträge ohne menschliche Reaktion; danach wartet der Bot auf echten Chat. Opener und ganze Antworten werden gegen frühere Beiträge abgeglichen, damit sich nichts wiederholt.
- **`!panda <frage>`-Befehl:** Direkter Draht zum Bot.
- **Robust:** Ist das LLM-Backend mal weg, schweigt der Bot einfach, statt abzustürzen. Transiente Fehler (Rate-Limit/5xx) werden bei allen HTTP-Backends automatisch kurz wiederholt, und beim Start warnt der Bot klar sichtbar, wenn der lokale llama-server nicht erreichbar ist.

## ⚠️ Wichtig: TwitchIO 3 statt 2

Das ursprüngliche Skript war für **TwitchIO 2** geschrieben (IRC, `initial_channels`,
`channel.send`). Diese Version nutzt **TwitchIO 3**, wo IRC entfernt und durch
**EventSub** ersetzt wurde. Das bedeutet ein anderes Auth-Modell: Statt eines
einzelnen Chat-Tokens brauchst du eine **Twitch-Application** (Client-ID/Secret),
und Bot- sowie Kanal-Account autorisieren sich einmalig über den Browser. Die
Tokens werden danach automatisch in `.tio.tokens.json` gespeichert und erneuert –
du musst das nur **einmal** machen.

## Voraussetzungen

- **Python 3.11 oder neuer** (TwitchIO 3 verlangt das)
- Entweder ein laufender **llama-server** mit deinem Modell oder ein **Google AI Studio API-Key**
- Zwei Twitch-Accounts: dein **Kanal** und ein separater **Bot-Account** (empfohlen)

## Installation

```bash
git clone <dein-repo>
cd pandabot

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Konfiguration

### 1. Twitch-Application anlegen

1. Gehe zur [Twitch Developer Console](https://dev.twitch.tv/console) → **Register Your Application**.
2. Trage als **OAuth Redirect URL** exakt ein: `http://localhost:4343/oauth/callback`
3. Notiere dir **Client ID** und **Client Secret**.

### 2. User-IDs herausfinden

Du brauchst die **numerischen IDs** (nicht die Namen) deines Kanals und des
Bot-Accounts. Anleitung dazu steht in den
[TwitchIO-FAQ](https://twitchio.dev/en/latest/getting-started/faq.html#bot-id-owner-id).

### 3. `.env` erstellen

Kopiere die Vorlage und trage deine Werte ein:

```bash
cp .env.example .env
```

```env
TWITCH_CLIENT_ID=deine_client_id
TWITCH_CLIENT_SECRET=dein_client_secret
TWITCH_BOT_ID=123456789       # User-ID des Bot-Accounts
TWITCH_OWNER_ID=987654321     # Deine eigene User-ID (der Kanal)
TWITCH_CHANNEL=dawasteh
TWITCH_BOT_NAME=PandaBot      # optionaler Spitzname, auf den der Bot zusätzlich hört

LLM_BACKEND=ask   # fragt beim Start: lokal oder online?
LLM_SERVER_URL=http://127.0.0.1:1235/v1/chat/completions

# Optional für Online-Modus mit Google Gemma 4 31B IT:
GOOGLE_API_KEY=dein_google_ai_studio_key
```

Alle weiteren Werte (Temperatur, Idle-Zeit, Google-Modell usw.) haben sinnvolle
Defaults und sind in `.env.example` dokumentiert.

> **Wie hört der Bot auf seinen Namen?** Der Bot erkennt seinen **echten
> Twitch-Account-Namen** automatisch (er löst ihn beim Start aus der
> `TWITCH_BOT_ID` auf) – `@dawastehbot` oder einfach `dawastehbot` im Chat
> sprechen ihn also direkt an. `TWITCH_BOT_NAME` ist ein **zusätzlicher**
> Spitzname (z. B. „PandaBot"), auf den er ebenfalls reagiert und der in
> Prompts/Logs auftaucht. Beides wird als eigenständiges Wort erkannt, damit
> nicht zufällige Wortbestandteile fälschlich triggern.

## Einmalige Autorisierung (OAuth)

Diesen Ablauf musst du **nur einmal** durchführen (oder wenn du die Scopes änderst):

1. **Starte den Bot:**
   ```bash
   python pandabot.py
   ```
   Er startet im Hintergrund einen kleinen Webserver auf Port `4343`.

2. **Bot-Account autorisieren:** Öffne ein **Inkognito-Fenster**, logge dich dort
   als dein **Bot-Account** ein und rufe auf:
   ```
   http://localhost:4343/oauth?scopes=user:read:chat%20user:write:chat%20user:bot&force_verify=true
   ```

3. **Kanal autorisieren:** In deinem **normalen Browser** (eingeloggt als dein
   **Kanal-Account**) rufe auf:
   ```
   http://localhost:4343/oauth?scopes=channel:bot&force_verify=true
   ```

Fertig. Ab jetzt verbindet sich der Bot bei jedem Start automatisch – die Tokens
liegen in `.tio.tokens.json` und werden selbstständig erneuert.

## LLM auswählen

Beim Start fragt PandaBot standardmäßig:

```text
[1] Lokal: llama-server
[2] Online: Google Gemma 4 31B IT (gemma-4-31b-it)
[3] Online: Google Gemma 4 26B A4B / MoE (gemma-4-26b-a4b-it)
```

Mit `LLM_BACKEND=local`, `LLM_BACKEND=online` (Default-Modell aus `.env`) oder
`LLM_BACKEND=online-a4b` (MoE-Alternative) in der `.env` kannst du die Abfrage
überspringen. Für den Online-Modus brauchst du einen API-Key aus
[Google AI Studio](https://aistudio.google.com/app/apikey):

```env
GOOGLE_API_KEY=dein_google_ai_studio_key
GOOGLE_LLM_MODEL=gemma-4-31b-it
```

Wichtig: Die API-Modell-ID heißt `gemma-4-31b-it`, auch wenn sie über die Gemini API läuft. Falls du versehentlich `gemini-4-31b-it` einträgst, normalisiert PandaBot das beim Start automatisch auf `gemma-4-31b-it`.

Der Online-Modus nutzt den **nativen `generateContent`-Endpunkt** (nicht den
OpenAI-Kompatibilitäts-Shim, der für die MoE-Variante `gemma-4-26b-a4b-it`
aktuell HTTP 500 liefert) und schaltet llama.cpp-spezifische Felder wie
`repeat_penalty` automatisch ab. Wenn du VRAM freihalten willst, setze einfach `LLM_BACKEND=online`.

**Gemma-Besonderheiten, die PandaBot automatisch behandelt:**

- Gemma 4 unterstützt System Instructions über den nativen `generateContent`-
  Endpunkt. Der System-Prompt wird als `systemInstruction` geschickt.
- Gemma verlangt strikt abwechselnde user/assistant-Turns. PandaBot legt
  aufeinanderfolgende Chatter-Nachrichten zusammen und sorgt dafür, dass die
  Konversation mit einer User-Nachricht beginnt.
- Gemma schreibt gern interne `<thought>`-Notizen vor der Antwort. PandaBot
  filtert sie heraus (sowohl `<think>` als auch `<thought>`-Tags sowie die
  nativen `thought: true`-Parts von `generateContent`);
  `GOOGLE_LLM_MAX_TOKENS` ist dafür großzügig voreingestellt, damit nach den
  Gedanken noch echte Antwort übrig bleibt.
- Der Stream-Titel wird vor jedem Prompt von Tags, Emotes und `!commands`
  bereinigt, damit das Modell ihn nicht wörtlich in den Chat kopiert.

## Lokalen LLM-Server starten

Beispielhaft mit `llama-server` aus llama.cpp (Port an deine `.env` anpassen):

```bash
llama-server -m /pfad/zu/deinem-modell.gguf --port 1235 -c 4096
```

> **Tipp für kleine Modelle (~1B):** PandaBot setzt bereits Stop-Strings und eine
> `repeat_penalty`, damit sich das Modell nicht selbst als andere Chatter
> halluziniert oder im Kreis dreht. Falls die Antworten zu wiederholend wirken,
> dreh `LLM_REPEAT_PENALTY` in der `.env` schrittweise hoch (z. B. auf `1.2`).
> Werden sie zu wirr, senke `LLM_TEMPERATURE`.

> **Modell-agnostisch:** Lokal und bei den meisten API-Anbietern nutzt der Bot
> die OpenAI-kompatible `chat/completions`-Schnittstelle; Google läuft wegen
> Gemma 4 nativ über `generateContent`, Abo-Profile über offizielle CLIs.
> Beim lokalen Server ist der Checkpoint frei wählbar. Wechselst du das
> **Backend** (z. B. auf vLLM), kennt dieses den llama.cpp-eigenen
> `repeat_penalty` evtl. nicht – setze dann `LLM_SEND_REPEAT_PENALTY=false`,
> damit der Parameter weggelassen wird.

## LLM Control GUI (`pandabot_gui.py`)

Eine plattformübergreifende (Windows/Linux) tkinter-GUI zur Verwaltung aller
LLM-Backends, On-the-fly-Modellwechsel, Abo/CLI-Logins und SSH-Tunneln.

Starten:

```bash
# Windows
start_gui.bat
# Linux/macOS
./start_gui.sh
# oder direkt
python pandabot_gui.py
```

Die GUI nutzt tkinter plus das moderne plattformübergreifende Sun-Valley-Theme
`sv-ttk` (wird vom Launcher bei Bedarf aus `requirements-gui.txt` installiert).
Auf manchen Linux-Distributionen zusätzlich: `sudo apt install python3-tk`.
Die Launcher bevorzugen
für die GUI `.venv-gui`, danach `.venv`; der Bot selbst wird weiterhin mit
seiner Repo-`.venv` gestartet. Optional getrennt anlegen:

```bash
# Windows
py -3.12 -m venv .venv-gui
.venv-gui\Scripts\python -m pip install -r requirements-gui.txt
# Linux
python3 -m venv .venv-gui
.venv-gui/bin/python -m pip install -r requirements-gui.txt
```

### API-Profile

- Provider-Presets: Local llama.cpp, Google native, OpenAI, OpenRouter, Groq,
  Mistral, xAI, **Z.AI (GLM)**, **Z.AI Coding-Plan**, Custom OpenAI-compatible.
- Pro Profil: Provider, maskierter API-Key, Endpoint, Modell-Dropdown
  (editierbar), Temperatur/TopP/MaxTokens/Timeout/Systemrolle.
- **„Modelle laden“** ruft pro API-Key `/models` ab und befüllt das Dropdown.
  Bei Google native werden nur `generateContent`-fähige Modelle gezeigt.
  Für Z.AI werden GLM-Vorschläge (`glm-4.7`, `glm-4.6`, …) vorbefüllt.
- **Z.AI:** `zai` nutzt die normale GLM-API
  (`https://api.z.ai/api/paas/v4/...`), `zai_coding` den Endpoint des
  GLM-Coding-Plan-Abos (`https://api.z.ai/api/coding/paas/v4/...`) – beide mit
  demselben API-Key (`ZAI_API_KEY` in der `.env` als Fallback).
- API-Keys werden in einer lokalen, atomar geschriebenen `llm_profiles.json`
  gespeichert (gitignored, best-effort `0600`). `.env`-Keys sind Fallback.
  Die Control-Datei für den Profilwechsel enthält **keinen** API-Key; der Bot
  löst den Key selbst über `llm_profiles.json`/`.env` auf.

### On-the-fly Modellwechsel

- **„Profil aktivieren“** schreibt eine atomare Control-Datei
  (`.pandabot_llm_control.json`).
- Der Bot läuft mit `PANDABOT_GUI_CONTROL=1` und übernimmt das Profil vor
  dem nächsten LLM-Request.
- Die aiohttp-Session wird bei Endpoint/Timeout-Wechsel erneuert.

### Bot starten/stoppen

- Die GUI startet den Bot im Repo-`.venv` (bevorzugt) oder mit dem
  System-Python.
- stdout/stderr werden live im „Bot“-Tab angezeigt.
- Sauberer Shutdown: Windows `CTRL_BREAK_EVENT`, POSIX `SIGTERM` → `SIGKILL`.

### Abo-Modelle via offizieller CLIs

Die GUI bietet pro Anbieter **Installieren**, **Login**, ein editierbares
Modell-Dropdown und **Aktivieren**. **„Alle offiziellen CLIs installieren“**
installiert mit Node.js 22+/npm ausschließlich die offiziellen Pakete:
`@anthropic-ai/claude-code`, `@openai/codex`, `@google/gemini-cli` und
`@github/copilot`. Danach öffnet **Login** den jeweiligen offiziellen
Browser-/OAuth-Flow. Bei Codex lädt **„Modelle laden“** den Modellkatalog
des angemeldeten Kontos (`codex debug models`) dynamisch. Bei CLIs ohne
offiziellen List-Models-Befehl bleiben dokumentierte Aliase/Vorschläge sowie
manuelle Eingaben verfügbar.

Die Login-Buttons öffnen ein Terminal mit dem offiziellen CLI:

| CLI | Login | Headless-Aufruf (read-only) |
|-----|-------|-----------------------------|
| Claude Code (`claude`) | `/login` in interaktiver Session | `--bare -p --tools "" --no-session-persistence`, Prompt via stdin |
| OpenAI Codex (`codex`) | `codex login` | `exec --sandbox read-only --json -`, Prompt via stdin |
| Gemini CLI (`gemini`) | „Sign in with Google“ | `--output-format json --sandbox`, Prompt via stdin |
| Copilot CLI (`copilot`) | `/login` in interaktiver Session | `-p --deny-tool=write --deny-tool=shell` |

Der Prompt (mit ungefiltertem Twitch-Chat) geht bei Claude/Codex/Gemini über
**stdin** statt als Kommandozeilen-Argument. Das verhindert, dass cmd.exe bei
npm-`.cmd`-Shims unter Windows Metazeichen aus Chat-Nachrichten interpretiert
(„BatBadBut“). Für Copilot (argv-Prompt) werden bei `.cmd`/`.bat`-Shims alle
cmd-Metazeichen aus dem Prompt entfernt.

Consumer-Abo ≠ API. Die CLIs laufen über dein bereits angemeldetes Abo;
die Browser-/OAuth-Verbindung läuft per HTTPS im jeweiligen offiziellen CLI.
Verfügbarkeit und Modelle hängen vom Abo ab. Jeder Bot-Request läuft in einem
leeren temporären Arbeitsordner und mit den konservativsten verfügbaren Flags
(tool-disabled/read-only/plan/deny). Coding-CLIs bleiben trotzdem Agenten:
Globale MCP-Server, Extensions und Account-Tools solltest du im jeweiligen CLI
zusätzlich deaktivieren; nur Claude bietet hier eine vollständige `--tools ""`-
Abschaltung. PandaBot vergibt niemals `--allow-all-tools`.

### SSH-Tunnel

Forward einen lokalen Port an einen entfernten OpenAI-kompatiblen Endpoint:

```
ssh -N -L 127.0.0.1:LOCAL:REMOTE_HOST:REMOTE_PORT -p PORT [-i key] user@host
```

- `BatchMode=yes` (nur Key-Auth, keine Passwort-Prompts)
- `ExitOnForwardFailure=yes` (schnelles Fehlschlagen bei belegtem Port)
- Private Schlüssel werden nie kopiert; die GUI hält nur den ausgewählten Pfad.
- Danach ein Custom-OpenAI-Profil mit `http://127.0.0.1:LOCAL/v1/chat/completions`
  anlegen, Modelle laden und aktivieren.
- Der SSH-Host muss vorher einmal vertrauenswürdig in `known_hosts` bestätigt
  worden sein; `BatchMode=yes` öffnet absichtlich keine Passwortabfrage.

## Benutzung

Ist ein LLM-Backend ausgewählt/erreichbar und der Bot gestartet, läuft alles automatisch:

- Schreibe im Chat `PandaBot, wie geht's?` → der Bot antwortet.
- Nutze `!panda Was hältst du vom Spiel?` für einen direkten Befehl.
- Bleibt der Chat ruhig, meldet sich der Bot nach `IDLE_THRESHOLD` Sekunden mit einem Gesprächsaufhänger. Er **spricht dabei keine Lurker an**, sondern startet ein Topic. Danach wartet er bei `IDLE_MAX_SOLO_MESSAGES=1` auf echte Chat-Aktivität, statt allein weiterzureden.
- Wer den Bot öfter anspricht, bekommt automatisch ein reichhaltigeres Profil – die nächste Antwort passt sich in Ton und Inhalt an.

## Chatter-Profile (`uservault/`)

Pro Twitch-User-ID liegt eine Markdown-Datei mit:

- Anzeigename & Interaktionszähler,
- einem **Sprachprofil** (automatische Zählung der erkannten Sprachen),
- und **Notizen**, die der Hintergrund-Task nach `PROFILE_SUMMARY_AFTER` bzw.
  `PROFILE_SUMMARY_INTERVAL` Interaktionen konsolidiert: Anrede, Interessen,
  Humor & Stil, Distanz/Ton, wiederkehrende Wünsche, Besonderheiten.

Die Notizen sind „stille Hintergrundnotizen“ – sie fließen in Antworten ein,
werden aber nie erwähnt, zitiert oder aufgezählt. Explizite „merk dir …“-Bitten
werden zusätzlich sofort erfasst. Mit `USER_MEMORY_ENABLED=false` schaltest du
alles aus, mit `PROFILE_SUMMARY_AFTER=0` nur die automatische Zusammenfassung.

## Projektstruktur

| Datei | Zweck |
|-------|-------|
| `pandabot.py` | Hauptlogik: Bot, LLM-Client (OpenAI/Google native/CLI), Stream-Kontext, Idle-Routine |
| `config.py` | Lädt die Konfiguration aus `.env` / Umgebungsvariablen |
| `llm_profiles.py` | Provider-Presets, Profil-Store, Modell-Normalisierung, Control-Datei |
| `cli_backends.py` | CLI-Transporte (Claude/Codex/Gemini/Copilot), SSH-Tunnel, Modell-Listen |
| `pandabot_gui.py` | Cross-platform GUI für Profil-Verwaltung, Abo/CLI, SSH, Bot-Control |
| `start_gui.bat` / `start_gui.sh` | GUI starten (Windows / Linux) |
| `requirements-gui.txt` | GUI-Requirements (nur Stdlib nötig) |
| `.env.example` | Vorlage für deine Konfiguration |
| `tests/test_pandabot.py` | Tests für Antwort-Aufbereitung, Erwähnungs-Erkennung, Profile, CLI |
| `conftest.py` | Pytest-Setup (Dummy-Env), damit Tests eigenständig laufen |
| `.github/workflows/ci.yml` | CI: Lint (ruff) + Tests auf Python 3.11–3.13 |

## Konfigurationsreferenz

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `LLM_BACKEND` | `ask` | `ask`, `local` oder `online` |
| `LLM_TEMPERATURE` | `0.7` | Kreativität (höher = bunter, wirrer) |
| `LLM_TOP_P` | `0.9` | Nucleus-Sampling |
| `LLM_REPEAT_PENALTY` | `1.15` | Strafe gegen Wiederholungen (llama.cpp) |
| `LLM_SEND_REPEAT_PENALTY` | `true` | `repeat_penalty` mitschicken? Online wird automatisch deaktiviert |
| `LLM_SEND_LLAMA_EXTRAS` | `true` | llama.cpp-Thinking-Extras mitschicken? Online wird automatisch deaktiviert |
| `LLM_USE_SYSTEM_ROLE` | `true` | Anweisungen als System-Rolle senden? Gemma 4 nutzt nativ `systemInstruction`; für inkompatible Custom-Backends auf `false` setzen |
| `LLM_MAX_TOKENS` | `80` | Maximale Antwortlänge lokal |
| `LLM_TIMEOUT` | `20` | Sekunden, bis ein lokaler LLM-Aufruf abbricht |
| `GOOGLE_API_KEY` | leer | API-Key für Google AI Studio / Gemini API |
| `GOOGLE_LLM_MODEL` | `gemma-4-31b-it` | Online-Modell |
| `GOOGLE_LLM_MAX_TOKENS` | `512` | Maximale Antwortlänge online (inkl. Puffer für gefilterte `<thought>`-Blöcke) |
| `GOOGLE_LLM_TIMEOUT` | `45` | Timeout für Online-Aufrufe |
| `HISTORY_LENGTH` | `16` | Wie viele Verlaufs-Turns als Kontext dienen (inkl. eigener Bot-Antworten) |
| `IDLE_THRESHOLD` | `900` | Sekunden Stille bis zur Eigeninitiative (ereignisgesteuert, kein Poll-Zyklus) |
| `IDLE_JITTER` | `90` | Max. zufällige Sekunden, die zum Threshold addiert werden |
| `IDLE_MAX_SOLO_MESSAGES` | `1` | Max. eigene Idle-Nachrichten ohne neue echte Chat-Nachricht (`0` deaktiviert Idle) |
| `CONTEXT_TTL` | `120` | Cache-Dauer für Titel/Spiel in Sekunden |
| `USER_MEMORY_ENABLED` | `true` | Lokale Chatter-Profile an/aus |
| `USER_MEMORY_DIR` | `uservault` | Ordner für die Profil-Dateien |
| `PROFILE_SUMMARY_AFTER` | `2` | Nach so vielen Interaktionen: erste Profil-Zusammenfassung (`0` = Auto-Summary aus) |
| `PROFILE_SUMMARY_INTERVAL` | `5` | Alle X weiteren Interaktionen wird das Profil aufgefrischt |
| `PROFILE_INTERACTIONS_KEPT` | `8` | Wie viele letzte (User,Bot)-Gespräche pro User im Speicher bleiben |
| `PROFILE_MAX_NOTES` | `10` | Max. Notiz-Bullets im Profil nach einer Consolidation |

## Sicherheit

`.env`, `.tio.tokens.json` und die alte `config.json` stehen in `.gitignore` und
dürfen **niemals** committet werden – sie enthalten deine Secrets bzw. Tokens.

## Fehlersuche

- **„CLI fehlt / nicht im PATH":** Das Anbieter-CLI ist noch nicht installiert. Im Tab **Abos & Login** zuerst **Installieren** oder **Alle offiziellen CLIs installieren** wählen; danach **Login**. Node.js 22+ und npm müssen vorhanden sein.
- **„Pflicht-Variable … fehlt":** Deine `.env` ist unvollständig – vergleiche mit `.env.example`.
- **Bot startet, sagt aber nichts:** Bei `local`: Läuft der `llama-server` unter der `LLM_SERVER_URL`? Bei `online`: Ist `GOOGLE_API_KEY` gesetzt und gültig? Check die Logs auf „LLM-Backend nicht erreichbar".
- **Keine Reaktion im Chat:** Wurde die Autorisierung für **beide** Accounts durchgeführt? Lösche notfalls `.tio.tokens.json` und wiederhole den OAuth-Schritt.
- **Idle-Nachrichten kommen nicht:** Der Bot wird nur aktiv, wenn der Stream **live** ist.
- **Bot gibt Streamtitel, Labels oder Prompt-Schnipsel aus (v. a. online/Gemma):** Sollte mit der aktuellen Version nicht mehr passieren - Titel werden bereinigt, Anweisungen liegen für Gemma in der ersten User-Nachricht. Falls du ein anderes OpenAI-kompatibles Backend nutzt, das System-Messages sauber kann, setze `LLM_USE_SYSTEM_ROLE=true`.

## Lizenz

Frei verwendbar – passe es an deinen Stream an. 🐼