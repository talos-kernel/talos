# Claude-Worker (begrenzte Coding-Jobs hinter einem eigenen Daemon)

Talos kann Coding-Aufgaben an einen lokalen Claude-Code-Worker **delegieren**:
ein eigener Daemon (`talos/claudeworker.py`) läuft als Benutzer `talos-claude`,
nimmt Jobs über einen Unix-Socket entgegen und führt jeden Job als
`claude -p --output-format stream-json` unter den bestehenden Sandbox-Backends
aus — schreiben darf der Job nur in seinem eigenen, kernel-abgeleiteten
Workspace.

Die Funktion ist eine **Installations-Entscheidung** und standardmässig **aus**
(`TALOS_CLAUDE_WORKER_ENABLED`, Default `"0"`): ohne laufenden Worker und ohne
gesetzten Socket existieren die beiden Werkzeuge `delegate_code` und
`delegate_status` im Agenten gar nicht.

Wichtig für das Vertrauensmodell: die Delegation ist **eine** kernel-gegate
Aktion mit der `run_shell`-Vertrauensform (Effect.EXEC, `sandbox_required`,
Ziel = die Worker-Root, nie ein Modell-Pfad). Was der delegierte Claude *innerhalb*
des Jobs tut, ist nicht mehr pro Aktion gegatet — es ist durch die Confinement-
Form begrenzt (kein Schreiben ausserhalb des Job-Workspaces, Root read-only,
Gesamt-Deadline pro Job). Wer das nicht tragen will, installiert den Worker
nicht.

## Protokoll

JSON-Lines über `/run/talos/claude.sock`, eine Anfrage pro Verbindung:

```
→  {"op": "submit", "job_id": "…", "prompt": "…", "workspace": "…",
    "browser_mcp": false, "mcp_servers": ["chrome-devtools"]}
←  {"ok": true, "state": "accepted"}
→  {"op": "status", "job_id": "…"}
←  {"ok": true, "state": "done", "summary": "…", "files": ["…"], "returncode": 0}
←  {"ok": false, "kind": "busy", "message": "…"}
```

`state` ist `accepted | running | done | failed | timeout`; Fehler-`kind` ist
`invalid_request | unknown_job | busy | unavailable`. `summary` kommt aus dem
`result`-Event des stream-json-Stroms, `files` aus `tool_use`-Events mit Pfaden
**innerhalb** des Workspaces — Belege aus dem Strom, nie aus Prosa, und ein
behaupteter Pfad ausserhalb des Jails wird verworfen, nicht umgeschrieben.
Kaputte Frames kosten die Verbindung, nie den Daemon. Zugriffskontrolle ist die
Dateisystem-Permissionierung (Socket `0660` + Gruppe), wie beim Modell-Worker —
ein Bearer-Token wäre ein Geheimnis mehr, das aus einer Kind-Umgebung
herausgehalten werden müsste, die keine sehen darf.

## Installation (Linux, systemd)

```bash
# 1. Benutzer und Gruppe; der Agent (talos) kommt ueber die GRUPPE an den Socket.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin talos-claude
sudo usermod -aG talos-claude talos

# 2. Worker-Root (Job-Workspaces) und dediziertes HOME (nur Claude-OAuth-State).
sudo install -d -m 0750 -o talos-claude -g talos-claude /var/lib/talos/claude-jobs
sudo install -d -m 0700 -o talos-claude -g talos-claude /var/lib/talos/claude-home
#   Claude-Login einmalig als Worker-User einrichten:
sudo -u talos-claude HOME=/var/lib/talos/claude-home claude auth login

# 3. Env-Datei des Workers — er liest NUR diese, nie talos.env ("der Worker
#    soll weniger wissen als der Agent").
sudo install -d -m 0750 -o root -g talos-claude /etc/talos
sudo install -m 0640 -o root -g talos-claude /dev/null /etc/talos/claude-worker.env
sudoedit /etc/talos/claude-worker.env
#   TALOS_CLAUDE_WORKER_ROOT=/var/lib/talos/claude-jobs
#   TALOS_CLAUDE_WORKER_HOME=/var/lib/talos/claude-home
#   TALOS_CLAUDE_WORKER_BIN=/usr/local/bin/claude
#   TALOS_CLAUDE_WORKER_MAX_PARALLEL=2        # optional, Default 2
#   TALOS_CLAUDE_WORKER_JOB_TIMEOUT=900       # optional, Default 900

# 4. Unit installieren und starten (ReadWritePaths ggf. an eigene Pfade anpassen).
sudo install -m 0644 deploy/talos-claude-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now talos-claude-worker.service
```

