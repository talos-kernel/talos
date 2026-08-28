# Dashboard (Beobachten, nicht eingreifen)

Ein eigener, minimaler Prozess (`talos/dashboard.py`, nur stdlib) zeigt den Stand
der Maschine live: laufende Läufe, offene Freigaben, das Event-Log, Zeitpläne und
installierte Blueprints. Er ist die Antwort auf eine Lücke: `/health` im Chat setzt
einen laufenden Agenten voraus, und `talos health` ist ein Schnappschuss für die
Kommandozeile — kein Blick, der mitläuft.

Die Doktrin bleibt unangetastet: **kein eingehender Kanal in den Agentenprozess.**
Das Dashboard öffnet dieselben Dateien wie `talos health` — Event-Log, Zeitplan-DB,
Blueprint-Stand — und zwar read-only (`file:…?mode=ro` plus `PRAGMA query_only=ON`).
Es gibt nur GET. POST/PUT/DELETE bekommen 405, unbekannte Pfade 404. **Freigaben
gibt es hier bewusst nicht**: Telegram bleibt der einzige Eingriffsweg. Ein
„Approve"-Knopf wäre ein zweiter Erlaubnisweg am Kernel vorbei.

## Routen

| Route | Inhalt |
|---|---|
| `GET /` | self-contained HTML-Seite (inline CSS/JS, pollt alle 5 s) |
| `GET /api/status` | `health.collect()` + Version |
| `GET /api/runs` | laufende Läufe (Heuristik: `reason.started` ohne `reason.done`) und die letzten 25 beendeten |
| `GET /api/approvals` | stehende Freigaben (exakt, nachgespielt) + offene als ehrlich gelabelte Heuristik |
| `GET /api/events` | letzte 50 Events, Payloads gedeckelt und redigiert |
| `GET /api/schedules` | Anzahl, nächste Fälligkeiten, Blueprints — **nie ein Prompt** |

„Laufend" und „offen" sind aus einem append-only Log approximiert und im JSON als
Heuristik markiert; der exakte Stand lebt im flüchtigen Speicher des Agenten, zu
dem dieser Prozess keinen Draht hat.

## Betrieb

```bash
# Von Hand (Vordergrund):
python -m talos dashboard          # 127.0.0.1:8810

# Als Dienst:
sudo install -m 0644 deploy/talos-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now talos-dashboard.service

# Erreichbar machen — nur übers Tailnet, nie direkt:
tailscale serve --bg 8810          # https://<maschine>.<tailnet>.ts.net
```

Zwei Schranken statt einer: der Dienst bindet Loopback; Reichweite kommt
ausschliesslich vom Tailnet-Proxy davor. Fällt die Proxy-Regel weg, steht nichts
im offenen Netz. Der Betreiber sichtet das Dashboard über Tailscale — für
Dritte ist es unsichtbar.
