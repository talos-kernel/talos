#!/usr/bin/env bash
# Den oeffentlichen Stand (talos-kernel/talos) aus dieser Arbeitskopie nachziehen.
#
# ⚠️ Warum das ein Skript ist und keine Zeile im Kopf: der Weg hat zwei Fallen, und ich
# bin am 06.08. an EINEM Tag zweimal in dieselbe getappt.
#
#   1. NICHT `git archive`. `.gitattributes` traegt `export-ignore` auf `site/` und
#      `assets/`. Fuer den Release-Tarball ist das richtig; beim Repo verschwinden damit
#      60 von 200 Dateien, ohne dass irgendetwas fehlschlaegt.
#   2. `cd` wirkt im selben Shell-Aufruf fort. `cd "$ZIEL" && … ; git ls-files | tar`
#      listet dann die Dateien des ZIELS — das gerade geleert wurde — und tar meldet
#      200 mal „Cannot stat". Die Quelle wird hier deshalb ueberall absolut benannt.
#
# Aufruf:  scripts/sync-public.sh <zielverzeichnis>
# Das Ziel ist ein Klon von github.com/talos-kernel/talos (eigene, saubere Historie).
set -euo pipefail

QUELLE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIEL="${1:-}"

if [ -z "$ZIEL" ] || [ ! -d "$ZIEL/.git" ]; then
  echo "usage: scripts/sync-public.sh <clone-of-talos-kernel/talos>" >&2
  exit 2
fi

# Alles ausser .git weg: sonst bleiben Dateien liegen, die es in der Quelle nicht mehr gibt.
find "$ZIEL" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

# Die Quelle absolut, damit kein vorheriges `cd` mitredet.
git -C "$QUELLE" ls-files -z | (cd "$QUELLE" && xargs -0 tar cf -) | tar xf - -C "$ZIEL"

quelle_n=$(git -C "$QUELLE" ls-files | wc -l | tr -d ' ')
ziel_n=$(find "$ZIEL" -path "$ZIEL/.git" -prune -o -type f -print | wc -l | tr -d ' ')
echo "Quelle: $quelle_n · Ziel: $ziel_n"
[ "$quelle_n" = "$ziel_n" ] || { echo "FEHLER: Dateizahl weicht ab" >&2; exit 1; }

# Zwei Stichproben auf genau das, was `git archive` schlucken wuerde.
for pfad in site/index.html assets/talos-icon-256.png; do
  [ -f "$ZIEL/$pfad" ] || { echo "FEHLER: $pfad fehlt im Ziel" >&2; exit 1; }
done
echo "site/ und assets/ sind da."

cat <<'HINWEIS'

Naechste Schritte von Hand — bewusst nicht automatisch:
  cd <ziel> && python -m pytest tests/ -q && python redteam.py
  git add -A
  git commit                # Identitaet EINMAL im Ziel-Klon setzen: git config user.email …
  git push origin main      # <- im ZIEL-Klon; dort ist `origin` das oeffentliche Repo

⚠️ Die Identitaet steht bewusst NICHT mehr hier. Sie stand ausgeschrieben in dieser
Datei, und diese Datei lag bis 0.8.1 in jedem ausgelieferten Tarball. Sie gehoert in
die Konfiguration des Ziel-Klons, wo sie ohnehin gebraucht wird.

⚠️ Im Arbeitsbaum heissen die Remotes `private` und `public`, und `public` ist zum
Pushen GESPERRT: ein direkter Push von dort truege 108 Commits samt echter
Mailadresse ins oeffentliche Repo. Der oeffentliche Stand entsteht nur hier.

⚠️ Der oeffentliche Verlauf traegt KEINE Co-Author- oder Session-Zeilen. Vor dem Push:
  git log -1 --format=%B | grep -ciE 'co-authored-by|claude-session|claude\.ai/|session_[a-z0-9]{6}'

⚠️ Das Muster sucht die ZUSCHREIBUNGSFORMEN, nicht das blosse Wort. Vorher stand dort
`claude`, und das schlug am 06.08. beim Dateinamen `CLAUDE.md` an — einem voellig
regulaeren Teil dieses Repos. Ein Waechter, der bei etwas Erlaubtem laeutet, wird
ueberlesen, und dann laeutet er auch beim naechsten echten Fall vergebens.
HINWEIS
