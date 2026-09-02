#!/usr/bin/env bash
# Talos — installer.
#
# What this does, in one sentence: it puts everything under ~/talos, runs the full
# test suite and the adversarial suite in front of your eyes, and starts nothing.
#
# What it does NOT do: no sudo, no system files, no service, no autostart, no
# telemetry, no network access beyond the one download below.
#
# You are about to pipe a script from the internet into a shell. Talos' own kernel
# classifies that pattern as risky and would ask you before running it. So: read this
# file first. It is short on purpose, and it is served as plain text so you can.
#
#   curl -fsSL https://talos-agent.ch/install.sh | less

set -euo pipefail

VERSION="0.17.2-alpha"
BASE="${TALOS_BASE:-https://talos-agent.ch}"
TARBALL="${BASE}/dist/talos-${VERSION}.tar.gz"
PREFIX="${TALOS_PREFIX:-$HOME/talos}"

# --- output ------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  BRONZE=$'\033[38;5;179m'; AMBER=$'\033[38;5;214m'; PATINA=$'\033[38;5;108m'
  ERR=$'\033[38;5;167m'; WARN=$'\033[38;5;179m'
  TTY=1
else
  B=""; DIM=""; R=""; BRONZE=""; AMBER=""; PATINA=""; ERR=""; WARN=""
  TTY=0
fi

