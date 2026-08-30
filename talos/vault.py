"""Secure runners and shared path rules for the markdown notes vault.

Path validation lives here so the policy kernel and the filesystem runners derive the
same canonical target.  qmd is always invoked with an argv list; no user input reaches
a shell.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

DEFAULT_VAULT_DIR = Path.home() / ".talos" / "vault"
DEFAULT_QMD_BIN = str(Path.home() / ".local" / "bin" / "qmd")
QMD_COLLECTION = "obsidian"

QUERY_MAX_CHARS = 300
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 10
MAX_SEARCH_OUTPUT_CHARS = 20_000
MAX_GET_BYTES = 64 * 1024
MAX_NOTE_BYTES = 64 * 1024
QMD_SEARCH_TIMEOUT_S = 20
QMD_UPDATE_TIMEOUT_S = 30
SEARCH_FALLBACK_TERMS_MAX = 8

WRITE_CATEGORIES = frozenset({"errors", "gotchas", "decisions", "workflows", "patterns"})
SECRET_CATEGORIES = frozenset(
    {"credential", "credentials", "secret", "secrets", "token", "tokens", "keys", "private"}
)
FRONTMATTER_FIELDS = frozenset(
    {"type", "tags", "projects", "date", "confidence", "last-verified"}
)
_KEBAB_NOTE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):(?:\s*(.*))?$")

# Deliberately targets obvious assignments and well-known token prefixes.  It does
# not claim to be a DLP engine; path/category denial remains the primary boundary.
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"password|passwd|client[_-]?secret|secret|authorization)\b"
    r"(\s*[:=]\s*)([^\s,}\]]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9._-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,})\b"
)


class VaultPathError(ValueError):
    """A requested path is outside the public markdown surface of the vault."""


def _root(vault_dir: str | os.PathLike[str]) -> Path:
    return Path(vault_dir).expanduser().resolve(strict=False)


def _path_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text or "\x00" in text or "\\" in text:
        raise VaultPathError("Vault-Pfad fehlt oder enthält ungültige Zeichen")
    if text.startswith("qmd://"):
        parsed = urlsplit(text)
        if (
            parsed.scheme != "qmd"
            or parsed.netloc != QMD_COLLECTION
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise VaultPathError("nur qmd://obsidian/<pfad>.md ist zulässig")
        text = unquote(parsed.path[1:])
    elif "://" in text:
        raise VaultPathError("nur qmd://obsidian/<pfad>.md ist zulässig")
    return text


def _blocked_parts(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    return any(
        not part or part in {".", ".."} or part.startswith(".") or part in SECRET_CATEGORIES
        for part in lowered
    )


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise VaultPathError(f"Symlink als Schreibziel gesperrt: {current}")


def canonical_vault_path(
    raw: object,
    vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR,
    *,
    for_write: bool = False,
) -> Path:
    """Return one canonical markdown target or fail closed.

    Existing symlinks are resolved for reads and must remain under the configured
    root. Writes reject every symlink component, including the destination itself.
    """
    text = _path_text(raw)
    supplied = Path(text).expanduser()
    raw_parts = supplied.parts[1:] if supplied.is_absolute() else supplied.parts
    if _blocked_parts(tuple(raw_parts)):
        raise VaultPathError("versteckter, geschützter oder traversierender Vault-Pfad")
    if supplied.suffix != ".md":
        raise VaultPathError("nur Markdown-Dateien (.md) sind zulässig")

    root = _root(vault_dir)
    candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise VaultPathError("Vault-Pfad liegt außerhalb des konfigurierten Roots") from None
    if _blocked_parts(relative.parts):
        raise VaultPathError("versteckter oder geschützter Vault-Pfad")

    if for_write:
        # Writes have a deliberately smaller surface than reads: one kebab-case note
        # directly below one of the five note categories.
        lexical_candidate = candidate.absolute()
        try:
            lexical_relative = lexical_candidate.relative_to(root)
        except ValueError:
            raise VaultPathError("Schreibziel liegt außerhalb des Vault-Roots") from None
        if (
            len(lexical_relative.parts) != 2
            or lexical_relative.parts[0] not in WRITE_CATEGORIES
            or not _KEBAB_NOTE.fullmatch(lexical_relative.parts[1])
        ):
            raise VaultPathError(
                "Schreibziel muss <errors|gotchas|decisions|workflows|patterns>/<kebab-case>.md sein"
            )
        _reject_symlink_components(root, lexical_relative)
        # A symlink resolution that changed the lexical target is never a valid write.
        if resolved != lexical_candidate:
            raise VaultPathError("Symlink als Schreibziel gesperrt")
    return resolved


def canonical_target_from_args(
    tool: str,
    args: Mapping[str, object],
    vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR,
) -> tuple[str, ...]:
    """Shared target extractor used by PolicyKernel and the runners."""
    if tool == "vault_search":
        return ()
    if tool == "vault_get":
        return (str(canonical_vault_path(args.get("path"), vault_dir)),)
    if tool == "vault_write_note":
        return (str(canonical_vault_path(args.get("path"), vault_dir, for_write=True)),)
    raise VaultPathError(f"kein Vault-Tool: {tool}")


def redact_secrets(text: str) -> str:
    redacted = _ASSIGNMENT_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    redacted = _BEARER_SECRET.sub("Bearer [REDACTED]", redacted)
    return _TOKEN_SECRET.sub("[REDACTED]", redacted)


def _bounded(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    suffix = "\n…[Ausgabe gekürzt]"
    return text[: maximum - len(suffix)] + suffix


def _secret_locator(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = unquote(value).replace("\\", "/").casefold()
    parts = tuple(part for part in normalized.split("/") if part)
    return any(part in SECRET_CATEGORIES or part.startswith(".") for part in parts)


def _result_is_secret(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    locator_keys = {"file", "path", "uri", "url", "source", "filename", "document"}
    return any(_secret_locator(value) for key, value in item.items() if str(key).casefold() in locator_keys)


def _safe_search_output(raw: str) -> str:
    try:
        payload = json.loads(raw or "[]")
    except ValueError:
        # Compatibility fallback for an older qmd build that ignores --format json.
        safe_lines = [line for line in (raw or "").splitlines() if not _secret_locator(line)]
        return _bounded(redact_secrets("\n".join(safe_lines).strip()), MAX_SEARCH_OUTPUT_CHARS)

    if isinstance(payload, list):
        payload = [item for item in payload if not _result_is_secret(item)]
    elif isinstance(payload, dict):
        for key in ("results", "matches", "documents"):
            if isinstance(payload.get(key), list):
                payload[key] = [item for item in payload[key] if not _result_is_secret(item)]
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    return _bounded(redact_secrets(rendered), MAX_SEARCH_OUTPUT_CHARS)


def _json_search_items(raw: str) -> list[dict[str, object]] | None:
    """Return qmd result objects, or None when an older qmd did not emit JSON."""
    try:
        payload = json.loads(raw or "[]")
    except ValueError:
        return None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "matches", "documents"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _fallback_search_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in re.findall(r"[^\W_][\w.-]*", query, flags=re.UNICODE):
        folded = term.casefold()
        if len(folded) < 2 or folded in seen:
            continue
        seen.add(folded)
        terms.append(term)
        if len(terms) >= SEARCH_FALLBACK_TERMS_MAX:
            break
    return terms if len(terms) > 1 else []


def _hybrid_query(query: str) -> str:
    """Build qmd's structured lex+vec document without exposing its query grammar."""
    one_line = " ".join(str(query).replace('"', " ").split())
    return f"lex: {one_line}\nvec: {one_line}"


