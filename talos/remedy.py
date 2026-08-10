"""Was gerade nicht geht — und was es kostet, es moeglich zu machen.

Der Anlass war eine Antwort, die woertlich stimmte und trotzdem nichts wert war: „✕
failed: could not deliver the answer". Der Betreiber will nicht erfahren, DASS etwas
fehlt. Er will wissen, was er tun muss. Ein Waechter, der eine Luecke meldet und den
Weg daneben verschweigt, hat die Haelfte der Arbeit gemacht und die unangenehmere
Haelfte dem Menschen ueberlassen.

Das Wissen dafuer liegt seit Wochen in `doctor.py`, und zwar vollstaendig: jeder Befund
traegt seine Abhilfe im eigenen Text mit („missing — `pip install ddgs`, no key
needed"). Nur hat das Modell es nie gesehen — `talos doctor` schreibt auf die Konsole
des Betreibers, nicht in den Zug des Agenten. Dieses Modul schliesst genau diese Luecke
und legt kein zweites Wissensregister an: was der Doktor weiss, ist die Wahrheit, und
zwei Register, die dasselbe behaupten, laufen unweigerlich auseinander.

⚠️ **Der Unterschied, an dem hier alles haengt: KANN NICHT ist nicht DARF NICHT.**

Eine fehlende Bibliothek ist ein *Mangel*. Dort ist „geht nicht" faul: die Voraussetzung
steht fest, sie ist benennbar, und meist ist sie ein Befehl weit weg. Der Agent soll den
Weg nennen oder ihn gehen.

Ein Urteil des Kernels ist eine *Entscheidung*. Dort ist „geht nicht" die richtige und
vollstaendige Antwort, und der einzige Weg dahin fuehrt ueber den Betreiber — Freigabe,
Autonomiestufe, ein engerer Pfad. Nie ueber einen Umweg.

Dieses Modul kennt ausschliesslich die erste Sorte. Es liest Diagnosebefunde, niemals
Urteile: es importiert `policy` nicht, ruft `decide` nicht auf und hat keinen Zugang zu
etwas, das eine Erlaubnis ausspricht. Ein Test haelt das fest. Die Trennung ist der
ganze Punkt — ein „so ginge es doch" auf ein Kernel-Nein waere keine Hilfsbereitschaft,
sondern der Anfang vom Ende des Gates.

⚠️ Und der Weg selbst ist keine Vollmacht. `pip install ddgs` ist `run_shell` und geht
durch dieselbe Kette wie jeder andere Befehl. Der Block sagt dem Modell, was moeglich
waere — nicht, dass es erlaubt ist.
"""
from __future__ import annotations

__all__ = ["block", "gaps", "render"]

# Bewusst knapp: der Block steht in jedem Zug. Eine gesunde Installation hat null
# Luecken und damit null Zeilen; fuenf sind schon eine Maschine, die Pflege braucht.
MAX_GAPS = 5
DETAIL_CHARS = 120

HEADER = (
    "[Capabilities this machine is missing right now, and what each one takes. "
    "A lack is not a refusal: do not answer \"I cannot\" and stop — name the step, or "
    "take it. Taking it goes through the gate like anything else.]"
)


def _rank(check) -> int:
    """Fehlt es hart, steht es oben. Sonst geht der blockierende Befund unter den Kuerlücken unter."""
    return 0 if getattr(check, "blocking", False) else 1


def gaps(checks) -> tuple[tuple[str, str], ...]:
    """(Werkzeug/Bereich, Abhilfe) fuer alles, was nicht in Ordnung ist.

    Der Doktor beschriftet seine Befunde bereits mit dem WERKZEUGNAMEN („web_search
    (ddgs)"), nicht mit dem Bibliotheksnamen. Das ist der Grund, warum das Modell mit
    diesen Zeilen etwas anfangen kann: es liest den Namen, den es selbst aufrufen wollte.
    """
    gefunden: list[tuple[int, tuple[str, str]]] = []
    for check in checks:
        zustand = str(getattr(check, "state", "") or "").lower()
        if zustand == "ok":
            continue
        label = str(getattr(check, "label", "") or "?")
        detail = " ".join(str(getattr(check, "detail", "") or zustand).split())
        gefunden.append((_rank(check), (label, detail[:DETAIL_CHARS])))
    gefunden.sort(key=lambda paar: paar[0])
    return tuple(eintrag for _, eintrag in gefunden[:MAX_GAPS])


def render(pairs) -> str:
    """Der Textblock fuer den naechsten Zug — leer, wenn nichts fehlt.

    Nimmt bereits ermittelte Paare, nicht die Befunde: dieselbe Liste speist auch den
    Selbstreview (`review.survey(gaps=…)`). Zwei Stellen, die unabhaengig voneinander
    dieselben Luecken ermitteln, laufen frueher oder spaeter auseinander — und dann
    behauptet der Prompt etwas anderes als der Bericht an den Betreiber.

    Leer heisst leer. Ein Block, der jedes Mal „alles in Ordnung" meldet, ist dieselbe
    Sorte Moebel wie eine Grenzzeile unter jeder Antwort: nach dreissig Wiederholungen
    liest ihn niemand mehr, auch nicht das Modell.
    """
    offen = tuple(pairs)
    if not offen:
        return ""
    zeilen = [HEADER] + [f"  {name}: {weg}" for name, weg in offen]
    return "\n".join(zeilen) + "\n\n"


def block(checks) -> str:
    """Bequemlichkeit fuer den Fall, dass die Befunde direkt vorliegen (CLI, Tests)."""
    return render(gaps(checks))
