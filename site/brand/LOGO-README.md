# Talos — Markenzeichen

> ⚠️ **Stand 5. August 2026: die Rasterdateien zeigen nicht mehr die Maske.**
> Auf Wunsch des Betreibers tragen alle benutzten Icons jetzt das **Porträt des Wächters**
> (`assets/talos-character.png`), als dunkle Gravur auf Bronze — die Siegel-Idee dieser
> Datei, nur mit einem Gesicht statt einer Maske.
>
> | | zeigt heute |
> |---|---|
> | `favicon.ico`, `apple-touch-icon.png`, `icon-192/512.png`, `og-logo.png` | Porträt |
> | `assets/talos-icon*.png`, `site/talos-icon.png` | Porträt |
> | **alle `.svg` in diesem Ordner**, `print/`, `png/` | **weiterhin die Maske** |
>
> Die Vektorfassungen sind handgezeichnet und lassen sich aus einem Rasterbild nicht
> herstellen — sie bleiben unangetastet, statt sie durch einen Auto-Trace zu ersetzen.
> **Damit ist die Marke derzeit zweigeteilt.** Wer sie wieder vereinheitlichen will, hat
> zwei Wege: das Porträt als Vektor neu zeichnen lassen, oder die Rasterdateien
> zurückholen (`git checkout <commit vor dem Wechsel> -- site/brand assets`).
>
> ⚠️ Bei **16 px** trägt das Porträt nicht — feine Strichzeichnung überlebt diese Grösse
> nicht, die Maske tat es. Die `favicon.ico` enthält 16/32/48; die 16er ist ein
> Bronzefleck.
>
> Alles unterhalb dieser Zeile beschreibt weiterhin die **Maske** und gilt für die
> Vektordateien.

---

Bronzemaske mit bernsteinfarbenen Augen. Handgezeichnetes Vektor-SVG, kein Auto-Trace,
kein eingebettetes Raster. Alle Dateien in diesem Ordner stammen aus derselben Geometrie.

---

## Die Idee

Talos ist im Mythos der bronzene Automat, der Kreta dreimal täglich umrundete und nichts an
Land liess, was nicht dorthin gehörte. Die Marke ist genau das: **ein gegossenes Gesicht, kein
Assistent.** Flächen statt Rundungen, Kanten wie aus der Form geschlagen, ein Gesichtsausdruck,
der urteilt statt begrüsst.

**Der Kniff:** Brauensteg und Nasenrücken sind die einzigen erhabenen Flächen der Maske — die
Stellen, die beim gegossenen Helm das Licht fangen. Zusammen ergeben sie ein **T**. Das ist
gleichzeitig der Initial der Marke und die tatsächliche Öffnungsform eines korinthischen Helms.
Zwei Lesarten, eine Form, nichts hinzuerfunden.

**Bernstein liegt ausschliesslich in den Augen.** Sie sind der einzige helle Punkt in der ganzen
Marke — so wie im Installer, wo genau diese eine Zeile der ASCII-Maske bernsteinfarben gedruckt
wird. Wer die Augen einfärbt oder aufhellt, nimmt der Marke ihren einzigen Akzent.

In der einfarbigen Fassung wird das T zur **eingravierten Rille** — die Marke wird zum Siegel.
Das ist der Härtetest und zugleich die Prägevorlage.

---

## Palette

| Rolle | HEX | RGB | CMYK (Annäherung) | Pantone (Annäherung) |
|---|---|---|---|---|
| Bronze (Körper) | `#C08A47` | 192 138 71 | 0 / 28 / 63 / 25 | 7563 C |
| Bernstein (Augen) | `#E8A33D` | 232 163 61 | 0 / 30 / 74 / 9 | 143 C |
| Bronze tief (helle Gründe) | `#7E5518` | 126 85 24 | 0 / 33 / 81 / 51 | 7561 C |
| Patina (UI-Akzent, nicht in der Marke) | `#5E9C86` | 94 156 134 | 40 / 0 / 14 / 39 | 5555 C |
| Schnitt (Augenhöhlen, Mund) | `#14100B` | 20 16 11 | 0 / 19 / 45 / 92 | — |
| Grund (Website) | `#0C0E0D` | 12 14 13 | 15 / 0 / 7 / 95 | — |

Die Werte stammen aus `site/index.html` (`--bronze`, `--amber`, `--patina`, `--ground`) — die
Marke und die Website teilen eine Palette.

