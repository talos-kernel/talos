"""Der Assistent wird komplett ohne Terminal und ohne Netz gefahren.

Beides ist Absicht: ein Einrichtungsschritt, der sich nur von Hand prüfen lässt,
wird genau einmal geprüft — beim Schreiben. Die Fälschungen unten sind deshalb
so schmal wie die injizierten Abhängigkeiten (`stdin`, `stdout`, `http`).
"""
from __future__ import annotations

import io

from talos import config
from talos.setup_wizard import (
    ANTHROPIC_MODELS,
    EXIT_OK,
    HttpError,
    HttpResponse,
    LocalRuntimes,
    mask_token,
    run_setup,
)

# Formgültige Tokens (Ziffern, ':', dann >= 30 erlaubte Zeichen) — beide erfunden.
GOOD_TOKEN = "123456789:AAHfake-token-value-0123456789abcdef"
OTHER_TOKEN = "987654321:BBZanother-token-value-0123456789abc"
# Erfunden, aber in der echten Form. Enthält absichtlich weder „777" noch „111",
# damit die Identitäts-Tests weiterhin beweisen, was sie behaupten.
API_KEY = "sk-ant-api03-fake-key-value-0123456589abcdef"
OTHER_API_KEY = "sk-proj-another-fake-key-value-0123456589ab"

# Kein `claude`, kein `hermes` — der Zustand einer frischen fremden Installation.
NO_CLI = LocalRuntimes()
CLAUDE_CLI = LocalRuntimes(claude="/usr/local/bin/claude")
HERMES_CLI = LocalRuntimes(hermes="/home/someone/.local/bin/hermes")

# Der Modell-Schritt ohne lokale CLI: Enter = anthropic-api, Schlüssel, Enter = Modell.
MODEL_ANSWERS = ["", API_KEY, ""]
# Mit lokaler CLI: Enter behält sie, Enter nimmt das erste Modell. Kein Schlüssel.
KEEP_LOCAL_ANSWERS = ["", ""]


class FakeStdin:
    """stdin mit behauptetem Terminal; die Antworten kommen aus einer Liste.

    Leere Liste = EOF, also derselbe Fall wie ein abgebrochener Terminal-Dialog.
    """

    def __init__(self, answers: list[str], *, tty: bool = True) -> None:
        self._answers = list(answers)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        if not self._answers:
            return ""
        return f"{self._answers.pop(0)}\n"


def ok(result: object) -> HttpResponse:
    return HttpResponse(200, {"ok": True, "result": result})


UNAUTHORIZED = HttpResponse(401, {"ok": False, "description": "Unauthorized"})
BOT = ok({"id": 42, "is_bot": True, "first_name": "Talos", "username": "talos_guard_bot"})


def message(user_id: int, *, name: str = "Operator", username: str = "operator", update_id: int = 1):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "chat": {"id": user_id},
            "from": {"id": user_id, "is_bot": False, "first_name": name, "username": username},
            "text": "hallo",
        },
    }


class FakeHttp:
    """Antworten je aufgerufenem Endpunkt. Die letzte bleibt stehen und wiederholt sich.

    Ein Eintrag darf eine Exception sein — so wird ein Netzfehler geprüft, ohne
    dass jemals ein Socket entsteht. `models` bedient beide Schlüsselprüfungen:
    Anthropic (`/v1/models`) und OpenAI-kompatibel (`/models`) enden gleich.
    """

    def __init__(
        self, *, me: list | None = None, updates: list | None = None, models: list | None = None
    ) -> None:
        self._queues = {
            "getMe": list(me if me is not None else [BOT]),
            "getUpdates": list(updates if updates is not None else [ok([])]),
            "models": list(models if models is not None else [HttpResponse(200, {"data": []})]),
        }
        self.calls: list[str] = []
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def get(
        self, url: str, params: dict, timeout: float, headers: dict | None = None
    ) -> HttpResponse:
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        queue = self._queues.get(method) or [ok([])]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def drive(answers, http, tmp_path, *, extra=(), tty=True, runtimes=NO_CLI):
    """Ein kompletter Lauf. Rückgabe: Exit-Code, gesammelte Ausgabe, Zielpfad.

    `runtimes` wird IMMER gesetzt: würde der Assistent hier den echten Rechner
    absuchen, hinge das Ergebnis daran, ob auf der Testmaschine zufällig eine
    `claude`-CLI liegt — und ein Test, der auf einem Rechner grün ist und auf dem
    anderen etwas anderes prüft, beweist nichts.
    """
    out = tmp_path / "secrets" / "talos.env"
    stdout = io.StringIO()
    code = run_setup(
        ["--out", str(out), *extra],
        stdin=FakeStdin(answers, tty=tty),
        stdout=stdout,
        http=http,
        runtimes=runtimes,
    )
    return code, stdout.getvalue(), out