say()  { printf '%s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$R"; }
die()  { printf '\n  %s✕ stopped:%s %s\n\n' "$ERR" "$R" "$*" >&2; exit 1; }

STEP_N=0
step() {
  STEP_N=$((STEP_N + 1))
  printf '\n  %s%s▸%s %s%s%s\n' "$BRONZE" "$B" "$R" "$B" "$*" "$R"
}
ok() { printf '    %s✓%s %s\n' "$PATINA" "$R" "$*"; }

# A spinner that degrades to a single line when there is no terminal — piping into
# a log should not produce a screenful of control characters.
spin() {
  local msg="$1"; shift
  if [ "$TTY" -eq 0 ]; then
    printf '    %s ... ' "$msg"
    if "$@" >/dev/null 2>&1; then printf 'ok\n'; return 0; else printf 'failed\n'; return 1; fi
  fi
  local frames='◐◓◑◒' i=0 pid rc
  "$@" >/dev/null 2>&1 &
  pid=$!
  printf '\033[?25l'
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r    %s%s%s %s' "$AMBER" "${frames:i++%${#frames}:1}" "$R" "$msg"
    sleep 0.09
  done
  wait "$pid"; rc=$?
  printf '\033[?25h\r\033[K'
  if [ $rc -eq 0 ]; then ok "$msg"; else printf '    %s✕%s %s\n' "$ERR" "$R" "$msg"; fi
  return $rc
}

trap 'printf "\033[?25h"; die "unexpected error on line $LINENO — nothing was started."' ERR
trap 'printf "\033[?25h"' EXIT

# --- the guardian ------------------------------------------------------------
# Drawn once, line by line, because the first thing a guardian does is show up.

mask() {
  local lines=(
    "        ▄▄██████████▄▄        "
    "      ██████████████████      "
    "    ██████████████████████    "
    "   ████▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀████   "
    "   ███  ▄▄▄▄▄      ▄▄▄▄▄  ███ "
    "   ███ ████████  ████████ ███ "
    "   ███  ▀▀▀▀▀      ▀▀▀▀▀  ███ "
    "   ████▄            ▄▄▄▄████  "
    "    ██████▄▄▄▄▄▄▄▄▄███████    "
    "      ████ ▀▀▀▀▀▀▀ █████      "
    "        ▀████████████▀        "
  )
  local eyes=4
  printf '\n'
  for i in "${!lines[@]}"; do
    if [ "$i" -eq $((eyes + 1)) ]; then
      printf '  %s%s%s\n' "$AMBER" "${lines[$i]}" "$R"
    else
      printf '  %s%s%s\n' "$BRONZE" "${lines[$i]}" "$R"
    fi
    [ "$TTY" -eq 1 ] && sleep 0.035
  done
}

[ "$TTY" -eq 1 ] && mask
say ""
say "  ${B}TALOS${R} ${DIM}${VERSION}${R}"
say "  ${DIM}An agent whose actions are bounded, explained and checkable.${R}"
say ""
say "  ${DIM}This installer proves its claims before it finishes, and starts nothing.${R}"

# --- 1. prerequisites --------------------------------------------------------

step "Checking prerequisites"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "Talos needs Python 3.11 or newer. Found: $(python3 -V 2>&1 || echo none)."
ok "$($PY -V) — $(command -v "$PY")"

for c in curl tar; do
  command -v "$c" >/dev/null 2>&1 || die "'$c' is missing."
done
ok "curl, tar available"

if [ -e "$PREFIX" ]; then
  die "$PREFIX already exists. Move or remove it — I do not touch what is not mine."
fi

# --- 2. sources --------------------------------------------------------------

step "Fetching sources"
note "$TARBALL"

TMP="$(mktemp -d)"
trap 'printf "\033[?25h"; rm -rf "$TMP"' EXIT
spin "downloading" curl -fsSL "$TARBALL" -o "$TMP/talos.tar.gz" || die "download failed."

# --- 2a. proof of integrity and origin ---------------------------------------
# ⚠️ Until 0.9.0 this section COMPUTED the checksum, printed it, and told you to
# "compare with <url>" — it never fetched the published sum and never compared. The
# signature was not checked at all. So the one path that pipes a script from the
# internet into a shell had no enforcement, while `talos update` enforced both. That is
# backwards: the installer is the more exposed of the two, and it is the one running on
# a machine that has never seen this project before.
#
# Both are refusals now, not notes. Nothing is unpacked and nothing is executed until
# the archive has proven that it arrived intact AND that it came from us.

if command -v sha256sum >/dev/null 2>&1; then
  SUM="$(sha256sum "$TMP/talos.tar.gz" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  SUM="$(shasum -a 256 "$TMP/talos.tar.gz" | cut -d' ' -f1)"
else
  die "no sha256 implementation (sha256sum/shasum) — I will not unpack what I cannot check."
fi
curl -fsSL "${BASE}/dist/talos-${VERSION}.tar.gz.sha256" -o "$TMP/sum" \
  || die "no published checksum at ${BASE}/dist/talos-${VERSION}.tar.gz.sha256 — refusing."
PUBLISHED="$(cut -d' ' -f1 < "$TMP/sum")"
[ "$SUM" = "$PUBLISHED" ] \
  || die "sha256 mismatch — published ${PUBLISHED:0:16}…, downloaded ${SUM:0:16}…. Nothing was unpacked."
ok "sha256 ${SUM:0:16}…${SUM: -8} matches the published sum"

# The signature answers the other question: the checksum sits next to the archive, so
# whoever can replace one replaces both. The key below does not live on that server.
step "Proving the archive came from us"
curl -fsSL "${BASE}/dist/talos-${VERSION}.tar.gz.sig" -o "$TMP/sig" \
  || die "no signature at ${BASE}/dist/talos-${VERSION}.tar.gz.sig — this release cannot prove its origin."
# ⚠️ Verified with a vetted implementation in a THROWAWAY environment, not with
# hand-written crypto in a shell script and not with the tarball's own dependencies —
# those are exactly what is still unproven at this point. `cryptography` is named here,
# not read from the download.
spin "verifier" "$PY" -m venv "$TMP/verify" || die "could not build the verification environment."
spin "verifier deps" "$TMP/verify/bin/python" -m pip install --quiet cryptography \
  || die "could not install the signature verifier — an install is not the place to skip this."
"$TMP/verify/bin/python" - "$TMP/talos.tar.gz" "$TMP/sig" <<'VERIFY' || die "the release signature does not match this archive. Nothing was unpacked, nothing was changed."
import base64, pathlib, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

RELEASE_PUBLIC_KEY = "Do7lfPckC7pJJtD4BECN/mLPIOqHZVWm/j/MfJOK2hk="
archive = pathlib.Path(sys.argv[1]).read_bytes()
signature = base64.b64decode(pathlib.Path(sys.argv[2]).read_text().strip(), validate=True)
try:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(RELEASE_PUBLIC_KEY)).verify(
        signature, archive
    )
except InvalidSignature:
    raise SystemExit(1)
VERIFY
ok "signature verified — the archive was published by the holder of the release key"

mkdir -p "$PREFIX"
tar -xzf "$TMP/talos.tar.gz" -C "$PREFIX" --strip-components=1
ok "unpacked into $PREFIX"

# --- 3. environment ----------------------------------------------------------