Das Verzeichnis `/run/talos` legt systemd an (`RuntimeDirectory=`): Eigentümer
`talos-claude:talos-claude`, Mode 0750; der Socket darin bekommt Mode 0660.

## Agent-Seite

In der Unit (oder Umgebung) des **Agenten**:

```
Environment=TALOS_CLAUDE_WORKER_ENABLED=1
Environment=TALOS_CLAUDE_WORKER_SOCKET=/run/talos/claude.sock
Environment=TALOS_CLAUDE_WORKER_ROOT=/var/lib/talos/claude-jobs
# Optional, zweites Backend (siehe unten): Environment=TALOS_AGY_BACKEND=1
```

⚠️ Diese drei Schlüssel gehören in die **Prozess-Umgebung** des Agenten (die
`Environment=`-Zeilen seiner Unit), nicht nur in eine Secrets-Datei: der Kernel
prüft `requires_env` gegen `os.environ` und `policy.claude_work_root()` liest
die Root selbst aus der Umgebung — bewusst ohne Umweg über `config.py` (das
`TALOS_MODEL_WORKER`-Muster). Wer sie nur in die Secrets-Datei schreibt,
bekommt ein sauberes, aber unvermeidliches `DENY: required env not set` —
gemessen am ersten E2E dieses Releases.

Erst dann existieren die Werkzeuge. `delegate_code {"prompt": "…"}` reicht einen
begrenzten Auftrag ein und gibt eine `job_id` zurück; `delegate_status
{"job_id": "…"}` liest Stand und Ergebnis. Der Kernel entscheidet wie immer:
`delegate_code` ist Effect.EXEC mit `sandbox_required`, das Extraktor-Ziel ist
die Worker-Root (`policy.claude_work_root()`), und der Job-Workspace wird
kernel-seitig aus der `job_id` abgeleitet (`policy.claude_job_workspace()`) —
kein Modell-Argument wählt je, wohin ein delegierter Claude schreibt.
Unbeaufsichtigte Deckel (unattended ceilings) verschärfen wie bisher.

## MCP-Server im Job (generisch, operator-owned)

Über den Browser-Sonderfall hinaus kann ein Job weitere MCP-Server mitbekommen
— derselbe Mechanismus, verallgemeinert: Talos spricht selbst **nie** MCP, das
`claude -p`-Kind im Job tut es; Talos erzeugt nur die MCP-Konfiguration und
reicht sie per `--mcp-config` durch. Der Frame trägt **nur Namen**, niemals
command/args.

Die Wahrheit liegt in der Registry-Datei `data/mcp-servers.json`
(operator-owned, gitignored, fail-closed wie `entities.json`):

```json
{
  "version": 1,
  "servers": [
    {
      "name": "chrome-devtools",
      "command": "/usr/local/bin/chrome-devtools-mcp",
      "package": "chrome-devtools-mcp@latest",
      "args": ["--headless=true", "--executablePath=/usr/bin/chromium"],
      "description": "Browser-Automatisierung im Job; command leer = Start per npx <package>"
    },
    {
      "name": "filesystem",
      "command": "/usr/local/bin/mcp-server-filesystem",
      "args": ["/var/lib/talos/claude-jobs"],
      "description": "Dateizugriff, auf die Worker-Root gerootet — NICHT weiter"
    }
  ]
}
```