def test_stops_without_a_terminal_and_names_the_environment_variables(tmp_path) -> None:
    http = FakeHttp()
    code, text, out = drive([], http, tmp_path, tty=False)

    assert code != EXIT_OK
    assert "TELEGRAM_BOT_TOKEN" in text
    assert "TALOS_ALLOWED_PRINCIPALS" in text
    # Ohne den Denkweg führt der Weg von Hand in eine unvollständige Konfiguration:
    # der Agent stünde da, ohne womit zu denken.
    assert "TALOS_MODEL_PROVIDER" in text
    assert "TALOS_MODEL" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "OPENAI_API_KEY" in text
    assert "TALOS_BASE_URL_OPENAI_API" in text
    assert not out.exists()
    assert http.calls == []


def test_rejected_token_is_asked_again_and_only_the_valid_one_is_written(tmp_path) -> None:
    http = FakeHttp(me=[UNAUTHORIZED, BOT], updates=[ok([message(777)])])
    code, text, out = drive(
        ["nonsense", OTHER_TOKEN, GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path
    )

    assert code == EXIT_OK
    assert "401" in text
    # „nonsense" fällt an der Form durch und kostet keinen Netzaufruf.
    assert http.calls.count("getMe") == 2
    assert f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}" in out.read_text(encoding="utf-8")
    assert OTHER_TOKEN not in out.read_text(encoding="utf-8")


