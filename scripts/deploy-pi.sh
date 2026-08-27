#!/usr/bin/env bash
# Deploy only the Python package into an existing private Talos deployment.
# The deployment root keeps its own identity, configuration, secrets, and runtime data.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_NAME="talos"
PACKAGE_PARENT="$(basename "$REPO_ROOT")"
SOURCE="$REPO_ROOT/$PACKAGE_NAME"

TARGET=""
APPLY=0
KEEP_BACKUP=0
ROLLBACK=0
SKIP_TESTS=0
DIFF_FILE=""

usage() {
  cat <<'EOF'
Usage: scripts/deploy-pi.sh --target user@host:path [options]

Deploys only the local talos/ package to path/talos/ on the remote host.
The target path is the deployment root and must end in /talos (the package
parent), not in /talos/talos.

Safety:
  * Dry-run is the default; use --apply to change the remote deployment.
  * Repository-root files are never synced. SOUL.md, CLAUDE.md, *.env, and
    data/ remain deployment-owned; defensive rsync excludes protect them too.
  * Local tests run before deployment unless --skip-tests is supplied.

Options:
  --target user@host:path  Required remote deployment root.
  --apply                  Execute the deployment or rollback.
  --backup                 Before deploying, copy remote talos/ to
                           talos.bak-<UTC timestamp>.
  --rollback               Restore the newest remote talos.bak-* tree.
  --skip-tests             Do not run python3 -m pytest tests/ -q.
  -h, --help               Show this help.
EOF
}

die() {
  echo "FEHLER: $*" >&2
  exit 2
}

cleanup() {
  if [ -n "$DIFF_FILE" ]; then
    rm -f "$DIFF_FILE"
  fi
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      [ "$#" -ge 2 ] || die "--target braucht user@host:path"
      TARGET="$2"
      shift 2
      ;;
    --target=*)
      TARGET="${1#*=}"
      shift
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --backup)
      KEEP_BACKUP=1
      shift
      ;;
    --rollback)
      ROLLBACK=1
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unbekannte Option: $1"
      ;;
  esac
done

[ -n "$TARGET" ] || die "--target user@host:path ist erforderlich"
[ "$KEEP_BACKUP" -eq 0 ] || [ "$ROLLBACK" -eq 0 ] || \
  die "--backup und --rollback koennen nicht kombiniert werden"

case "$TARGET" in
  *@*:*) ;;
  *) die "Ziel muss die Form user@host:path haben" ;;
esac

REMOTE="${TARGET%%:*}"
REMOTE_ROOT="${TARGET#*:}"
REMOTE_ROOT="${REMOTE_ROOT%/}"

if [[ ! "$REMOTE" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]]; then
  die "ungueltiger user@host-Teil im Ziel"
fi
case "$REMOTE_ROOT" in
  ''|'/') die "leerer Zielpfad und / sind gesperrt" ;;
  *[!A-Za-z0-9._/-]*) die "Zielpfad enthaelt nicht erlaubte Zeichen" ;;