**Für den Druck:** CMYK ist rechnerisch umgesetzt, nicht profiliert. Vor der Auflage andrucken.
Für Tiefschwarz auf grossen Flächen `60 / 40 / 40 / 100` statt `15 / 0 / 7 / 95` verwenden.
Für Prägung, Siebdruck und Gravur ausschliesslich `talos-mark-mono.svg` benutzen — dort sind
Augen, Mund und die T-Rille echte Aussparungen in einer einzigen Kontur.

---

## Konstruktion

Alles ist auf ein **64er-Raster** gezeichnet, alle Leitmasse liegen auf Vielfachen von 4.
Damit fallen 16 px, 32 px und 64 px exakt auf Pixelkanten — deshalb bleibt das Favicon scharf.

```
viewBox        0 0 64 64
Maske          x 8 … 56   (Breite 48)      y 4 … 60   (Höhe 56)
Augenhöhlen    x 11 … 27  und  37 … 53     y 22,5 … 35,5
Nasensteg      8 Einheiten zwischen den Höhlen (x 28 … 36)
Brauensteg     x 14 … 50                   y 16,5 … 22
Mund           x 24 … 40                   y 47 … 50,6
```

Die Augen sind korinthisch geschnitten: aussen hoch, innen schmal, um 12° geneigt. Der
Schnittwinkel ist das, was die Maske ernst macht statt freundlich.

**Geometrie-Quelle.** Alle Dateien in diesem Ordner sind aus diesen sechs Pfaden gebaut. Wer
die Marke neu aufbauen oder in ein anderes Format überführen muss, braucht nichts weiter:

```
Maske        M24 4h16l16 8v20l-4 12-8 12-8 4h-8l-8-4-8-12-4-12V12Z
Nasenschatten M27 21h10v21l-5 5-5-5Z
Steg (T)     M15.5 16.5h33L50 18v2.5L48.5 22H35v18l-3 3.5-3-3.5V22H15.5L14 20.5V18Z
Höhle links  M11 22.5 27 26.5v8L12 35.5Z
Höhle rechts M53 22.5 37 26.5v8l15 1Z
Mund         M25.4 47h13.2a1.4 1.4 0 0 1 1.4 1.4v.8a1.4 1.4 0 0 1-1.4 1.4H25.4
             a1.4 1.4 0 0 1-1.4-1.4v-.8A1.4 1.4 0 0 1 25.4 47Z
Auge         rect 10.5 x 5.6, rx 2, translate(19 29.6) rotate(12)  bzw.
                                    translate(45 29.6) rotate(-12)
```

**Schutzzone:** rundum mindestens **ein Viertel der Markenhöhe** frei. Beim Lockup mindestens
die **halbe Versalhöhe** (50 Einheiten der Lockup-Datei). Der viewBox enthält bereits 8
Einheiten seitlich und 4 Einheiten oben/unten — die Schutzzone kommt darüber hinaus.

**Mindestgrössen**

| Anwendung | Minimum | Datei |
|---|---|---|
| Bildschirm, sehr klein | 16 px | `talos-mark-micro.svg` / `favicon.ico` |
| Bildschirm, normal | ab 32 px | `talos-mark.svg` |
| Lockup Bildschirm | 96 px Breite | `talos-lockup-*.svg` |
| App-Kachel | ab 64 px, sinnvoll ab 128 px | `talos-app-icon.svg` |
| Druck Bildmarke | 7 mm Breite | `print/talos-mark.pdf` |
| Druck Prägung/Gravur | 10 mm Breite | `print/talos-mark-mono.pdf` |
| Druck Lockup | 26 mm Breite | `print/talos-lockup-*.pdf` |

---

## Wortmarke

**Cinzel**, Versalien, Gewicht 620, Laufweite **0,16 em** — dieselbe Inschriftenschrift, die
die Website für Marke und Auszeichnungen verwendet (`site/fonts/cinzel-var.woff2`).

Die Glyphen sind **in Pfade ausgebaut**. In keiner Logodatei steht ein `<text>`-Element; die
Marke braucht auf keinem System eine installierte Schrift.

Im Lockup gilt: **Markenhöhe = 1,52 × Versalhöhe**, **Abstand = 0,44 × Versalhöhe**, die
Versalmitte der Schrift liegt auf der Mitte der Maske.

---

## Dateien

### Vektor (Master)

