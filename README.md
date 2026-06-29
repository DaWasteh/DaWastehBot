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
- **Robust:** Ist das LLM-Backend mal weg, schweigt der Bot einfach, statt abzustürzen.

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
[3] Online: Google Gemma 4 26B A4B / MoE (gemma-4-26b-a4b)
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

Der Online-Modus nutzt den OpenAI-kompatiblen Gemini-Endpunkt und schaltet
llama.cpp-spezifische Felder wie `repeat_penalty` automatisch ab. Wenn du VRAM freihalten willst, setze einfach `LLM_BACKEND=online`.

**Gemma-Besonderheiten, die PandaBot automatisch behandelt:**

- Gemma kennt keine echte System-Rolle. Im Online-Profil bettet PandaBot die
  Anweisungen deshalb in die erste User-Nachricht ein (`LLM_USE_SYSTEM_ROLE`
  steht online automatisch auf `false`, lokal bleibt es bei `true`).
- Gemma verlangt strikt abwechselnde user/assistant-Turns. PandaBot legt
  aufeinanderfolgende Chatter-Nachrichten zusammen und sorgt dafür, dass die
  Konversation mit einer User-Nachricht beginnt.
- Gemma schreibt gern interne `<thought>`-Notizen vor der Antwort. PandaBot
  filtert sie heraus; `GOOGLE_LLM_MAX_TOKENS` ist dafür etwas großzügiger
  voreingestellt, damit nach den Gedanken noch echte Antwort übrig bleibt.
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

> **Modell-agnostisch:** Der Bot spricht nur die OpenAI-kompatible
> `chat/completions`-Schnittstelle, das Modell ist also frei wählbar – ob
> MiniCPM, ein LFM-2.5 oder ein größeres Modell, am Code ändert sich nichts,
> nur `LLM_MODEL` bzw. der mit `llama-server` geladene Checkpoint. Wechselst du
> das **Backend** (z. B. auf vLLM), kennt dieses den llama.cpp-eigenen
> `repeat_penalty` evtl. nicht – setze dann `LLM_SEND_REPEAT_PENALTY=false`,
> damit der Parameter weggelassen wird.

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
| `pandabot.py` | Hauptlogik: Bot, LLM-Client, Stream-Kontext, Idle-Routine |
| `config.py` | Lädt die Konfiguration aus `.env` / Umgebungsvariablen |
| `.env.example` | Vorlage für deine Konfiguration |
| `test_pandabot.py` | Tests für Antwort-Aufbereitung und Erwähnungs-Erkennung |
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
| `LLM_USE_SYSTEM_ROLE` | `true` | Anweisungen als System-Rolle senden? Online (Gemma) automatisch `false`; dann landen sie in der ersten User-Nachricht |
| `LLM_MAX_TOKENS` | `80` | Maximale Antwortlänge lokal |
| `LLM_TIMEOUT` | `20` | Sekunden, bis ein lokaler LLM-Aufruf abbricht |
| `GOOGLE_API_KEY` | leer | API-Key für Google AI Studio / Gemini API |
| `GOOGLE_LLM_MODEL` | `gemma-4-31b-it` | Online-Modell |
| `GOOGLE_LLM_MAX_TOKENS` | `200` | Maximale Antwortlänge online (inkl. Puffer für gefilterte `<thought>`-Blöcke) |
| `GOOGLE_LLM_TIMEOUT` | `30` | Timeout für Online-Aufrufe |
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

- **„Pflicht-Variable … fehlt":** Deine `.env` ist unvollständig – vergleiche mit `.env.example`.
- **Bot startet, sagt aber nichts:** Bei `local`: Läuft der `llama-server` unter der `LLM_SERVER_URL`? Bei `online`: Ist `GOOGLE_API_KEY` gesetzt und gültig? Check die Logs auf „LLM-Backend nicht erreichbar".
- **Keine Reaktion im Chat:** Wurde die Autorisierung für **beide** Accounts durchgeführt? Lösche notfalls `.tio.tokens.json` und wiederhole den OAuth-Schritt.
- **Idle-Nachrichten kommen nicht:** Der Bot wird nur aktiv, wenn der Stream **live** ist.
- **Bot gibt Streamtitel, Labels oder Prompt-Schnipsel aus (v. a. online/Gemma):** Sollte mit der aktuellen Version nicht mehr passieren - Titel werden bereinigt, Anweisungen liegen für Gemma in der ersten User-Nachricht. Falls du ein anderes OpenAI-kompatibles Backend nutzt, das System-Messages sauber kann, setze `LLM_USE_SYSTEM_ROLE=true`.

## Lizenz

Frei verwendbar – passe es an deinen Stream an. 🐼