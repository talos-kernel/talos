#!/bin/bash
# tailnet-status.sh — read-only Tailnet device snapshot as JSON on stdout.
# Ein fester Befehl, keine Parameter, keine Anfrage-Daten. Never modifies state.
#
# Gerätenamen kommen aus DNSName, nicht HostName: iOS-Geräte melden als HostName
# gern "localhost", der DNSName-Kurzname ist die stabile Kennung im Tailnet.

export LC_ALL=C

raw=$(timeout 5 tailscale status --json 2>/dev/null)
if [ -z "$raw" ]; then
    printf '{"error":"tailscale status unavailable"}\n'
    exit 1
fi

printf '%s' "$raw" | jq -c '{
  generated_at: (now | todate),
  devices: (
    ([.Self + {Online: true}] + (.Peer // {} | [.[]]))
    | map({
        host: (((.DNSName // "") | split(".")[0]) as $dns
               | if $dns != "" then $dns else ((.HostName // "") | ascii_downcase) end),
        os: (.OS // ""),
        ip: (.TailscaleIPs[0] // ""),
        online: (.Online // false),
        last_seen: (.LastSeen // "")
      })
    | sort_by(.host)
  )
}'