def _result_locator(item: Mapping[str, object]) -> str:
    for key in ("file", "path", "uri", "url", "source", "filename", "document"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _result_score(item: Mapping[str, object]) -> float:
    value = item.get("score", 0)
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _run_qmd(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"qmd Zeitüberschreitung nach {timeout}s") from error
    except OSError as error:
        raise RuntimeError(f"qmd nicht startbar: {error}") from error


def make_vault_search_runner(
    vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR,
    qmd_bin: str = DEFAULT_QMD_BIN,
) -> Callable[[object], str]:
    # vault_dir is intentionally captured too: all vault runners have one explicit
    # configuration even though qmd itself resolves the named collection.
    _root(vault_dir)

    def vault_search(req: object) -> str:
        args = getattr(req, "args")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > QUERY_MAX_CHARS:
            raise ValueError(f"vault_search query muss 1..{QUERY_MAX_CHARS} Zeichen haben")
        limit = args.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not SEARCH_LIMIT_MIN <= limit <= SEARCH_LIMIT_MAX:
            raise ValueError(f"vault_search limit muss eine Ganzzahl {SEARCH_LIMIT_MIN}..{SEARCH_LIMIT_MAX} sein")
        def search(one_query: str) -> str:
            argv = [
                str(qmd_bin), "search", one_query, "-c", QMD_COLLECTION,
                "-n", str(limit), "--format", "json",
            ]
            proc = _run_qmd(argv, timeout=QMD_SEARCH_TIMEOUT_S)
            if proc.returncode != 0:
                detail = _bounded(
                    redact_secrets((proc.stderr or proc.stdout or "unbekannt").strip()), 500
                )
                raise RuntimeError(f"qmd search fehlgeschlagen (rc={proc.returncode}): {detail}")
            return proc.stdout

        raw = search(query)
        primary = _json_search_items(raw)
        if primary is None or primary:
            return _safe_search_output(raw)

        # Semantic rescue comes only after an exact lexical miss. It is deliberately
        # structured (no query-expansion model) and skips the CPU-heavy reranker. If
        # vectors are unavailable or qmd is older, the existing bounded term fallback
        # remains the reliable path.
        hybrid_argv = [
            str(qmd_bin), "query", _hybrid_query(query), "-c", QMD_COLLECTION,
            "-n", str(limit), "--no-rerank", "--format", "json",
        ]
        hybrid_proc = _run_qmd(hybrid_argv, timeout=QMD_SEARCH_TIMEOUT_S)
        if hybrid_proc.returncode == 0:
            hybrid_items = _json_search_items(hybrid_proc.stdout)
            if hybrid_items:
                return _safe_search_output(hybrid_proc.stdout)

        # qmd's lexical search can interpret a natural-language query conjunctively.
        # When it is empty, merge bounded single-term searches and rank documents by
        # how many distinct terms found them, then by qmd's best score.
        merged: dict[str, tuple[dict[str, object], int, float]] = {}
        for term in _fallback_search_terms(query):
            for item in _json_search_items(search(term)) or []:
                if _result_is_secret(item):
                    continue
                locator = _result_locator(item)
                previous = merged.get(locator)
                score = _result_score(item)
                if previous is None:
                    merged[locator] = (item, 1, score)
                else:
                    best_item = item if score > previous[2] else previous[0]
                    merged[locator] = (best_item, previous[1] + 1, max(score, previous[2]))
        ranked = sorted(
            merged.values(),
            key=lambda entry: (-entry[1], -entry[2], _result_locator(entry[0])),
        )
        return _safe_search_output(json.dumps([entry[0] for entry in ranked[:limit]]))

    return vault_search


def _read_markdown(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"Vault-Notiz nicht lesbar: {error}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Vault-Ziel ist keine reguläre Datei")
        if info.st_size > MAX_GET_BYTES:
            raise ValueError(f"Vault-Notiz zu groß (maximal {MAX_GET_BYTES} Bytes)")
        data = os.read(fd, MAX_GET_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_GET_BYTES:
        raise ValueError(f"Vault-Notiz zu groß (maximal {MAX_GET_BYTES} Bytes)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Vault-Notiz ist kein gültiges UTF-8-Markdown") from error


def make_vault_get_runner(
    vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR,
) -> Callable[[object], str]:
    root = _root(vault_dir)

    def vault_get(req: object) -> str:
        path = canonical_vault_path(getattr(req, "args").get("path"), root)
        return redact_secrets(_read_markdown(path))

    return vault_get


def validate_frontmatter(content: str) -> None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Frontmatter fehlt (Dokument muss mit --- beginnen)")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("Frontmatter ist nicht mit --- abgeschlossen") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        match = _FRONTMATTER_KEY.match(line)
        if match:
            key, value = match.group(1), (match.group(2) or "").strip()
            if key in fields:
                raise ValueError(f"Frontmatter-Feld doppelt: {key}")
            fields[key] = value
    missing = sorted(FRONTMATTER_FIELDS - fields.keys())
    empty = sorted(key for key in FRONTMATTER_FIELDS if key in fields and not fields[key])
    if missing or empty:
        details = []
        if missing:
            details.append("fehlt: " + ", ".join(missing))
        if empty:
            details.append("leer: " + ", ".join(empty))
        raise ValueError("Frontmatter unvollständig (" + "; ".join(details) + ")")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise VaultPathError("Symlink als Schreibziel gesperrt")
    if path.parent.resolve(strict=True) != path.parent:
        raise VaultPathError("Symlink im Schreibpfad gesperrt")

    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(path.parent, dir_flags)
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temp_fd: int | None = None
    try:
        try:
            existing = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise VaultPathError("existing write target is not a regular file")
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=dir_fd,
        )
        with os.fdopen(temp_fd, "wb", closefd=True) as handle:
            temp_fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


def make_vault_write_runner(
    vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR,
    qmd_bin: str = DEFAULT_QMD_BIN,
) -> Callable[[object], str]:
    root = _root(vault_dir)

    def vault_write_note(req: object) -> str:
        args = getattr(req, "args")
        path = canonical_vault_path(args.get("path"), root, for_write=True)
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("vault_write_note content muss Text sein")
        data = content.encode("utf-8")
        if len(data) > MAX_NOTE_BYTES:
            raise ValueError(f"Vault-Notiz zu groß (maximal {MAX_NOTE_BYTES} Bytes)")
        validate_frontmatter(content)
        # Der Marker ist Evidenz, keine Kosmetik: die Destill-Bilanz (`distill.py`)
        # zaehlt neu/aktualisiert aus diesem Text — und der stammt vom Runner, der
        # die Datei vorher gesehen hat, nicht aus einer Modellbehauptung. Ein
        # Existenzblick ist hier kein Wettlauf: er faellt keine Entscheidung, er
        # beschriftet sie nur.
        marker = "aktualisiert" if path.exists() else "neu"
        _atomic_write(path, data)

        try:
            proc = _run_qmd([str(qmd_bin), "update"], timeout=QMD_UPDATE_TIMEOUT_S)
        except RuntimeError as error:
            return f"{len(data)} Bytes atomar nach {path} geschrieben ({marker}). Warnung: {error}"
        if proc.returncode != 0:
            detail = _bounded(redact_secrets((proc.stderr or proc.stdout or "unbekannt").strip()), 500)
            return (
                f"{len(data)} Bytes atomar nach {path} geschrieben ({marker}). "
                f"Warnung: qmd update fehlgeschlagen (rc={proc.returncode}): {detail}"
            )
        return f"{len(data)} Bytes atomar nach {path} geschrieben ({marker}); qmd-Index aktualisiert."

    return vault_write_note


# Default-config wrappers keep the runners first-class for tests/embedders. Production
# wiring replaces these with runners built from TalosConfig.
vault_search = make_vault_search_runner()
vault_get = make_vault_get_runner()
vault_write_note = make_vault_write_runner()