esac
case "/$REMOTE_ROOT/" in
  */../*|*/./*) die "Zielpfad darf keine .- oder ..-Segmente enthalten" ;;
esac
[ "$(basename "$REMOTE_ROOT")" = "$PACKAGE_PARENT" ] || \
  die "Zielpfad muss im Package-Parent /$PACKAGE_PARENT enden"
[ -d "$SOURCE" ] || die "lokales Package fehlt: $SOURCE"

REMOTE_PACKAGE="$REMOTE_ROOT/$PACKAGE_NAME"
REMOTE_DEST="$REMOTE:$REMOTE_PACKAGE/"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

command -v ssh >/dev/null 2>&1 || die "ssh ist nicht installiert"
command -v rsync >/dev/null 2>&1 || die "rsync ist nicht installiert"

RSYNC_COMMON=(
  -a
  --delete
  --exclude=/SOUL.md
  --exclude=/CLAUDE.md
  --exclude='*.env'
  --exclude=/data/
  # Build-Reste der Quelle gehoeren nicht in ein Deployment: ein mitgeschickter
  # pyc-Stand einer anderen Maschine ist Alt-Code, der wie aktueller aussieht,
  # und Finder-Beifang gehoert auf keinen Server.
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.DS_Store'
)
RSYNC_REPORT=(
  --itemize-changes
  --human-readable
  --out-format='%i %l %n%L'
)

newest_backup() {
  ssh "$REMOTE" \
    "find '$REMOTE_ROOT' -maxdepth 1 -type d -name 'talos.bak-*' -print 2>/dev/null | LC_ALL=C sort -r | head -n 1"
}

rollback_package() {
  local latest backup_suffix restore_tmp displaced
  latest="$(newest_backup)"
  [ -n "$latest" ] || die "kein Remote-Backup talos.bak-* gefunden"
  backup_suffix="${latest#"$REMOTE_ROOT/talos.bak-"}"
  if [ "$latest" != "$REMOTE_ROOT/talos.bak-$backup_suffix" ] || \
     [[ ! "$backup_suffix" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    die "Remote-Backup hat keinen sicheren Zeitstempel-Namen: $latest"
  fi

  echo "Rollback-Quelle: $REMOTE:$latest"
  if [ "$APPLY" -eq 0 ]; then
    echo "DRY-RUN: wuerde $latest nach $REMOTE_PACKAGE wiederherstellen"
    echo "Mit --apply ausfuehren."
    return
  fi

  restore_tmp="$REMOTE_ROOT/.talos.restore-$TIMESTAMP"
  displaced="$REMOTE_ROOT/talos.pre-rollback-$TIMESTAMP"
  ssh "$REMOTE" "set -eu
    test ! -e '$restore_tmp'
    cp -a '$latest' '$restore_tmp'
    if test -e '$REMOTE_PACKAGE'; then mv '$REMOTE_PACKAGE' '$displaced'; fi
    if mv '$restore_tmp' '$REMOTE_PACKAGE'; then
      :
    else
      if test -e '$displaced'; then mv '$displaced' '$REMOTE_PACKAGE'; fi
      exit 1
    fi"
  echo "Rollback ausgefuehrt: $latest -> $REMOTE_PACKAGE"
  echo "Der ersetzte Stand liegt, falls vorhanden, unter $displaced."
}

if [ "$ROLLBACK" -eq 1 ]; then
  rollback_package
  exit 0
fi

if [ "$SKIP_TESTS" -eq 0 ]; then
  # Die Projekt-venv zuerst: ein globales python kann ein fremdes site-packages-
  # 'tests'-Paket shadowen und Tests faelschlich brechen (hier gemessen, nicht vermutet).
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
  else
    PYTHON="python3"
  fi
  echo "Lokale Tests: $PYTHON -m pytest tests/ -q"
  (cd "$REPO_ROOT" && "$PYTHON" -m pytest tests/ -q)
else
  echo "Lokale Tests uebersprungen (--skip-tests)."
fi

DIFF_FILE="$(mktemp "${TMPDIR:-/tmp}/talos-deploy-diff.XXXXXX")"
rsync "${RSYNC_COMMON[@]}" "${RSYNC_REPORT[@]}" --dry-run \
  "$SOURCE/" "$REMOTE_DEST" >"$DIFF_FILE"

if [ "$APPLY" -eq 0 ]; then
  echo "DRY-RUN: geplante Remote-Aenderungen (Groesse in Bytes):"
  if [ -s "$DIFF_FILE" ]; then
    cat "$DIFF_FILE"
  else
    echo "(keine)"
  fi
  echo "Mit --apply ausfuehren."
  exit 0
fi

if [ "$KEEP_BACKUP" -eq 1 ]; then
  BACKUP_PATH="$REMOTE_ROOT/talos.bak-$TIMESTAMP"
  ssh "$REMOTE" "set -eu
    test -d '$REMOTE_PACKAGE'
    test ! -e '$BACKUP_PATH'
    cp -a '$REMOTE_PACKAGE' '$BACKUP_PATH'"
  echo "Backup erstellt: $REMOTE:$BACKUP_PATH"
fi

rsync "${RSYNC_COMMON[@]}" "$SOURCE/" "$REMOTE_DEST"

VERIFY_FILE="$(mktemp "${TMPDIR:-/tmp}/talos-deploy-verify.XXXXXX")"
if ! rsync "${RSYNC_COMMON[@]}" "${RSYNC_REPORT[@]}" --dry-run \
  "$SOURCE/" "$REMOTE_DEST" >"$VERIFY_FILE"; then
  rm -f "$VERIFY_FILE"
  die "Post-Deploy-Verifikation fehlgeschlagen"
fi
if [ -s "$VERIFY_FILE" ]; then
  cat "$VERIFY_FILE" >&2
  rm -f "$VERIFY_FILE"
  die "Remote-Package weicht nach dem Deploy noch von der Quelle ab"
fi
rm -f "$VERIFY_FILE"

echo "Remote geaendert (rsync-Itemisierung, Groesse in Bytes):"
if [ -s "$DIFF_FILE" ]; then
  cat "$DIFF_FILE"
else
  echo "(keine)"
fi
echo "Deploy verifiziert: ausschliesslich $SOURCE/ -> $REMOTE_DEST"

# Der Blueprint-Katalog liegt neben dem Package (INSTALL_DIR/blueprints) — ohne
# ihn kennt das Ziel keine installierbaren Blueprints. Gleicher Vertrag wie oben:
# spiegeln, dann per erneutem Dry-Run beweisen, dass Quelle und Ziel gleich sind.
BLUEPRINTS_SOURCE="$REPO_ROOT/blueprints"
if [ -d "$BLUEPRINTS_SOURCE" ]; then
  rsync -a --delete --exclude='.DS_Store' \
    "$BLUEPRINTS_SOURCE/" "$REMOTE:$REMOTE_ROOT/blueprints/"
  if rsync -a --delete --dry-run --itemize-changes \
      "$BLUEPRINTS_SOURCE/" "$REMOTE:$REMOTE_ROOT/blueprints/" | grep -q .; then
    die "Remote-Blueprint-Katalog weicht nach dem Deploy noch von der Quelle ab"
  fi
  echo "Deploy verifiziert: $BLUEPRINTS_SOURCE/ -> $REMOTE:$REMOTE_ROOT/blueprints/"
fi
