#!/usr/bin/env bash
# Die Website (site/) per rsync auf den Webhost bringen.
#
# ⚠️ Warum das ein Skript ist und keine Zeile im Kopf: dieselbe Begruendung wie bei
# scripts/sync-public.sh. Der Weg hat Fallen, und die Fallen stehen hier als Guards,
# nicht als Erinnerung, die beim zwanzigsten Deploy fehlt.
#
#   1. Dry-run ist der DEFAULT. Ohne --apply wird kein Byte uebertragen. Ein Deploy,
#      der beim Tippfehler schon laeuft, ist keiner.
#   2. Das Ziel wird geprueft, BEVOR rsync es sieht. Dieses Skript loescht am Ziel,
#      was in der Quelle fehlt (--delete) — auf ein falsches Ziel gerichtet loescht
#      es das falsche Verzeichnis. Leer, "/" und Pfade, die nicht nach Web-Root
#      aussehen, werden deshalb verweigert, mit Begruendung.
#   3. --itemize-changes immer, im Probelauf wie im echten: was sich aendert, steht
#      Zeile fuer Zeile da. Ein Deploy ohne Diff ist ein Blindflug mit Protokoll.
#   4. Nach --apply laeuft derselbe rsync noch einmal als Probelauf: null verbleibende
#      Differenzen sind der Read-back-Beweis, dass Quelle und Ziel identisch sind.
#      `exit 0` allein beweist das nicht.
#
# Aufruf:
#   scripts/deploy-site.sh --target user@host:/pfad/zum/webroot            # Dry-run
#   scripts/deploy-site.sh --target user@host:/pfad/zum/webroot --apply    # Deploy
#
# Der Host-Teil darf auch ein ssh-Alias ohne user@ sein; der Pfad-Teil muss ein
# Segment tragen, das nach Web-Root aussieht (www, html, htdocs, public, site, web).
set -euo pipefail

QUELLE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/site"
TARGET=""
APPLY=0

usage() {
  echo "usage: scripts/deploy-site.sh --target user@host:path [--apply]" >&2
  echo "       (Dry-run ist der Default; erst --apply uebertraegt.)" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --apply)  APPLY=1; shift ;;
    -h|--help) usage ;;
    *) echo "FEHLER: unbekanntes Argument: $1" >&2; usage ;;
  esac
done

# ── Quelle pruefen: nicht irgendein Verzeichnis, sondern die Site dieses Repos.
# Zwei Stichproben, wie in sync-public.sh — eine Seite und ein Asset.
for pfad in index.html vergleich/index.html brand/favicon.ico; do
  [ -f "$QUELLE/$pfad" ] || { echo "FEHLER: $QUELLE/$pfad fehlt — falsche Arbeitskopie?" >&2; exit 1; }
done

# ── Ziel-Guard. Reihenfolge: Form, dann Pfad, dann Plausibilitaet.
[ -n "$TARGET" ] || usage

case "$TARGET" in
  *:?*) : ;;   # host:pfad mit nicht-leerem Pfad
  *) echo "FEHLER: Ziel muss die Form user@host:pfad haben (bekommen: '$TARGET')" >&2; exit 1 ;;
esac

HOSTPART="${TARGET%%:*}"
PFADPART="${TARGET#*:}"

[ -n "$HOSTPART" ] || { echo "FEHLER: Host-Teil des Ziels ist leer." >&2; exit 1; }

if [ "$PFADPART" = "/" ]; then
  echo "FEHLER: '/' als Zielpfad wird verweigert — rsync --delete auf die Wurzel" >&2
  echo "        eines Hosts ist die eine Zeile, die dieses Skript verhindern muss." >&2
  exit 1
fi

# Heuristik, keine Sicherheitsgrenze: ein Webroot heisst praktisch immer nach einem
# dieser Muster. Der Guard faengt den vertippten oder verwechselten Pfad, bevor
# --delete ihn leert — wer wirklich anders heisst, benennt sein Webroot-Verzeichnis
# entsprechend oder passt die Liste hier bewusst an.
case "/$PFADPART/" in
  */www*|*/html*|*/htdocs*|*/public*|*/site*|*/web*) : ;;
  *)
    echo "FEHLER: '$PFADPART' sieht nicht nach einem Web-Root aus." >&2
    echo "        Erwartet wird ein Pfadsegment wie www, html, htdocs, public, site oder web." >&2
    exit 1
    ;;
esac

command -v rsync >/dev/null || { echo "FEHLER: rsync nicht gefunden." >&2; exit 1; }

# ── rsync. README.md ist Repo-Doku, keine Seite; .DS_Store ist macOS-Beifang.
# --delete gehoert dazu: eine geloeschte Seite, die am Ziel weiterlebt, ist eine
# Seite, die niemand mehr pflegt und jeder noch findet.
# /dist/ traegt die SIGNIERTEN Release-Tarballs — sie werden nie aus diesem Baum
# erzeugt und duerfen vom Abgleich unberuehrt bleiben. Die Hex-.txt im Wurzel-
# verzeichnis ist die Domain-Verifikation des Hosters (robots.txt faellt nicht
# unter das Muster: 'r','o','s','t' sind keine Hex-Zeichen).
RSYNC_ARGS=(-rlptz --delete --itemize-changes
  --exclude README.md --exclude .DS_Store
  --exclude '/dist/' --exclude '/[0-9a-f]*.txt')

if [ "$APPLY" -eq 1 ]; then
  echo "▸ Deploy: $QUELLE/ → $TARGET"
  rsync "${RSYNC_ARGS[@]}" "$QUELLE/" "$TARGET/"

  # Read-back: derselbe Aufruf als Probelauf muss jetzt leer sein.
  rest=$(rsync -n "${RSYNC_ARGS[@]}" "$QUELLE/" "$TARGET/" | grep -c . || true)
  if [ "$rest" -eq 0 ]; then
    echo "✓ Read-back: Quelle und Ziel identisch."
  else
    echo "FEHLER: nach dem Deploy bleiben $rest Differenzen — Ziel pruefen." >&2
    exit 1
  fi
else
  echo "▸ Dry-run (es wird NICHTS uebertragen): $QUELLE/ → $TARGET"
  ausgabe=$(rsync -n "${RSYNC_ARGS[@]}" "$QUELLE/" "$TARGET/")
  [ -z "$ausgabe" ] || printf '%s\n' "$ausgabe"
  aenderungen=$(printf '%s' "$ausgabe" | grep -c . || true)
  echo ""
  echo "$aenderungen Aenderung(en) anstehend. Ausfuehren mit:"
  echo "  scripts/deploy-site.sh --target '$TARGET' --apply"
fi
