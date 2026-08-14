# Operator-owned Statusquelle: das ganze Tailnet, ohne offenen Adressbereich

`entity_status` bindet an feste, vom Betreiber konfigurierte Quellen — nie an eine
Adresse, die das Modell selbst wählt. Der Web-Guard blockt `100.64.0.0/10` (Tailscale)
deshalb komplett; freigeschaltet werden einzelne Adressen über
`TALOS_WEB_ALLOWED_ADDRESSES`, typischerweise genau eine: der eigene Server.

Dieses Beispiel zeigt, wie der Agent trotzdem den Live-Status **aller** Tailnet-Geräte
melden kann, ohne dass die Freigabe wächst:

```
entity_status("NAS")
  → Registry-Match (entities.json)
  → http://<vps>.<tailnet>.ts.net/tailnet          eine bereits freigegebene Adresse
      (tailscale serve :80 → 127.0.0.1:8800)
  → status-server.py                                feste Pfad→Skript-Map, nichts injizierbar
  → tailnet-status.sh = `tailscale status --json`   ein Gerät im Tailnet kennt alle anderen
  → Evidence: {host, os, ip, online, last_seen} je Gerät
```

- **`status-server.py`** — Loopback-only-HTTP-Dienst; jeder Pfad führt genau ein
  fest verdrahtetes Skript aus.
- **`tailnet-status.sh`** — kompakter Geräte-Snapshot aus `tailscale status --json`
  (braucht `jq`; Gerätename aus `DNSName`, weil iOS als `HostName` „localhost" meldet).
- In `../entities.json` zeigt die Entity `nas`, wie ein Gerät auf den `/tailnet`-Endpoint
  gebunden wird: alle Geräte-Entities teilen sich dieselbe URL, das Modell liest sein
  Gerät aus dem Dump. Neue Geräte erscheinen automatisch im Snapshot; eine neue
  **Identität** bekommt bewusst nur, wer von Hand in die Registry eingetragen wird.