Regeln des Parsers (`talos/mcpservers.py`), hart und fail-closed: `version: 1`
Pflicht; Datei fehlt/kaputt/zu gross → **leere** Registry; ein ungültiger
Eintrag fällt einzeln heraus. Namen müssen `[a-z0-9-]{1,32}` genügen,
`command` muss ein **absoluter** Pfad sein (oder leer — dann startet
`npx <package>`), `args` ist eine gedeckelte String-Liste, und ein
**`env`-Feld kostet den ganzen Eintrag**: der MCP-Server erbt exakt das
minimierte Job-Env, eine eigene Env-Tabelle wäre ein zweiter, ungeprüfter
Credential-Weg. Beim filesystem-Beispiel oben: als Wurzel gehört die
Worker-Root (oder ein Pfad darunter) in die args — der Job kann ohnehin nur
dort schreiben; eine weiter gefasste Wurzel gäbe dem Kind Lesezugriff, den die
Sandbox-Form nicht vorsieht.

Zwei Gates, wie beim Browser — beide müssen den Server nennen, sonst wird die
Anforderung **benannt abgelehnt** und kein Job startet:

- **Agent** (`Environment=` der Agent-Unit): `TALOS_MCP_SERVERS=filesystem,…`
  — erlaubt ist die *Schnittmenge* mit der Registry.
  `TALOS_BROWSER_MCP_ENABLED=1` impliziert weiterhin `chrome-devtools`;
  Bestandskonfigurationen laufen unverändert weiter.
- **Worker** (`/etc/talos/claude-worker.env`):
  `TALOS_CLAUDE_WORKER_MCP_SERVERS=filesystem,…` und
  `TALOS_CLAUDE_WORKER_MCP_REGISTRY=/home/…/talos/data/mcp-servers.json`
  (Pfad zur Registry — der Worker importiert `config.py` nicht und findet die
  Datei nur über diese Variable). `TALOS_CLAUDE_WORKER_BROWSER_MCP=1`
  impliziert weiterhin `chrome-devtools`; fehlt der Registry-Eintrag dafür,
  baut der Worker die Konfiguration wie bisher aus den
  `TALOS_CLAUDE_WORKER_BROWSER_*`-Schlüsseln (Env-Synthese).

Im Agenten fordert das Modell Server über `delegate_code {"prompt": "…",
"mcp": ["filesystem"]}` an; `browser: true` bleibt der Alias für
`mcp: ["chrome-devtools"]`. Pro angefragtem Server bekommt `--allowedTools`
genau ein `mcp__<name>`-Präfix — nie mehr als angefragt wurde.

## Zweites Backend: agy (Antigravity CLI)

Derselbe Daemon, derselbe Socket, dieselbe Confine-Form — aber der Job muss
nicht `claude` sein. Der Submit-Frame trägt optional ein `"backend"`: fehlt
es oder steht `"claude"`, gilt alles oben Bit für Bit; `"agy"` waehlt die
Antigravity-CLI (`agy -p --output-format stream-json
--dangerously-skip-permissions --print-timeout <timeout>s`, gemessen an
Binary 1.1.22). Jeder andere Wert ist `invalid_request` — benannt, und es
startet kein Job. MCP und Browser bleiben dem claude-Backend vorbehalten:
ein agy-Frame mit `mcp_servers`/`browser_mcp` ist `invalid_request`.

Zwei eigene Gates, wie überall hier — beide müssen ja sagen:

- **Worker** (`/etc/talos/claude-worker.env`):
  `TALOS_CLAUDE_WORKER_AGY_BIN=/usr/local/bin/agy` (absolut, existierend)
  und `TALOS_CLAUDE_WORKER_AGY_HOME=/var/lib/talos/agy-home` (dediziertes
  agy-HOME mit dem OAuth-Login). Fehlt eines oder zeigt es ins Leere, gibt
  es das Backend nicht: ein agy-Frame wird mit `unavailable` beantwortet,
  kein Job startet.
- **Agent** (`Environment=` der Agent-Unit): `TALOS_AGY_BACKEND=1` — ohne
  es existiert `delegate_agy` nicht im Manifest (dieselbe Zwei-Gate-Form
  wie `TALOS_MCP_SERVERS`); `delegate_status` gilt für beide Backends und
  nennt das `backend` des Jobs in der Antwort (Vorgabe `"claude"`).

