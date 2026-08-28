# WhatsApp über den eigenen Broker (SSH-Pull statt Webhook)

Zwei Implementierungen tragen denselben Kanalnamen (`whatsapp`) — und werden nie
gleichzeitig registriert. Die Cloud-API-Variante (`talos/whatsapp.py`) bleibt
**Zustellung-only**: Metas Eingangsweg ist ein Webhook, und ein Webhook ist genau der
Port, den dieses Design nicht öffnet. Die Broker-Variante (`talos/wabroker.py`) holt
herein, ohne die Regel zu brechen — jede Verbindung geht von Talos nach draussen, zu
einer Maschine, die der Betreiber selbst kontrolliert.

Ist `TALOS_WA_BROKER_SSH` gesetzt, gewinnt der Broker gegen die Cloud-API-Variante:
die Registry verlangt eindeutige Namen, und der Broker ist der einzige der beiden
Wege, der auch hereinholt. Der Namespace bleibt derselbe — Konversationen heissen
weiter `whatsapp:<nummer>`, Identitäten in `TALOS_ALLOWED_PRINCIPALS` ebenso.

## Architektur

```
WhatsApp ──▶ Listener auf dem Broker-Host ──▶ JSONL-Queue (append, prefix-geroutet)
                                                    ▲
Talos ── ssh: tail -c +<cursor+1> ──────────────────┘
Talos ── ssh: node scripts/send.js ──────────▶ Broker ──▶ WhatsApp
```

- **Eingehend:** Ein Listener auf dem Broker-Host schreibt Nachrichten, die per
  broker-seitiger Prefix-Konvention (die Adressierungsform des Agenten) an Talos
  geroutet sind, zeilenweise in eine JSONL-Queue. Talos **holt** die Queue per SSH ab — mit einem Byte-Cursor,
  der in `data/wa-broker-cursor.json` liegt. Der **erste Lauf holt nichts**: ohne
  belegten Stand springt der Cursor ans Dateiende, statt den Backlog noch einmal
  als Auftrag zu stellen. Danach liefert jeder Poll nur, was seit dem letzten
  erfolgreichen Stand angehängt wurde (gedeckelt auf 64 Einträge und 256 KB pro
  Zug; der Rest kommt im nächsten Zyklus — nichts geht verloren).
- **Ausgehend:** Über das Sende-Skript des Brokers (`scripts/send.js` im
  CLI-Verzeichnis). Der Text reist base64-kodiert — keine Shell sieht ihn je als
  Syntax, und in Fehlermeldungen landet er nicht. Lange Texte werden an
  Absatz-, Zeilen-, dann Wortgrenzen geteilt (4000 Zeichen), vollständig und in
  Reihenfolge.
- **Dateien:** In zwei Sprüngen — `scp` auf den Broker-Host (`/tmp/wa_…`), dann
  `send.js` von dort. Bilder gehen als `--image`, alles andere als `--document`
  mit Mime aus der Endung. Ein fehlender lokaler Pfad scheitert **vor** jedem
  Subprozess, ein fehlgeschlagener Upload löst kein Senden aus.

Format einer Queue-Zeile (Vertrag mit dem Broker, `text` bereits prefix-bereinigt):

```json
{"at":"…","atMs":…,"messageId":"ABCD1234",
 "chatJid":"…","senderNumber":"41790000000",
 "pushName":"…","text":"status der agenten"}
```

Kaputte oder unvollständige Zeilen fallen still heraus (der Cursor ist ohnehin
darüber hinweg); eine letzte Zeile ohne Newline gilt als halb geschriebener Append
und wird **nicht** verbraucht — der nächste Poll liest sie komplett.

## Konfiguration

| Variable | Vorgabe | Was sie ist |
|---|---|---|
| `TALOS_WA_BROKER_SSH` | *(leer = Kanal aus)* | Das SSH-Ziel — ein Alias aus `~/.ssh/config`. Erst ein nicht-leerer Wert schaltet den Kanal ein; ohne Opt-in versucht keine bestehende Installation plötzlich einen SSH-Ruf. |
| `TALOS_WA_BROKER_QUEUE` | `/var/lib/wa-broker/talos-queue.jsonl` | Pfad der JSONL-Queue auf dem Broker-Host. |
| `TALOS_WA_BROKER_CLI_DIR` | `/opt/wa-broker` | Verzeichnis des CLI auf dem Broker-Host (darin `scripts/send.js`). |

Die SSH-Aufrufe laufen mit `BatchMode=yes` und eigenen Deadlines (15 s Connect,
30 s pro Ruf): ein hängendes `ssh` wird zum Kanalfehler, nicht zum Stillstand des
Poll-Loops. Der Broker braucht auf seiner Seite die Prefix-Konvention — nur
Nachrichten, die mit dem vereinbarten Prefix an den Agenten adressiert sind,
landen in der Queue.

## Vertrauensmodell

Der Kanal trägt `Trust.FULL` — als `property` ohne Setter, nicht zu heben und
nicht zu senken. Die Begründung ist dieselbe wie bei Telegram: die Absendernummer
kommt aus dem WhatsApp-Konto des Betreibers, nicht aus einem Textfeld, das der
Absender selbst tippt. Wer die Queue fälschen kann, kontrolliert bereits den
Broker — der Rückweg ist nicht weicher als die erste Tür.

Wer schreiben darf, steht wie überall in `TALOS_ALLOWED_PRINCIPALS`, als
`whatsapp:<nummer>`.

## Fehlersemantik

- **Poll-Fehler** (SSH weg, Queue unlesbar, Timeout) → lauter `BrokerError`, den
  die Registry als `channel.error`-Event meldet. Der Cursor rückt **nie** bei
  einem Fehler vor — der nächste Poll liest dieselben Bytes noch einmal. Ein
  still verschluckter Ausfall sähe aus wie „keine Nachrichten", und genau so
  sähe auch ein abgeklemmter Weg aus; deshalb ist der Fehler laut.
- **Sende-Fehler** nennen rc und stderr (gekürzt), niemals die Nutzlast.
- **Fail-closed überall:** leere Queue-/CLI-Pfade sind ein Konfigurationsfehler
  vor dem ersten Kommando; eine kaputte `conversation` wird abgelehnt, bevor
  irgendein Subprozess startet.
