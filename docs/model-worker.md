# Modell-Worker (UID-Trennung der Provider-Schlüssel)

Der Agent-Prozess hält im Worker-Modus **keinen** Provider-Schlüssel mehr. Ein
eigener Daemon (`talos/modelworker.py`) läuft als Benutzer `talos-model`, liest die
Schlüssel aus `/etc/talos/model.env` und spricht mit den Anbietern. Der Agent
schickt Anfragen über einen Unix-Socket; über den Draht gehen Prompts und Antworten,
nie Schlüssel.

Das schliesst die Lücke, die der Kernel nicht schliessen kann: solange Agent und
Schlüssel derselben UID gehören, reicht ein Fehler irgendwo im Agent-Prozess — nicht
im Kernel — um die Schlüssel zu verlieren. Die Trennung ist eine
**Installations-Entscheidung** (Eigentum, systemd), kein Code-Mechanismus: der Code
enthält kein setuid, er spricht nur Socket.

## Protokoll

JSON-Lines über `/run/talos/model.sock`, eine Anfrage pro Verbindung:

```
→  {"provider": "openai-api", "model": "…",
    "messages": [{"role": "system"|"user", "content": "…"}],
    "params": {"timeout_s": 180}}
←  {"ok": true,  "text": "…", "model": "…"}
←  {"ok": false, "kind": "rate_limited", "message": "(Reasoner error: …)"}
```

`kind` ist exakt die `ReasonerFailure`-Taxonomie aus `talos/api_reasoner.py`
(`key_rejected`, `rate_limited`, `overloaded`, `network_failed`, `timed_out`,
`http_failed`) — die Laufzeit-Fallback-Kette (`talos/fallback.py`) funktioniert über
den Socket unverändert. Kaputte Frames bekommen `invalid_request` und kosten die
Verbindung, nie den Daemon. Unbekannte Felder und unbekannte Rollen werden verworfen.

## Installation (Linux, systemd)

```bash
# 1. Benutzer und Gruppe; der Agent (talos) kommt ueber die GRUPPE an den Socket.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin talos-model
sudo usermod -aG talos-model talos

# 2. Schluessel-Datei — nur der Worker darf sie lesen.
sudo install -d -m 0750 -o talos-model -g talos-model /etc/talos
sudo install -m 0600 -o talos-model -g talos-model /dev/null /etc/talos/model.env
sudoedit /etc/talos/model.env
#   OPENAI_API_KEY=sk-…
#   ANTHROPIC_API_KEY=sk-ant-…
#   TALOS_BASE_URL_OPENAI_API=…        # optional, wie bisher pro Anbieter

# 3. Unit installieren und starten.
sudo install -m 0644 deploy/talos-model.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now talos-model.service
```

Das Verzeichnis `/run/talos` legt systemd an (`RuntimeDirectory=`): Eigentümer
`talos-model:talos-model`, Mode 0750. Der Socket darin bekommt Mode 0660; seine
Gruppe erbt `talos-model` vom Verzeichnis. **Ehrliche Abweichung von der
Design-Skizze:** das Verzeichnis gehört dem Dienst-User statt root — für den Agenten
gleich streng (Gruppe `r-x`, kein Schreiben), und der Dienst braucht so kein root.

## Agent-Seite

In der Unit (oder Umgebung) des **Agenten**, nicht in `talos.env` — `ApiReasoner`
liest die Variable beim Bauen aus dem Prozess-Env, und zwei Quellen würden driften
(in `talos.env` gesetzt, im Reasoner ignoriert = stiller Rückfall auf den Direktweg):

```
Environment=TALOS_MODEL_WORKER=socket:///run/talos/model.sock
```

Danach die Provider-Schlüssel (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) aus der
Agent-Umgebung und `talos.env` **entfernen**. Ein Agent-Env ohne Schlüssel ist im
Worker-Modus der erwartete Zustand, kein Mangel; `config get` antwortet für die
Schlüssel weiterhin `[REDACTED]` (gesetzt oder nicht, hier wie dort).

## Fail-closed-Verhalten

- Socket unerreichbar → klassifizierter `network_failed`-Fehler; die Fallback-Kette
  greift. Es gibt **keinen** stillen Rückfall auf einen Schlüssel im Agent-Env.
- `TALOS_MODEL_WORKER` gesetzt, aber nicht in der Form `socket://…` → Fehler beim
  Bauen des Reasoners, keine stille Vorgabe.
- Der Worker erzwingt für sich den Direktweg: ein `TALOS_MODEL_WORKER` in *seiner*
  Umgebung wird ignoriert — Anfragen werden nie über einen Socket weitergereicht.

## Grenzen (ehrlich)

- **OAuth/CLI-Reasoner können nicht in den Worker.** `ClaudeCliReasoner` und
  `HermesCliReasoner` sind Abo-Logins, die eine lokale CLI spawnen — die CLI samt
  Login lebt im Agent-Prozess und liesse sich nur mit ihr umziehen. Die Trennung
  gilt für den API-Weg (`ApiReasoner`); wer ein Abo nutzt, bleibt beim Direktweg.
- **Kein Live-Streaming über den Socket.** Der Antworttext reist als eine Zeile; die
  Live-Anzeige bekommt ihn am Stück statt delta-weise.
- **Token-Zähler zeigen 0 im Worker-Modus.** Über das Protokoll reist nur Text;
  `/usage` zählt Lauf und Dauer weiter, aber keine Provider-Tokens.
- Der Worker hat keinen Werkzeug-Code, keine Shell, kein Dateisystem ausser
  `/etc/talos/model.env`. Er ist ein Rohr, kein Agent.