Installation und Login (einmalig, als Worker-User — agy startet dabei den
Google-OAuth-Flow und legt Token und State unter
`$HOME/.gemini/antigravity-cli/` ab):

```bash
sudo install -d -m 0700 -o talos-claude -g talos-claude /var/lib/talos/agy-home
sudo -u talos-claude HOME=/var/lib/talos/agy-home agy
```

Die Credential-Form unterscheidet sich ehrlich vom claude-Backend: agy
kennt keinen Env-Token wie `CLAUDE_CODE_OAUTH_TOKEN` — die Token-**Datei**
muss mit. Der Worker kopiert darum pro Job `antigravity-oauth-token` aus
dem Worker-agy-HOME nach `<job-workspace>/.home/.gemini/antigravity-cli/`
(Mode 0600), bevor das Kind startet; das Worker-agy-HOME selbst taucht
weder in der Job-Env noch in argv auf. Fehlt der Token, ist die Antwort
`unavailable` — benannt, kein Spawn.

Und der Returncode luegt: der gemessene Auth-Fehler kommt als
`{"event":"result","result":{"status":"ERROR","error":"authentication
failed or timed out", …}}` **mit RC 0**. Darum zaehlt der Strom, nicht der
Exit-Code: ein `result`-Event mit nicht-leerem `error` oder einem Status
ausserhalb der OK-Menge ({"OK","DONE","SUCCESS"}, gross-/kleinschreibungs-
tolerant gelesen) macht den Job `failed`, egal was der Prozess noch
behauptet. agy kennzeichnet seine Events mit `event` statt `type` und
verschachtelt das Abschluss-Event — die Summary kommt aus
`.result.response`. Welche tool-Events agy im Erfolgsfall wirklich sendet,
zeigt erst der Live-E2E: der Parser liest claude- UND plausibel
agy-foermige Events, Dateipfade gelten nur innerhalb des Workspaces
(verworfen, nie umgeschrieben — wie bisher), und wo nichts passt, ist
`files` eben leer: Belege duerfen fehlen, sie werden nie erfunden.

## Fail-closed-Verhalten

- Socket unerreichbar oder kaputt → benannter `unavailable`-Fehler im
  Tool-Ergebnis. Es gibt **keinen** stillen Rückfall, der `claude` im
  Agent-Prozess selbst startete.
- `TALOS_CLAUDE_WORKER_ENABLED` unset/`0` → die Werkzeuge stehen nicht im
  Manifest; eine Delegation scheitert am Kernel, nicht erst am Socket.
- `TALOS_AGY_BACKEND` unset/`0` → `delegate_agy` steht nicht im Manifest;
  und selbst mit agent-seitigem Gate gilt das Worker-Gate
  (`TALOS_CLAUDE_WORKER_AGY_BIN`/`_AGY_HOME` samt Token): ohne es ist die
  Antwort `unavailable` — benannt, kein Spawn.
- `requires_env` unerfüllt (kein Socket konfiguriert) → kein Grant, der Runner
  läuft nie (Red-Team-Fall).
- Unconfined ist verweigert: die Job-Backend-Auswahl filtert das
  `unconfined`-Backend heraus, und `TALOS_SANDBOX_ALLOW_UNCONFINED` gilt hier
  **nicht** — ein unconfined fremder Agent ist keine Degradation, sondern ein
  anderes Produkt.
- Job-Umgebung ist eine positive Allowlist (`PATH`, Locale, `TMPDIR`/`PWD` im
  Workspace, `HOME` = `<workspace>/.home`). Kein Talos-Geheimnis, kein
  Bridge-Token, kein Deployment-Env erreicht einen Job. Die einzige
  Credential ist die eigene des Jobs: der Daemon liest den Claude-OAuth-Token
  frisch aus dem Worker-HOME und gibt ihn als `CLAUDE_CODE_OAUTH_TOKEN`-Wert
  mit — die Token-DATEI betritt das Sandbox nie.
- Jeder Job hat eine Gesamt-Deadline (Default 900 s, hart gedeckelt); auf
  Timeout wird die Prozessgruppe gekillt, der Zustand ist `timeout`.

## Grenzen (ehrlich)