| Datei | Wofür |
|---|---|
| `talos-mark.svg` | Bildmarke, Vollfarbe. Funktioniert auf hellem und dunklem Grund. |
| `talos-mark-dark.svg` | Für dunkle Gründe: hellerer Körper, zurückgenommene Kontur. |
| `talos-mark-light.svg` | Für helle Gründe: abgedunkelte Bronze wie im Light-Theme der Website. |
| `talos-mark-mono.svg` | Einfarbig schwarz, eine Kontur mit `evenodd`-Aussparungen. Prägung, Stempel, Fax, Gravur. |
| `talos-mark-mono-invers.svg` | Dasselbe in Weiss für dunkle Gründe. |
| `talos-mark-micro.svg` | **Nur bis 24 px.** Auf das Pixelraster gezeichnet, ganzzahlige Kanten, keine Verläufe. |
| `talos-app-icon.svg` | Maske auf dunkler Platte, 72 % Höhe. App- und Store-Kacheln. |
| `talos-wordmark-dark/-light/-mono.svg` | Nur der Schriftzug. viewBox = Versalbox. |
| `talos-lockup-dark/-light/-mono/-mono-invers.svg` | Marke + Schriftzug, horizontal. |

### Raster

```
png/talos-16 24 32 48 64 128 256 512 1024.png   transparent, Bildmarke
png/talos-mono-512.png, talos-mono-invers-512.png
png/talos-lockup-{dark,light,mono}-1600.png
favicon.ico                 16 + 32 + 48, drei echte Rahmen
apple-touch-icon.png        180, dunkle Platte ohne Eckenrundung (iOS rundet selbst)
icon-192.png icon-512.png   PWA-Kacheln
og-logo.png                 1200 × 630, Social-Karte
talos-abnahmebogen.png      Kontaktbogen: alle Varianten und Grössen auf einem Blatt
print/*.pdf                 Vektor-PDF für Druckereien
```

Bis **24 px** zeichnet die Mikrofassung, ab **32 px** der Master. Der Master hat unter 32 px zu
viel Detail für zu wenig Pixel, die Mikrofassung über 24 px zu wenig.

---

## Anwendung auf der Website

`site/index.html` wird von dieser Lieferung **nicht** verändert. Für die Verdrahtung:

```html
<link rel="icon" href="brand/favicon.ico" sizes="any">
<link rel="icon" href="brand/png/talos-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="brand/apple-touch-icon.png">
<meta property="og:image" content="https://talos-agent.ch/brand/og-logo.png">
```

Für die Marke in der Navigation (`nav .mark`) eignet sich `talos-mark.svg` in 22–26 px Höhe
neben dem bestehenden Cinzel-Schriftzug — dann wird das dort verwendete Mono-Glyph überflüssig.

---

## Illustrator und Druck

Die SVGs enthalten ausschliesslich `svg`, `title`, `defs`, `linearGradient`, `stop`, `g`,
`path` und `rect`. Keine CSS-Klassen, kein `<style>`, keine Filter, keine Masken, kein
`clipPath`, keine eingebetteten Bilder. Illustrator, Affinity, Figma und Inkscape öffnen die
Dateien verlustfrei.

Die Verläufe sind einfache lineare Verläufe in `userSpaceOnUse`. Wenn eine Druckerei
Volltonfarben verlangt: `talos-mark-mono.svg` nehmen oder die drei Verläufe durch die
Mittelwerte `#C08A47` (Körper), `#D4A264` (Steg) und `#E8A33D` (Augen) ersetzen — die Marke
verliert dabei Plastizität, aber keine Lesbarkeit.

---

## Do

- Auf ruhigem Grund platzieren: dunkles Grau/Schwarz oder ein helles Warmgrau.
- Unter 32 px die Mikrofassung nehmen, nicht den Master herunterskalieren.
- Auf Fotos und unruhigen Flächen die einfarbige Fassung verwenden.
- Für Prägung, Gravur und Siebdruck immer `talos-mark-mono.svg`.

## Don't

- **Die Augen nicht umfärben.** Sie sind der einzige helle Punkt der Marke.
- Nicht neigen, spiegeln, verzerren, oder die Proportionen einzeln ändern.
- Keine Schlagschatten, kein Glow, keine zusätzliche Kontur.
- Nicht auf Bronze- oder Bernsteinflächen setzen — dort fehlt der Kontrast; stattdessen
  `talos-mark-mono-invers.svg`.
- Marke und Schriftzug im Lockup nicht neu anordnen und den Abstand nicht ändern.
- Den Master nicht unter 32 px ausspielen und die Mikrofassung nicht über 24 px.
