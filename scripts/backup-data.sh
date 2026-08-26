#!/usr/bin/env bash
# Pull the deployment-owned runtime data over SSH into an off-device archive.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_PARENT="$(basename "$REPO_ROOT")"

TARGET=""
DEST="./backups"
KEEP=14
AGE_RECIPIENT_VALUE="${AGE_RECIPIENT:-}"
INSECURE_PLAIN=0
TEMP_ARCHIVE=""

usage() {
  cat <<'EOF'
Usage: scripts/backup-data.sh --target user@host:path [options]

Streams the remote deployment's path/data/ directory over SSH into a timestamped
archive. The target path is the deployment root and must end in /talos.

Encryption is mandatory when AGE_RECIPIENT or --age-recipient is provided.
Without a recipient, the script refuses to run unless --insecure-plain is explicit.

Options:
  --target user@host:path  Required remote deployment root.
  --dest DIR               Local archive directory (default: ./backups).
  --age-recipient VALUE    age recipient; overrides AGE_RECIPIENT.
  --insecure-plain         Explicitly permit an unencrypted .tar.gz archive.
  --keep N                 Keep newest N archives (default: 14).
  -h, --help               Show this help.
EOF
}

die() {
  echo "FEHLER: $*" >&2
  exit 2
}

cleanup() {
  if [ -n "$TEMP_ARCHIVE" ]; then
    rm -f "$TEMP_ARCHIVE"
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
    --dest)
      [ "$#" -ge 2 ] || die "--dest braucht ein Verzeichnis"
      DEST="$2"
      shift 2
      ;;
    --dest=*)
      DEST="${1#*=}"
      shift
      ;;
    --age-recipient)
      [ "$#" -ge 2 ] || die "--age-recipient braucht einen Wert"
      AGE_RECIPIENT_VALUE="$2"
      shift 2
      ;;
    --age-recipient=*)
      AGE_RECIPIENT_VALUE="${1#*=}"
      shift
      ;;
    --insecure-plain)
      INSECURE_PLAIN=1
      shift
      ;;
    --keep)
      [ "$#" -ge 2 ] || die "--keep braucht eine Zahl"
      KEEP="$2"
      shift 2
      ;;
    --keep=*)
      KEEP="${1#*=}"
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
[ -n "$DEST" ] || die "--dest darf nicht leer sein"
case "$KEEP" in
  ''|*[!0-9]*) die "--keep muss eine positive ganze Zahl sein" ;;
esac
[ "$KEEP" -ge 1 ] || die "--keep muss mindestens 1 sein"

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

if [ -z "$AGE_RECIPIENT_VALUE" ] && [ "$INSECURE_PLAIN" -eq 0 ]; then
  die "kein age-Empfaenger gesetzt; AGE_RECIPIENT/--age-recipient verwenden oder --insecure-plain bestaetigen"
fi
if [ -n "$AGE_RECIPIENT_VALUE" ] && [ "$INSECURE_PLAIN" -eq 1 ]; then
  die "--insecure-plain kann nicht mit einem age-Empfaenger kombiniert werden"
fi

command -v ssh >/dev/null 2>&1 || die "ssh ist nicht installiert"
if [ -n "$AGE_RECIPIENT_VALUE" ]; then
  command -v age >/dev/null 2>&1 || die "age ist nicht installiert"
fi

mkdir -p "$DEST"
[ -d "$DEST" ] || die "Backup-Ziel ist kein Verzeichnis: $DEST"
DEST="$(cd "$DEST" && pwd)"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_BASE="talos-data-$TIMESTAMP.tar.gz"
if [ -n "$AGE_RECIPIENT_VALUE" ]; then
  FINAL_ARCHIVE="$DEST/$ARCHIVE_BASE.age"
else
  FINAL_ARCHIVE="$DEST/$ARCHIVE_BASE"
fi
[ ! -e "$DEST/$ARCHIVE_BASE" ] && [ ! -e "$DEST/$ARCHIVE_BASE.age" ] || \
  die "Archiv fuer diesen Zeitstempel existiert bereits: $ARCHIVE_BASE"

TEMP_ARCHIVE="$(mktemp "$DEST/.talos-data.XXXXXX")"
REMOTE_DATA="$REMOTE_ROOT/data"
REMOTE_TAR="set -eu; test -d '$REMOTE_DATA'; tar -C '$REMOTE_ROOT' -czf - data"

echo "Backup-Quelle: $REMOTE:$REMOTE_DATA/"
if [ -n "$AGE_RECIPIENT_VALUE" ]; then
  ssh "$REMOTE" "$REMOTE_TAR" | age --encrypt --recipient "$AGE_RECIPIENT_VALUE" >"$TEMP_ARCHIVE"
else
  echo "WARNUNG: unverschluesseltes Backup wurde explizit erlaubt." >&2
  ssh "$REMOTE" "$REMOTE_TAR" >"$TEMP_ARCHIVE"
fi

[ -s "$TEMP_ARCHIVE" ] || die "Backup-Archiv ist leer"
mv "$TEMP_ARCHIVE" "$FINAL_ARCHIVE"
TEMP_ARCHIVE=""

archive_number=0
while IFS= read -r old_archive; do
  archive_number=$((archive_number + 1))
  if [ "$archive_number" -gt "$KEEP" ]; then
    rm -f -- "$old_archive"
    echo "Rotation: entfernt $old_archive"
  fi
done < <(
  find "$DEST" -maxdepth 1 -type f \
    \( -name 'talos-data-*.tar.gz' -o -name 'talos-data-*.tar.gz.age' \) \
    -print | LC_ALL=C sort -r
)

echo "Backup erstellt: $FINAL_ARCHIVE"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$FINAL_ARCHIVE"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$FINAL_ARCHIVE"
else
  die "weder sha256sum noch shasum ist installiert"
fi