- **Netzwerk ist AN in den Job-Sandboxes.** Claude braucht OAuth/API — das ist
  der eine dokumentierte Unterschied zur `run_shell`-Confine-Form. Die
  Begrenzung trägt das Dateisystem (Root read-only, nur der Job-Workspace
  schreibbar), nicht das Netz.
- **Das dedizierte HOME hält nur den Claude-OAuth-State** — und genau deshalb
  darf es kein Talos-Geheimnis enthalten und muss dem Worker gehören. Ein
  kompromittierter Job sieht die Claude-Session (als Env-Wert), nichts aus
  Talos. Das eigene HOME des Jobs liegt als `.home` IM Job-Workspace: Claude
  braucht ein beschreibbares HOME für State (`~/.claude`, `~/.claude.json`),
  und der Workspace ist der einzige beschreibbare Ort — gemessen am zweiten
  Live-E2E, als Claudes Bash am read-only Dateisystem starb.
- **Beim agy-Backend betritt die Token-DATEI den Wegwerf-Workspace** — der
  eine dokumentierte Unterschied der Credential-Form: agy kennt keinen
  Env-Token, also kopiert der Worker `antigravity-oauth-token` ins Job-HOME
  (0600). Das Exfiltrations-Risiko ist zum claude-Backend gleichwertig — der
  Job hat ohnehin Netz, und wer den Job kontrolliert, kann die API mit der
  eigenen Credential des Jobs ohnehin rufen —, aber die Kopie ist
  flüchtig: sie stirbt mit dem Workspace. Zwei ehrliche Kanten bleiben: der
  Token im agy-HOME ist ein OAuth-**Refresh**-Token — rotiert Google ihn
  (Passwort-Reset, Widerruf, Inaktivität), schlagen alle agy-Jobs mit dem
  dokumentierten Auth-Fehler fehl, bis der Betreiber den Login erneuert; und
  waehrend ein Job laeuft, liegt die Credential als lesbare Datei IM
  beschreibbaren Bereich des Jobs — gegen den Job selbst ist das kein
  Schutz (er darf sie haben), gegen alles andere traegt weiter die Sandbox.
- **Job-Workspaces sind Wegwerf-Verzeichnisse.** Kontinuität (was wurde
  delegiert, was kam zurück) lebt im Event-Log des Agenten, nicht im Worker:
  ein neu gestarteter Worker weiss nichts, und das ist Absicht.
- **Kein Streaming zum Modell.** Der Agent sieht Zwischenstände nur über
  `delegate_status`; das Ergebnis kommt als `summary` + `files` am Ende.
- **Ein Prompt ist opaker Text.** Ein `delegate_code`-Prompt, der
  `TOOL_CALL:`-Syntax enthält, löst keinen verschachtelten Tool-Call aus — der
  Kernel entscheidet genau eine Aktion (die Delegation).
- **Geteiltes `/run/talos` mit dem Modell-Worker:** laufen beide Worker auf
  einem Host, teilen sie das Runtime-Verzeichnis. Entweder beide Units mit
  derselben `Group=` und `RuntimeDirectoryMode=0770` betreiben (Agent in diese
  Gruppe aufnehmen), oder dem Claude-Worker ein eigenes
  `RuntimeDirectory=talos-claude` samt abweichendem Socket-Pfad geben.
- Der Worker importiert `config.py` nicht und liest nur seine eigene Env-Datei.
  Er ist ein Job-Rahmen, kein Agent.
- **`ProtectKernelTunables=yes` ist mit bubblewrap unverträglich** (macht Teile
  von `/proc` read-only; bwraps eigener `/proc`-Mount scheitert dann mit
  "Can't mount proc" und jeder Job stirbt stumm — per Bisect gemessen). Die
  Unit lässt es darum weg; die Konfinement-Grenze der Jobs trägt die Sandbox.
- **Fehlschläge kommen mit Spur zurück.** `failed`/`timeout` liefern im
  Status ein `error`-Feld (stderr-Tail bzw. Spawn-Grund, begrenzt auf 2000
  Zeichen) und den Returncode — der erste Live-Lauf zeigte, dass ein stummes
  `failed` un-debuggbar ist.