step "Creating an isolated Python environment"
spin "venv" "$PY" -m venv "$PREFIX/.venv" || die "venv failed — is python3-venv installed?"
VPY="$PREFIX/.venv/bin/python"
# Every version pinned, every file hashed, resolved for every platform when the
# release was cut — installed under --require-hashes, so the signature checked
# above also covers what pip fetches now. pip itself is not upgraded first: that
# would be one unpinned download ahead of all the pinned ones.
[ -f "$PREFIX/requirements.lock" ] \
  || die "this archive ships no requirements.lock — refusing to install unpinned dependencies."
spin "dependencies" "$VPY" -m pip install --quiet --require-hashes -r "$PREFIX/requirements.lock" \
  || die "dependencies failed — a hash mismatch here means a package did not arrive as released."
# The proofs below need a test runner. It is a separate lock because the agent
# itself never imports it — installing it here is the price of not asking you
# to take the claims on faith.
[ -f "$PREFIX/requirements-dev.lock" ] \
  || die "this archive ships no requirements-dev.lock — the proofs below would run unpinned."
spin "test runner" "$VPY" -m pip install --quiet --require-hashes -r "$PREFIX/requirements-dev.lock" \
  || die "could not install the test runner — the proofs below would be skipped."
ok "$PREFIX/.venv — no system packages touched"

# --- 4. proof ----------------------------------------------------------------
# The actual reason this installer takes longer than it needs to: you should not
# have to take the claims on faith. Both suites run in full, visibly, and any
# failure aborts the install.

step "Running the test suite"
say ""
( cd "$PREFIX" && "$VPY" -m pytest -q ) || die "tests failed. Nothing configured, nothing started."

if [ -f "$PREFIX/redteam.py" ]; then
  step "Running the adversarial suite"
  say ""
  ( cd "$PREFIX" && "$VPY" redteam.py ) || die "an attack got through. That is a reason to abort."
fi

# --- 5. configuration --------------------------------------------------------

step "Writing an example configuration"
CONF="$PREFIX/talos.env"
if [ ! -f "$CONF" ]; then
  if [ -f "$PREFIX/.env.example" ]; then
    # ⚠️ Die Beispieldatei zeigt die FORM und traegt dafuer einen Platzhalter-Principal
    # (`telegram:000000000`). Kopiert man sie unveraendert, ist die Allowlist nicht leer,
    # sondern enthaelt eine erfundene Kennung — und die Zeile darunter behauptete
    # trotzdem "empty allowlist". Sie war schlicht falsch, und sie hielt die
    # Erstlauf-Hilfe aus: eine GESETZTE Liste ist erschoepfend, also stand jeder frisch
    # Installierte vor "cli:<uid> is not in TALOS_ALLOWED_PRINCIPALS".
    sed 's/^TALOS_ALLOWED_PRINCIPALS=.*/TALOS_ALLOWED_PRINCIPALS=/' \
      "$PREFIX/.env.example" > "$CONF"
  else
    cat > "$CONF" <<'ENV'
TELEGRAM_BOT_TOKEN=
TALOS_ALLOWED_PRINCIPALS=
ENV
  fi
  chmod 600 "$CONF"
fi
ok "$CONF — empty allowlist, mode 600 (the setup below fills it in)"

mkdir -p "$PREFIX/workspace" "$PREFIX/data"
chmod 700 "$PREFIX/data"
ok "$PREFIX/workspace (its working directory) · $PREFIX/data (event log, 0700)"

# --- 6. done — deliberately without starting ---------------------------------

say ""
say "  ${PATINA}Done.${R} Talos is in ${B}$PREFIX${R} — ${WARN}and it is not running.${R}"
say ""
say "  ${DIM}That is not a forgotten step. The switch is yours.${R}"
say ""
say "  Next:"
say "    ${B}1.${R} cd $PREFIX && .venv/bin/python -m talos setup --out $CONF"
say "       ${DIM}asks for the bot token, checks it against Telegram, and waits for${R}"
say "       ${DIM}you to message your bot so it can read your id from the message${R}"
say "       ${DIM}itself. Then it asks which model it should think with.${R}"
say "       ${DIM}Prefer an editor? \$EDITOR $CONF works too — same two keys.${R}"
say ""
say "    ${B}2.${R} .venv/bin/python -m talos"
say ""
say "  ${DIM}Update later: .venv/bin/python -m talos update --check"
say "  It unpacks beside this one, runs both suites in the NEW tree, and only"
say "  switches if they pass. Your talos.env and data stay yours.${R}"
say ""
say "  ${DIM}Stop it any time with /stop. Take an action back with /undo."
say "  Remove it entirely: rm -rf $PREFIX${R}"
say ""