def test_valid_token_is_confirmed_against_getme_and_shows_the_bot(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, text, out = drive([GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path)

    assert code == EXIT_OK
    assert http.calls[0] == "getMe"
    assert GOOD_TOKEN in http.urls[0]
    assert "Talos" in text and "@talos_guard_bot" in text
    assert "TELEGRAM_BOT_USERNAME=talos_guard_bot" in out.read_text(encoding="utf-8")


def test_captured_id_becomes_the_allowlist_entry_without_being_typed(tmp_path) -> None:
    answers = [GOOD_TOKEN, "y", *MODEL_ANSWERS]
    http = FakeHttp(updates=[ok([message(777)])])
    code, _, out = drive(answers, http, tmp_path)

    assert code == EXIT_OK
    assert "TALOS_ALLOWED_PRINCIPALS=telegram:777" in out.read_text(encoding="utf-8")
    # Die Kennung stand in keiner Eingabe — sie kam aus der echten Nachricht.
    assert not any("777" in answer for answer in answers)


def test_declining_is_that_you_writes_nothing(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, text, out = drive([GOOD_TOKEN, "n"], http, tmp_path)

    assert code != EXIT_OK
    assert not out.exists()  # ohne ausdrückliches Ja entsteht keine Allowlist
    assert "not confirmed" in text


def test_several_senders_are_listed_and_only_the_chosen_one_is_written(tmp_path) -> None:
    http = FakeHttp(updates=[ok([
        message(111, name="Fremd", username="fremd", update_id=1),
        message(222, name="Zweiter", username="zweiter", update_id=2),
    ])])
    code, text, out = drive([GOOD_TOKEN, "2", *MODEL_ANSWERS], http, tmp_path)

    assert code == EXIT_OK
    assert "1) Fremd (@fremd), id 111" in text
    assert "2) Zweiter (@zweiter), id 222" in text
    written = out.read_text(encoding="utf-8")
    assert "TALOS_ALLOWED_PRINCIPALS=telegram:222" in written
    assert "111" not in written


def test_existing_configuration_is_not_overwritten_without_consent(tmp_path) -> None:
    out = tmp_path / "talos.env"
    before = "TELEGRAM_BOT_TOKEN=old-secret\nTALOS_ALLOWED_PRINCIPALS=telegram:1\n"
    out.write_text(before, encoding="utf-8")
    http = FakeHttp()
    stdout = io.StringIO()

    code = run_setup(
        ["--out", str(out)], stdin=FakeStdin(["n"]), stdout=stdout, http=http, runtimes=NO_CLI
    )

    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8") == before
    assert http.calls == []  # ohne Zustimmung wird nicht einmal gefragt
    assert "old-secret" not in stdout.getvalue()


def test_written_file_is_mode_0600(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, _, out = drive([GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path)

    assert code == EXIT_OK
    assert out.stat().st_mode & 0o777 == 0o600


def test_token_never_appears_in_clear_text_in_the_output(tmp_path) -> None:
    # Der erste Versuch scheitert am Netz — und `requests` zitiert dabei die URL,
    # in der der Token steht. Genau dieser Weg ist das realistische Leck.
    leaky = HttpError(f"Max retries exceeded with url: /bot{GOOD_TOKEN}/getMe")
    http = FakeHttp(me=[leaky, BOT], updates=[ok([message(777)])])
    code, text, out = drive([GOOD_TOKEN, GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path)

    assert code == EXIT_OK
    assert GOOD_TOKEN not in text
    assert "[REDACTED]" in text
    assert f"TELEGRAM_BOT_TOKEN={mask_token(GOOD_TOKEN)}" in text
    # In der Datei muss er selbstverständlich vollständig stehen.
    assert f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}" in out.read_text(encoding="utf-8")


def test_network_failure_is_not_reported_as_an_invalid_token(tmp_path) -> None:
    http = FakeHttp(me=[HttpError("connection refused"), BOT], updates=[ok([message(777)])])
    code, text, _ = drive([GOOD_TOKEN, GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path)

    assert code == EXIT_OK
    assert "network problem, not a wrong token" in text
    assert "401" not in text


def test_timeout_offers_the_manual_fallback(tmp_path) -> None:
    http = FakeHttp(updates=[ok([])])
    code, text, out = drive(
        [GOOD_TOKEN, "777", *MODEL_ANSWERS], http, tmp_path, extra=["--wait", "0"]
    )

    assert code == EXIT_OK
    assert "Fallback" in text
    assert "TALOS_ALLOWED_PRINCIPALS=telegram:777" in out.read_text(encoding="utf-8")
    # Auch bei Zeitlimit 0 wird einmal nachgesehen: eine wartende Nachricht darf
    # nicht allein wegen der Uhr unsichtbar bleiben.
    assert http.calls.count("getUpdates") == 1


def test_empty_allowlist_is_refused(tmp_path) -> None:
    http = FakeHttp(updates=[ok([])])
    code, text, out = drive([GOOD_TOKEN, ""], http, tmp_path, extra=["--wait", "0"])

    assert code != EXIT_OK
    assert not out.exists()
    assert "open to everyone" in text


def test_non_numeric_manual_id_is_refused(tmp_path) -> None:
    http = FakeHttp(updates=[ok([])])
    code, _, out = drive([GOOD_TOKEN, "telegram:777"], http, tmp_path, extra=["--wait", "0"])

    assert code != EXIT_OK
    assert not out.exists()


def test_bots_are_not_offered_as_the_operator(tmp_path) -> None:
    echo = message(555, name="EchoBot", username="echo_bot")
    echo["message"]["from"]["is_bot"] = True
    http = FakeHttp(updates=[ok([echo]), ok([message(777)])])
    code, _, out = drive([GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path, extra=["--wait", "5"])

    assert code == EXIT_OK
    assert "TALOS_ALLOWED_PRINCIPALS=telegram:777" in out.read_text(encoding="utf-8")


def test_unwritable_target_stops_instead_of_crashing(tmp_path) -> None:
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")
    http = FakeHttp(updates=[ok([message(777)])])
    stdout = io.StringIO()

    code = run_setup(
        ["--out", str(blocked / "talos.env")],
        stdin=FakeStdin([GOOD_TOKEN, "y", *MODEL_ANSWERS]),
        stdout=stdout,
        http=http,
        runtimes=NO_CLI,
    )

    assert code != EXIT_OK
    assert "could not write" in stdout.getvalue()


def test_finish_says_that_nothing_was_started(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    _, text, _ = drive([GOOD_TOKEN, "y", *MODEL_ANSWERS], http, tmp_path)

    assert "not running" in text
    assert "python -m talos" in text


def test_unknown_argument_stops_with_usage(tmp_path) -> None:
    stdout = io.StringIO()
    code = run_setup(["--start"], stdin=FakeStdin([]), stdout=stdout, http=FakeHttp())

    assert code != EXIT_OK
    assert "usage:" in stdout.getvalue()


def test_leading_setup_word_is_accepted(tmp_path) -> None:
    out = tmp_path / "talos.env"
    http = FakeHttp(updates=[ok([message(777)])])
    stdout = io.StringIO()

    code = run_setup(
        ["setup", "--out", str(out)],
        stdin=FakeStdin([GOOD_TOKEN, "y", *MODEL_ANSWERS]),
        stdout=stdout,
        http=http,
        runtimes=NO_CLI,
    )

    assert code == EXIT_OK
    assert out.is_file()


def test_mask_shows_six_and_four_characters_at_most() -> None:
    assert mask_token(GOOD_TOKEN) == "123456…cdef"
    assert mask_token("short") == "…"
    assert mask_token("") == ""


# ------------------------------------------------------------------- Modell
def start_of_model_step(answers, http, tmp_path, *, runtimes=NO_CLI):
    """Führt bis in den Modell-Schritt: Token bestätigt, Identität eingefangen."""
    return drive([GOOD_TOKEN, "y", *answers], http, tmp_path, runtimes=runtimes)


def test_local_claude_cli_is_the_suggestion_and_enter_keeps_it(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, text, out = start_of_model_step(
        KEEP_LOCAL_ANSWERS, http, tmp_path, runtimes=CLAUDE_CLI
    )

    assert code == EXIT_OK
    written = out.read_text(encoding="utf-8")
    assert "/usr/local/bin/claude" in text  # zuerst gezeigt, was schon da ist
    assert "empty = claude-cli" in text  # ... und als Vorschlag angeboten
    assert "TALOS_MODEL_PROVIDER=claude-cli" in written
    # Der lokale Weg benutzt die vorhandene Anmeldung — es entsteht KEIN Schlüssel.
    assert "ANTHROPIC_API_KEY" not in written
    assert "OPENAI_API_KEY" not in written
    assert "TALOS_BASE_URL_OPENAI_API" not in written
    # Und er wird auch nicht gegen eine API geprüft: es gibt nichts zu prüfen.
    assert http.calls.count("models") == 0


def test_local_hermes_cli_keeps_the_provider_name_talos_already_uses(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, _, out = start_of_model_step(
        KEEP_LOCAL_ANSWERS, http, tmp_path, runtimes=HERMES_CLI
    )

    assert code == EXIT_OK
    written = out.read_text(encoding="utf-8")
    assert f"TALOS_MODEL_PROVIDER={config.DEFAULT_MODEL_PROVIDER}" in written
    assert f"TALOS_MODEL={config.DEFAULT_MODEL}" in written
    assert "API_KEY" not in written


def test_without_a_local_cli_the_suggestion_is_the_own_api_key(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, text, out = start_of_model_step(MODEL_ANSWERS, http, tmp_path)

    assert code == EXIT_OK
    assert "empty = anthropic-api" in text
    # Die CLI-Wege stehen gar nicht erst zur Wahl, wenn keine CLI da ist.
    assert "1) claude-cli" not in text
    assert "1) hermes" not in text
    assert "TALOS_MODEL_PROVIDER=anthropic-api" in out.read_text(encoding="utf-8")
    assert ANTHROPIC_MODELS == (
        "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
        "claude-opus-4-8", "claude-fable-5-1",
    )


def test_chosen_provider_model_and_key_land_under_the_promised_names(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, _, out = start_of_model_step(["anthropic-api", API_KEY, "2"], http, tmp_path)

    assert code == EXIT_OK
    written = out.read_text(encoding="utf-8")
    assert "TALOS_MODEL_PROVIDER=anthropic-api" in written
    assert "TALOS_MODEL=claude-sonnet-5" in written
    assert f"ANTHROPIC_API_KEY={API_KEY}" in written
    # Der Schlüssel ging als Kopfzeile mit, nicht in der URL — sonst stünde er im Log.
    assert http.headers[-1].get("x-api-key") == API_KEY
    assert "https://api.anthropic.com/v1/models" in http.urls[-1]


def test_openai_route_writes_its_own_key_name_and_base_url(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, _, out = start_of_model_step(
        ["openai-api", "https://api.deepseek.com/v1", OTHER_API_KEY, "deepseek-chat"],
        http, tmp_path,
    )

    assert code == EXIT_OK
    written = out.read_text(encoding="utf-8")
    assert "TALOS_MODEL_PROVIDER=openai-api" in written
    assert f"OPENAI_API_KEY={OTHER_API_KEY}" in written
    assert "TALOS_BASE_URL_OPENAI_API=https://api.deepseek.com/v1" in written
    assert "TALOS_MODEL=deepseek-chat" in written
    assert http.headers[-1].get("Authorization") == f"Bearer {OTHER_API_KEY}"


def test_invalid_api_key_is_asked_again_and_only_the_valid_one_is_written(tmp_path) -> None:
    http = FakeHttp(
        updates=[ok([message(777)])],
        models=[UNAUTHORIZED, HttpResponse(200, {"data": []})],
    )
    code, text, out = start_of_model_step(
        ["", OTHER_API_KEY, API_KEY, ""], http, tmp_path
    )

    assert code == EXIT_OK
    assert "rejected this key (401)" in text
    assert http.calls.count("models") == 2
    written = out.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={API_KEY}" in written
    assert OTHER_API_KEY not in written


def test_network_failure_on_the_key_check_is_not_reported_as_a_wrong_key(tmp_path) -> None:
    http = FakeHttp(
        updates=[ok([message(777)])],
        models=[HttpError("connection refused"), HttpResponse(200, {"data": []})],
    )
    code, text, out = start_of_model_step(["", API_KEY, API_KEY, ""], http, tmp_path)

    assert code == EXIT_OK
    assert "network problem, not a wrong key" in text
    assert "rejected this key" not in text
    assert "401" not in text
    assert f"ANTHROPIC_API_KEY={API_KEY}" in out.read_text(encoding="utf-8")


def test_api_key_never_appears_in_clear_text_in_the_output(tmp_path) -> None:
    # Dasselbe realistische Leck wie beim Bot-Token: der Anbieter-Fehler zitiert die
    # Anfrage — und in der steht der Schlüssel.
    leaky = HttpError(f"Max retries exceeded (x-api-key: {API_KEY})")
    http = FakeHttp(
        updates=[ok([message(777)])],
        models=[leaky, HttpResponse(200, {"data": []})],
    )
    code, text, out = start_of_model_step(["", API_KEY, API_KEY, ""], http, tmp_path)

    assert code == EXIT_OK
    assert API_KEY not in text
    assert "[REDACTED]" in text
    assert f"ANTHROPIC_API_KEY={mask_token(API_KEY)}" in text
    # In der Datei muss er selbstverständlich vollständig stehen.
    assert f"ANTHROPIC_API_KEY={API_KEY}" in out.read_text(encoding="utf-8")


def test_a_key_in_an_unknown_shape_is_masked_too(tmp_path) -> None:
    # Kein `sk-`-Präfix: das Muster greift nicht, die Konsole muss den Wert kennen.
    odd_key = "deepseek-0123456589abcdefghijklmnop"
    leaky = HttpError(f"Max retries exceeded (Authorization: Bearer {odd_key})")
    http = FakeHttp(
        updates=[ok([message(777)])],
        models=[leaky, HttpResponse(200, {"data": []})],
    )
    code, text, out = start_of_model_step(
        ["openai-api", "", odd_key, odd_key, ""], http, tmp_path
    )

    assert code == EXIT_OK
    assert odd_key not in text
    assert f"OPENAI_API_KEY={odd_key}" in out.read_text(encoding="utf-8")


def test_empty_api_key_stops_instead_of_writing_half_a_configuration(tmp_path) -> None:
    http = FakeHttp(updates=[ok([message(777)])])
    code, text, out = start_of_model_step(["", ""], http, tmp_path)

    assert code != EXIT_OK
    assert not out.exists()
    assert "no api key given" in text
