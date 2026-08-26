# deploy/ — systemd-Beispiele (Doku-Qualitaet)

Vier Dateien, zwei Ablaeufe, beide **send-only**: sie lesen die haltbaren Quellen
(Event-Log, Zeitplan-DB, Anker-Datei) und senden einen Bericht an den Betreiber. Kein
Agentenlauf, kein Modell, keine Werkzeug-Kette — und deshalb auch nichts, was der
Kernel freigeben muesste.

| Einheit | Was sie tut | Wann |
|---|---|---|
| `talos-anchor.timer` / `.service` | `talos anchor --send` — Kettenkopf festhalten, Digest an den Owner-Chat (mit `--mail` zusaetzlich per Mail) | taeglich 06:30 |
| `talos-briefing.timer` / `.service` | `talos briefing --send` — Morgen-Briefing (Gesundheit, Kette, offene Freigaben, Fehler des Vortags, Anker-Alter) an den Owner-Chat | taeglich 07:00 |

## Benutzung

1. In den `.service`-Dateien `User=`, `WorkingDirectory=` und `EnvironmentFile=` an die
   eigene Installation anpassen — die Werte hier sind Platzhalter.
2. Nach `/etc/systemd/system/` kopieren, dann:

   ```bash
   systemctl daemon-reload
   systemctl enable --now talos-anchor.timer talos-briefing.timer
   systemctl list-timers talos-*
   ```

3. Einzeln testen: `systemctl start talos-briefing.service` — Exit 1 heisst
   kritischer Befund (gebrochene Kette) oder fehlgeschlagener Versand und ist so
   gewollt: ein Waechter soll an echten Befunden scheitern, nicht an Warnungen.

## In-App-Alternative

Wer statt systemd den Zeitplan-Ticker des laufenden Dienstes nutzen will, installiert
den taeglichen Eintrag mit `talos briefing --install`. Der landet als gewoehnlicher
Auftrag in der Zeitplan-DB (`schedule.ScheduleStore`): beim Faelligwerden laeuft er
durch den Kernel und unter `UnattendedCeiling` — ein unbeaufsichtigter Lauf darf
weniger als ein getippter, und das Installieren erteilt selbst kein Recht.
