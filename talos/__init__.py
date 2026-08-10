"""Talos — autonomer Wächter-Agent. Das Paket; Name und Wesen stehen in SOUL.md.

Die Version steht hier UND in `site/install.sh` (`VERSION=`), weil der Installer
heruntergeladen und ausgeführt wird, bevor es ein Paket gibt, das er fragen könnte.
Zwei Orte für dieselbe Zahl driften auseinander — sie taten es bereits: das Paket sagte
0.0.1, während der Installer 0.2.0-alpha auslieferte. Ein Update-Weg, der Versionen
vergleicht, haette damit die falsche Antwort gegeben.

Deshalb haelt ein Test die beiden zusammen (`tests/test_version.py`). Wer hier hochzaehlt
und den Installer vergisst, bekommt es beim naechsten Lauf gesagt.
"""

__version__ = "0.9.3-alpha"
