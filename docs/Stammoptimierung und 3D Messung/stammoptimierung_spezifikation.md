# Spezifikation: Kamera-basierte Stamm-Eindrehoptimierung für eine Zerspanerlinie

**Status:** Fachlich-technische Spezifikation, Version 1.1  
**Ziel:** Einfache, robuste Entscheidung, wie ein Rundholzstamm bei unveränderter Zentrierung eingedreht werden soll, um in einer vorgegebenen Prioritätsrichtung möglichst viel nutzbare Stammgeometrie bereitzustellen.  
**Grundprinzip:** Kein Einschnittplan und keine Produktabmessungen werden in der Optimierungssoftware nachgebildet. Die aktive Prioritätsquelle übermittelt nur, **welche Richtung geometrisch priorisiert werden soll**. Während Entwicklung, Test und Inbetriebnahme ist diese Quelle zunächst das Frontend; später übernimmt die SPS dieselbe fachliche Vorgabe.

**Verbindliches Koordinatensystem:** `x` = Stamm-Längsachse, `y` = horizontal/quer, `z` = vertikal; Scheiben liegen in der `y-z`-Ebene und die Stammrotation erfolgt um `x`.

**Änderung Version 1.1:** Koordinatensystem durchgängig auf die Anlagenkonvention angepasst und Frontend als vorläufige Prioritätsquelle für Entwicklung/Inbetriebnahme spezifiziert.

---

## 1. Zielbild

Die bestehende kamerabasierte Krümmungserkennung wird zu einer einfachen Stamm-Geometrieerfassung erweitert. Aus einer Kamerasequenz während der Stammrotation wird ein grobes, aber geometrisch aussagekräftiges Modell des Stammes aufgebaut.

Der Stamm wird entlang seiner Längsachse in Scheiben zerlegt. Jede Scheibe wird näherungsweise als Ellipse beschrieben. Zusätzlich wird die Lage der Scheibenmitte relativ zur späteren Zentrier-/Maschinenachse erfasst.

Aus diesem Scheibenmodell werden zwei planunabhängige Größen berechnet:

- nutzbare Geometrie **oben/unten**,
- nutzbare Geometrie **links/rechts**.

Die aktive Prioritätsquelle liefert pro Stamm nur einen Prioritätsmodus. Für Entwicklung, Test und Inbetriebnahme wird der Modus zunächst im Frontend gewählt; später liefert die SPS denselben Modus:

- `VORSCHNITT_KRITISCH` → oben/unten maximieren,
- `NACHSCHNITT_KRITISCH` → links/rechts maximieren.

Der Optimierer sucht anschließend den Drehwinkel, bei dem die **schlechteste relevante Scheibe über die Stammlänge** in der priorisierten Richtung den größtmöglichen symmetrisch nutzbaren Bereich um die Zentrierachse bietet.

Die Ausgabe ist bewusst einfach:

1. empfohlene relative Drehung, z. B. `+18°`,
2. geometrischer Gewinn in der priorisierten Richtung, z. B. `+34 mm`,
3. Visualisierung des Stammmodells und der Stelle(n), an denen dieser Gewinn entsteht bzw. die Geometrie begrenzt wird.

---

## 2. Bewusste Abgrenzung

### 2.1 Bestandteil von Version 1

- eine Kamera bzw. die bestehende Kamerakette,
- bestehende Stammsegmentierung,
- bestehendes oder erweitertes Drehwinkel-Tracking,
- Krümmungsinformation,
- Durchmesserverlauf,
- Ovalität und Ovalitätswinkel,
- Scheibenmodell mit konfigurierbarem Abstand,
- geometrische Optimierung eines Drehwinkels,
- Prioritätsvorgabe über eine abstrahierte Quelle; zunächst manuell im Frontend, später über SPS,
- klare Visualisierung der Entscheidung,
- Diagnose- und Simulationsmöglichkeiten.

### 2.2 Nicht Bestandteil von Version 1

- vollständige 3D-Punktewolke,
- millimetergenaue Rekonstruktion jeder Rindenunregelmäßigkeit,
- Analyse eines konkreten Einschnittplans,
- Kenntnis von Brettbreiten, Brettstärken oder Produktlängen,
- Berechnung einer tatsächlichen Produktreserve,
- monetäre Ausbeuteoptimierung,
- Qualitätsklassen einzelner Bretter,
- Ast-, Riss- oder Fehlererkennung im Holz,
- dynamische Änderung der Zentrierachse.

### 2.3 Wichtige Begriffstrennung

Die Software kennt **keine tatsächliche Reserve zum Produkt**, weil sie den Einschnittplan nicht kennt.

Daher soll in UI, Logs, Schnittstellen und Code möglichst nicht der unqualifizierte Begriff `Reserve` für einen berechneten Millimeterwert verwendet werden.

Empfohlene Begriffe:

- `nutzbare Geometrie`,
- `verfügbarer symmetrischer Bereich`,
- `Geometriegewinn`,
- `Prioritätsrichtung`.

Die Prioritätsquelle darf fachlich weiterhin sagen, dass „Vorschnittreserve“ oder „Nachschnittreserve“ benötigt wird. Die Optimierungssoftware übersetzt dies nur in eine geometrische Prioritätsrichtung. In der Entwicklungs-/Inbetriebnahmephase wird diese Vorgabe manuell über das Frontend erzeugt; später kommt sie von der SPS.

---

## 3. Anlagenkontext

Ausgangslage der Linie:

1. Zentrierung
2. Spaner 1
3. Drehvorrichtung
4. Spaner 2
5. 3D-Messung
6. Fräser 1 und Fräser 2 für zwei Vorschnitte, beide höhenverfahrbar
7. Drehvorrichtung
8. Fräser 3 und Fräser 4, beide frei verfahrbar
9. Mehrblattkreissäge

Die hier spezifizierte Optimierung soll **vor dem relevanten Eindrehvorgang** eine robuste Drehentscheidung liefern. Die Zentrierstrategie bleibt in Version 1 unverändert.

Die spätere SPS kennt den gewählten Einschnitt bzw. die Produktionslogik und entscheidet deshalb, welche Achse größere geometrische Reserven benötigt. Bis diese Verbindung verfügbar ist, wird dieselbe Entscheidung im Frontend manuell vorgegeben. Die Kamera-/Optimierungssoftware muss den Einschnitt selbst zu keinem Zeitpunkt kennen.

---

## 4. Koordinatensystem

Das Maschinenkoordinatensystem ist projektweit verbindlich wie folgt definiert:

- `x`: Stamm-Längsachse / Förderrichtung
- `y`: Querachse, horizontal links/rechts
- `z`: Vertikalachse, unten/oben
- jede Stammscheibe liegt in einer `y-z`-Querschnittsebene
- Ursprung `(y=0, z=0)` jeder Scheibe: Schnittpunkt der späteren Zentrier-/Maschinenachse mit der jeweiligen `y-z`-Querschnittsebene

Wichtig: Alle Mittelpunkt-Offsets des Stammmodells müssen sich auf **dieselbe Referenzachse beziehen, die später bei der Zentrierung verwendet wird**.

### 4.1 Drehrichtung

Der Stamm dreht um seine Längsachse `x`. Eine positive Drehrichtung wird einmalig festgelegt, bevorzugt nach Rechtsschraubenregel um `+x`. Diese Definition muss in Kamera, Optimierer, Frontend, späterer SPS und Mechanik identisch sein.

Für die 2D-Rechnung innerhalb einer Scheibe wird der Querschnittsvektor in der Reihenfolge

```text
q = (y, z)^T
```

geführt. Damit gilt in der `y-z`-Ebene:

- horizontal/links-rechts: `u_H = (1, 0)^T` → `y`-Richtung,
- vertikal/oben-unten: `u_V = (0, 1)^T` → `z`-Richtung.

### 4.2 Relative statt absolute Optimierung

Für Version 1 wird bevorzugt mit einem relativen Winkel gearbeitet:

- aktuelle Lage = `Δθ = 0°`,
- Kandidat = relative Drehung `Δθ` um die `x`-Achse,
- Ergebnis = `Δθ_opt`.

Dadurch muss die UI keinen absoluten Stammwinkel erklären. Für die Mechanik kann intern weiterhin ein absoluter Drehwinkel geführt werden.

---

## 5. Scheibenmodell des Stammes

Der Stamm wird entlang `x` in diskrete Scheiben unterteilt.

Empfohlener Startwert:

- Scheibenabstand: `50 mm`,
- konfigurierbar, z. B. `25 ... 100 mm`.

Jede gültige Scheibe `i` wird beschrieben durch:

```text
StemSlice i
- x_i          : Position entlang des Stammes [mm]
- cy_i         : Mittelpunktversatz in y / horizontal [mm]
- cz_i         : Mittelpunktversatz in z / vertikal [mm]
- a_i          : große Ellipsen-Halbachse [mm]
- b_i          : kleine Ellipsen-Halbachse [mm]
- phi_i        : Orientierung der großen Ellipsenachse [rad oder °]
- quality_i    : Messqualitätswert 0..1
- valid_i      : gültig/ungültig
```

Konvention:

```text
a_i >= b_i > 0
```

### 5.1 Bedeutung der Parameter

`cy_i`, `cz_i` beschreiben die räumliche Krümmung des Stammes in der jeweiligen `y-z`-Scheibe relativ zur geraden Zentrier-/Maschinenachse entlang `x`.

`a_i`, `b_i`, `phi_i` beschreiben:

- lokalen Durchmesser,
- lokale Ovalität,
- Richtung der Ovalität.

Damit enthält das Modell genau die drei für Version 1 relevanten Geometrieinformationen:

1. Krümmung,
2. Ovalität,
3. Durchmesserverlauf.

### 5.2 Kein Anspruch auf echte Oberflächenrekonstruktion

Die Ellipse ist eine bewusste Näherung. Lokale Rindenstrukturen, kleine Beulen oder Einzüge werden in Version 1 nicht separat modelliert.

Das Modell soll stabil und reproduzierbar sein, nicht möglichst detailliert.

---

## 6. Gewinnung des Scheibenmodells aus der Kamera

Die genaue Kamerahardware ist nicht Bestandteil dieser Rechenspezifikation. Erwartet wird jedoch eine kalibrierte Bildauswertung, die aus mehreren Drehstellungen des Stammes geometrische Parameter erzeugen kann.

### 6.1 Benötigte Eingangsdaten aus dem bestehenden Krümmungsprojekt

Soweit vorhanden wiederverwenden:

- segmentierte Stammmaske bzw. Außenkontur,
- Stamm-Längsachse im Bild,
- sichtbare Mittellinie,
- aktuelle Drehstellung bzw. Drehwinkeländerung,
- Pixel-zu-mm-Kalibrierung,
- gültiger Messbereich.

### 6.2 Zusätzliche Auswertung

Für jede `x`-Position bzw. `y-z`-Scheibe und mehrere Drehstellungen werden bestimmt:

- projizierter Mittelpunkt,
- sichtbare Breite des Stammes,
- Qualitäts-/Konfidenzwert.

Aus dem Verlauf über den Drehwinkel werden geschätzt:

- räumlicher Mittelpunktversatz im Querschnitt `(cy, cz)`,
- große und kleine Halbachse `(a, b)`,
- Ellipsenorientierung `phi`.

### 6.3 Messprinzip

Die Stammrotation erzeugt mehrere Projektionen derselben Geometrie. Dadurch kann eine einzelne Kamera über die Zeit mehr Information liefern als ein einzelnes Standbild.

Version 1 benötigt ausdrücklich **kein perfektes tomographisches Modell**. Ziel ist ein stabiler Ellipsenfit je Scheibe.

### 6.4 Qualitätsregeln

Eine Scheibe wird nur verwendet, wenn mindestens folgende Bedingungen erfüllt sind:

- ausreichende Segmentierungsqualität,
- plausible Abmessungen,
- ausreichende Winkelabdeckung während der Messung,
- keine zu große Lücke in den Beobachtungen,
- Fit-Fehler unter einem konfigurierbaren Grenzwert.

Kurze Lücken dürfen interpoliert werden. Längere zusammenhängende ungültige Bereiche führen zu einem degradierten oder ungültigen Gesamtergebnis.

---

## 7. Vorverarbeitung des Scheibenmodells

Da Version 1 bewusst mit einem groben Modell arbeitet, muss die Berechnung robust gegen Messrauschen sein.

Empfohlene Verarbeitung:

1. Scheiben auf ein regelmäßiges `x`-Raster resamplen.
2. Ungültige Ausreißer verwerfen.
3. Kurze Lücken interpolieren.
4. `cy`, `cz`, `a`, `b` entlang `x` robust glätten.
5. `phi` als Winkelgröße unwrapen und anschließend glätten.
6. Geometrische Plausibilitätsprüfung durchführen.

Empfohlen für den Start:

- Medianfilter über 3 Scheiben oder vergleichbar robuster Filter,
- danach optional schwache Glättung.

Das Ziel ist **nicht**, echte lokale Engstellen wegzufiltern. Nur einzelne Messausreißer sollen nicht den gesamten Optimierungswinkel bestimmen.

---

## 8. Mathematisches Modell einer Scheibe

Eine Ellipse in der `y-z`-Ebene mit Mittelpunkt `c = (cy, cz)` und Halbachsen `a`, `b` wird durch

\[
(p-c)^T M (p-c) = 1
\]

beschrieben.

Dabei ist

\[
M = R(\varphi)
\begin{bmatrix}
1/a^2 & 0\\
0 & 1/b^2
\end{bmatrix}
R(\varphi)^T
\]

und

\[
R(\alpha)=
\begin{bmatrix}
\cos\alpha & -\sin\alpha\\
\sin\alpha & \cos\alpha
\end{bmatrix}.
\]

---

## 9. Virtuelle Drehung des Stammes

Für einen Kandidatenwinkel `Δθ` wird die gesamte Scheibengeometrie in der `y-z`-Ebene um die Maschinen-/Stammlängsachse `x` gedreht.

Für den Mittelpunkt:

\[
c' = R(\Delta\theta)c
\]

Für die Ellipsenorientierung:

\[
\varphi' = \varphi + \Delta\theta
\]

Die Halbachsen `a` und `b` bleiben unverändert.

Damit werden Krümmungsrichtung und Ovalitätsrichtung gemeinsam gedreht, genau wie beim realen Stamm.

---

## 10. Planunabhängige geometrische Kennzahl

### 10.1 Warum nicht einfach `Radius - Mittelpunktversatz`?

Eine einfache Näherung wie

```text
Halbdurchmesser - |Mittelpunktversatz|
```

ist nur für ideal kreisförmige oder speziell ausgerichtete Querschnitte korrekt genug. Bei einer gedrehten Ellipse kann auch ein Offset in der jeweils anderen Achse die verfügbare Geometrie beeinflussen.

Deshalb wird die nutzbare Geometrie je Scheibe geometrisch exakt aus der Ellipsengleichung bestimmt.

### 10.2 Definition: symmetrisch nutzbare Mittelsehne

Für die priorisierte Richtung wird eine Gerade durch die Maschinenachse gelegt.

Für **oben/unten**, also entlang der vertikalen `z`-Achse:

\[
u_V = (0,1)^T
\]

Für **links/rechts**, also entlang der horizontalen `y`-Achse:

\[
u_H = (1,0)^T
\]

Punkte auf dieser Geraden sind

\[
p(t)=t u.
\]

Einsetzen in die Ellipsengleichung ergibt:

\[
A t^2 + B t + C = 0
\]

mit

\[
A=u^T M u
\]

\[
B=-2u^T M c
\]

\[
C=c^T M c - 1.
\]

Die beiden reellen Lösungen `t1`, `t2` sind die Schnittpunkte der Ellipse mit der priorisierten Achse durch die Maschinenachse.

Nach Sortierung gilt idealerweise:

```text
t_neg <= 0 <= t_pos
```

Dann ist die symmetrisch nutzbare Halbausdehnung:

\[
r_{sym}=\min(t_{pos}, -t_{neg})
\]

und der gesamte symmetrisch nutzbare Bereich:

\[
G_i = 2r_{sym}.
\]

### 10.3 Interpretation

`G_i` ist **keine Produktreserve**.

`G_i` bedeutet:

> In dieser Scheibe steht um die Maschinen-/Zentrierachse in der gewählten Richtung ein symmetrischer, vollständig innerhalb des Ellipsenmodells liegender Bereich dieser Größe zur Verfügung.

### 10.4 Ungültige Fälle

Ist die Diskriminante negativ oder liegt die Maschinenachse außerhalb der Ellipse so, dass kein symmetrischer Bereich um `t=0` existiert, gilt die Scheibe für diese Bewertung als ungültig bzw. `G_i = 0`.

Dies ist zusätzlich als Datenqualitäts- bzw. Zentrierproblem zu markieren.

---

## 11. Kennzahl über die Stammlänge

Für jeden Kandidatenwinkel wird `G_i(Δθ)` für alle gültigen Scheiben berechnet.

Die planunabhängige Stammkennzahl ist die limitierende nutzbare Geometrie:

\[
G_{log}(\Delta\theta)=\min_i G_i(\Delta\theta)
\]

Nach der beschriebenen räumlichen Glättung ist das Minimum bewusst sinnvoll, weil ein einzelner tatsächlich enger Bereich für einen durchgehenden Schnitt limitierend sein kann.

### 11.1 Evaluationsbereich

Der verwendete Längsbereich ist konfigurierbar:

```text
x_eval_start
x_eval_end
```

Dadurch können konstruktiv irrelevante Randbereiche, unzuverlässige Stirnflächenmessungen oder definierte Endbesäumungen ausgeklammert werden, ohne einen Einschnittplan zu benötigen.

### 11.2 Optionaler robuster Modus

Für Inbetriebnahme und Diagnose kann zusätzlich ein robuster Kennwert, z. B. das 2-%- oder 5-%-Quantil, berechnet werden. Dieser darf aber nicht stillschweigend das Produktionskriterium ersetzen.

Empfehlung:

- Produktion: geglättetes Minimum,
- Diagnose: Minimum + Quantil gemeinsam loggen.

---

## 12. Prioritätsmodi

### 12.1 `VORSCHNITT_KRITISCH`

Annahme:

- der Vorschnitt benötigt die größere geometrische Sicherheit,
- relevante Richtung im Stamm-/Maschinenmodell: oben/unten.

Optimierungsgröße:

\[
G_V(\Delta\theta)=\min_i G_{V,i}(\Delta\theta)
\]

Gesucht wird:

\[
\Delta\theta_{opt}=\operatorname*{argmax}_{\Delta\theta} G_V(\Delta\theta).
\]

### 12.2 `NACHSCHNITT_KRITISCH`

Annahme:

- der Nachschnitt benötigt die größere geometrische Sicherheit,
- relevante Richtung: links/rechts.

Optimierungsgröße:

\[
G_H(\Delta\theta)=\min_i G_{H,i}(\Delta\theta)
\]

Gesucht wird:

\[
\Delta\theta_{opt}=\operatorname*{argmax}_{\Delta\theta} G_H(\Delta\theta).
\]

### 12.3 Kein Mischmodus in Version 1 erforderlich

Version 1 braucht keinen gewichteten Kompromiss aus Vorschnitt und Nachschnitt. Die aktive Prioritätsquelle bestimmt eindeutig die priorisierte Richtung.

Ein optionaler sekundärer Schutzmechanismus kann später ergänzt werden, ist aber nicht Teil des Kernalgorithmus.

### 12.4 Quelle des Prioritätsmodus

Der Optimierer erhält ausschließlich einen fachlichen `PriorityMode` und kennt die Herkunft dieses Wertes nicht. Die Herkunft wird außerhalb des Geometriekerns abstrahiert.

Für Entwicklung, Test und Inbetriebnahme gilt zunächst:

```text
priority_source = FRONTEND
```

Das Frontend muss den Modus manuell wählbar machen:

```text
VORSCHNITT_KRITISCH
NACHSCHNITT_KRITISCH
```

Optional kann `NONE` für einen deaktivierten/noch nicht gewählten Zustand verwendet werden.

Für den späteren Produktionsbetrieb wird die Quelle umgestellt auf:

```text
priority_source = PLC
```

Dann liefert die SPS denselben `PriorityMode`. Es darf dafür **keine Änderung am Optimierungsalgorithmus** erforderlich sein.

Die aktuell aktive Quelle soll in Diagnose und Logging mitgeführt werden, z. B.:

```text
priority_source = FRONTEND | PLC
```

Wenn `priority_source = PLC` aktiv ist, soll die manuelle Auswahl im Frontend entweder deaktiviert oder eindeutig als read-only angezeigt werden. Ein versehentliches Überschreiben einer SPS-Vorgabe durch die UI ist zu verhindern.

---

## 13. Winkelsuche

### 13.1 Symmetrie

Für die definierte geometrische Kennzahl sind Orientierungen im Abstand von `180°` äquivalent. Deshalb reicht grundsätzlich ein Suchbereich von `180°`.

### 13.2 Empfohlene Suchstrategie

Robust und einfach:

1. Grobsuche über `0 ... <180°` mit `1°` Schrittweite.
2. Bestes Grobergebnis bestimmen.
3. Lokale Feinsuche um das Grobmaximum, z. B. `±2°` mit `0,1°` oder `0,25°`.
4. Mechanisch günstigste äquivalente relative Drehung auswählen.

Bei begrenzter Mess- oder Aktuatorgenauigkeit kann bereits `0,5°` als finale Auflösung ausreichend sein.

### 13.3 Tie-Breaking

Wenn mehrere Winkel geometrisch praktisch gleichwertig sind, gilt folgende Reihenfolge:

1. maximaler Primärscore,
2. kleinster absoluter Drehweg,
3. stabilster Bereich/Plateau um den Winkel,
4. optional höhere Qualität der limitierenden Scheiben.

Ein Toleranzband verhindert unnötige Drehbewegungen bei numerisch nahezu identischen Ergebnissen.

Beispiel:

```text
score_tolerance_mm = 1.0
```

Liegt ein näherer Winkel innerhalb von 1 mm des besten Scores, wird der nähere Winkel bevorzugt.

---

## 14. Mindestnutzen und Entscheidung „nicht drehen“

Die aktuelle Lage `Δθ = 0°` ist immer ein gültiger Kandidat.

Der geometrische Gewinn lautet:

\[
\Delta G = G_{log}(\Delta\theta_{opt}) - G_{log}(0).
\]

Dieser Wert ist die zentrale numerische Ausgabe für die Visualisierung.

Wenn

\[
\Delta G < \Delta G_{min}
\]

soll keine Optimierungsdrehung angefordert werden.

Beispielkonfiguration:

```text
min_gain_mm = 3 ... 10 mm
```

Der genaue Wert wird während der Inbetriebnahme festgelegt.

Zusätzlich kann ein Mindestwinkel vorgesehen werden, um sehr kleine unnötige Stellbewegungen zu vermeiden:

```text
min_rotation_deg = 1 ... 3°
```

---

## 15. Ergebnisobjekt des Optimierers

Empfohlenes logisches Ergebnis:

```text
OptimizationResult
- valid                    : bool
- mode                     : VORSCHNITT_KRITISCH | NACHSCHNITT_KRITISCH
- delta_angle_deg          : empfohlene relative Drehung
- gain_mm                  : Delta G in der priorisierten Richtung
- should_rotate            : bool
- limiting_x_current_mm    : limitierende Scheibe in aktueller Lage
- limiting_x_optimal_mm    : limitierende Scheibe nach Optimierung
- score_current_mm         : interner Diagnosewert
- score_optimal_mm         : interner Diagnosewert
- model_quality            : Gesamtqualität 0..1
- reason_code              : OK | LOW_GAIN | INVALID_MODEL | ...
```

Wichtig für die Bedienoberfläche:

- `score_current_mm` und `score_optimal_mm` müssen nicht in der Hauptansicht angezeigt werden.
- Sichtbar sein sollen primär `delta_angle_deg` und `gain_mm`.

---

## 16. Visualisierung: Zielprinzip

Die Visualisierung muss innerhalb weniger Sekunden beantworten:

1. Wie sieht der Stamm geometrisch aus?
2. Welche Richtung ist von der aktiven Prioritätsquelle priorisiert?
3. Wo liegt der geometrisch limitierende Bereich?
4. Wie muss der Stamm gedreht werden?
5. Wie groß ist der geometrische Vorteil dieser Drehung?

Die Darstellung soll keine Kenntnis des mathematischen Verfahrens voraussetzen.

---

## 17. Hauptanzeige der HMI

### 17.1 Entscheidungskachel

Größtes und prominentestes Element:

```text
QUELLE: FRONTEND (TEST)
PRIORITÄT: VORSCHNITT / OBEN-UNTEN

DREHEN
+18°

GEOMETRIEGEWINN
+34 mm
```

oder bei zu kleinem Nutzen:

```text
PRIORITÄT: VORSCHNITT / OBEN-UNTEN

KEINE KORREKTURDREHUNG

GEOMETRIEGEWINN
< Mindestgrenze
```

### 17.2 Angezeigter Gewinn

Es wird **nur die Differenz zwischen aktueller und optimierter Lage in der priorisierten Richtung** prominent angezeigt:

```text
+34 mm Geometriegewinn oben/unten
```

oder

```text
+21 mm Geometriegewinn links/rechts
```

Nicht anzeigen als Hauptwert:

```text
aktuell 348 mm -> optimiert 382 mm
```

Diese absoluten Werte dürfen in einer Diagnoseansicht verfügbar sein, sind aber für die Bedienentscheidung nicht erforderlich.

### 17.3 Prioritätswahl für Entwicklung und Inbetriebnahme

Solange keine SPS-Verbindung vorhanden ist, enthält die Haupt-/Testansicht einen klaren Moduswahlschalter:

```text
PRIORITÄTSQUELLE: FRONTEND (TEST/INBETRIEBNAHME)

[ VORSCHNITT / OBEN-UNTEN ]   [ NACHSCHNITT / LINKS-RECHTS ]
```

Anforderungen:

- genau ein Modus ist aktiv,
- der aktive Modus ist sofort sichtbar,
- eine Modusänderung stößt eine Neuberechnung mit demselben vorhandenen Stammmodell an, sofern das Modell noch gültig ist,
- die Umschaltung darf keine neue Kameramessung erzwingen, wenn nur die Priorität geändert wird,
- die Quelle `FRONTEND` muss klar erkennbar sein, damit eine manuelle Testvorgabe nicht mit einer späteren SPS-Vorgabe verwechselt wird,
- nach SPS-Integration bleibt dieselbe UI-Komponente nutzbar, ist bei `priority_source = PLC` jedoch read-only/deaktiviert und zeigt den von der SPS gelieferten Modus an.

Der Prioritätswahlschalter ist **keine Einschnittauswahl**. Er setzt lediglich die geometrische Prioritätsrichtung.

---

## 18. Stamm-Längsansicht

Eine vereinfachte Längsansicht zeigt:

- Stammkontur bzw. abstrahierten Durchmesserverlauf,
- projizierte Mittellinie/Krümmung,
- `x`-Position,
- limitierende Scheibe aktuell,
- limitierende Scheibe nach Optimierung,
- optional lokalen Geometriegewinn je Scheibe.

Beispielkonzept:

```text
Zopf                                                   Wurzel
|---------------------------------------------------------|
       ____
______/    \______________________/\______________________
                     ^
                     |
              limitierender Bereich
```

Die Darstellung ist schematisch. Sie soll die Stammform verständlich machen, nicht eine fotorealistische 3D-Oberfläche vortäuschen.

---

## 19. Differenzdarstellung über die Stammlänge

Für jede Scheibe kann zusätzlich berechnet werden:

\[
\Delta G_i = G_i(\Delta\theta_{opt}) - G_i(0).
\]

Diese Kurve bzw. dieses Band zeigt, **wo** die gewählte Drehung in der priorisierten Richtung Geometrie gewinnt oder verliert.

Empfohlene Darstellung:

- horizontale `x`-Achse,
- Null-Linie,
- positiver Bereich = Geometriegewinn,
- negativer Bereich = lokale Verringerung,
- limitierende Stelle markieren.

Wichtig: Auch wenn einzelne Scheiben lokal etwas verlieren, kann der globale Minimum-Score steigen. Deshalb muss die UI klar zwischen lokalem `ΔG_i` und dem globalen Hauptwert `ΔG` unterscheiden.

---

## 20. Kritischer Querschnitt

Der wichtigste Querschnitt soll als einfache Ellipsengrafik gezeigt werden.

Empfehlung:

- Maschinenachse als festes Kreuz im Ursprung,
- Ellipse der aktuellen Lage als dünne/gestrichelte Kontur,
- Ellipse der optimierten Lage als kräftige Kontur,
- priorisierte Achse deutlich markieren,
- symmetrische Mittelsehne visualisieren,
- Drehpfeil anzeigen,
- keine erfundenen Produktkonturen einzeichnen.

Damit ist direkt sichtbar:

- Krümmungsversatz,
- Ovalitätsrichtung,
- Wirkung der Rotation,
- welche Richtung geometrisch verbessert wird.

### 20.1 Wechselnde limitierende Scheibe

Die limitierende Scheibe kann vor und nach der Optimierung an unterschiedlichen `x`-Positionen liegen.

Die UI muss deshalb entweder:

- beide Positionen kennzeichnen oder
- einen Umschalter `Aktuell / Optimiert` für die kritische Scheibe anbieten.

In der Hauptansicht genügt die prominentere der beiden Darstellungen; die zweite kann in Diagnoseinformationen stehen.

---

## 21. Empfohlenes Bildschirm-Layout

```text
+------------------------------------------------------------------+
| PRIORITÄT: VORSCHNITT / OBEN-UNTEN                               |
|                                                                  |
|        DREHEN +18°                 GEOMETRIEGEWINN +34 mm         |
+--------------------------------+---------------------------------+
|                                |                                 |
| LÄNGSANSICHT STAMM             | KRITISCHER QUERSCHNITT          |
|                                |                                 |
| - Kontur                       | - aktuelle Ellipse gestrichelt  |
| - Mittellinie                  | - optimale Ellipse kräftig      |
| - limitierende Position        | - Maschinenachse                |
|                                | - Prioritätsachse               |
+--------------------------------+---------------------------------+
| DIFFERENZ NUTZBARE GEOMETRIE ÜBER x                              |
|  +mm   ________       ____________                               |
|   0 --/--------\-----/------------\----------------------------  |
|  -mm                                                            |
+------------------------------------------------------------------+
| Modellqualität | Status | Diagnose/Details                       |
+------------------------------------------------------------------+
```

---

## 22. Farben und Semantik

Farben dürfen unterstützend wirken, aber nie die einzige Information tragen.

Empfehlung:

- optimierte Zielorientierung: positiv/grün,
- aktuelle Lage: neutral/grau,
- Prioritätsachse: deutlich hervorgehoben,
- Warn-/Qualitätsproblem: gelb/orange,
- ungültige Messung: rot.

Jede Farbcodierung muss zusätzlich durch Text, Symbol oder Linienstil unterscheidbar sein.

---

## 23. Prioritätsquelle und spätere SPS-Schnittstelle

Die Fachlogik soll von Anfang an über eine kleine Abstraktion für die Prioritätsquelle angebunden werden. Dadurch kann die Software ohne SPS vollständig entwickelt, getestet und in Betrieb genommen werden.

### 23.1 Aktuelle Entwicklungs-/Inbetriebnahmephase: Frontend

Bis eine SPS-Verbindung verfügbar ist, gilt standardmäßig:

```text
priority_source = FRONTEND
```

Das Frontend setzt den fachlichen Wert:

```text
priority_mode : ENUM
  0 = NONE
  1 = VORSCHNITT_KRITISCH
  2 = NACHSCHNITT_KRITISCH
```

Eine Änderung des Modus soll den Optimierer mit dem zuletzt gültigen `StemSliceModel` erneut ausführen können. Dadurch kann während Entwicklung und Inbetriebnahme direkt verglichen werden, wie derselbe Stamm bei vertikaler bzw. horizontaler Priorität eingedreht würde.

Für Logs und Replay müssen mindestens gespeichert werden:

```text
priority_source
priority_mode
```

### 23.2 Abstrakte fachliche Eingangsschnittstelle

Unabhängig von Frontend oder späterer SPS benötigt die Runtime fachlich nur:

```text
optimization_enable        : BOOL
priority_source            : FRONTEND | PLC
priority_mode              : NONE | VORSCHNITT_KRITISCH | NACHSCHNITT_KRITISCH
stem_id                    : optional ID
measurement_start          : BOOL / Event
measurement_end            : BOOL / Event
```

Mechanische Grenzen können zunächst aus der lokalen Konfiguration kommen und später optional von der SPS überschrieben werden.

### 23.3 Spätere Eingänge von der SPS

Wenn die SPS angebunden wird, übernimmt sie die Quelle des `priority_mode`. Die konkrete Feldbus-/Protokolltechnik wird an die bestehende Anlage angepasst. Optional können dann zusätzlich geliefert werden:

```text
max_rotation_positive_deg
max_rotation_negative_deg
rotation_direction_limit
recipe_id
```

`recipe_id` darf protokolliert werden, ohne dass die Optimierungssoftware den Einschnittplan interpretieren muss.

### 23.4 Spätere Ausgänge an die SPS

```text
result_valid               : BOOL
should_rotate              : BOOL
rotation_delta_deg         : REAL
gain_mm                    : REAL
model_quality              : REAL
result_ready               : BOOL
reason_code                : ENUM
```

Optional für Diagnose:

```text
limiting_x_current_mm
limiting_x_optimal_mm
score_current_mm
score_optimal_mm
```

Die Frontend-Testvorgabe und die spätere SPS-Vorgabe müssen denselben Kernpfad verwenden:

```text
Frontend manual mode ─┐
                     ├─> PriorityMode -> RotationOptimizer
SPS mode (später) ───┘
```

---

## 24. Zustandsmodell

Empfohlene Zustände:

```text
IDLE
  -> MEASURING
  -> MODEL_BUILDING
  -> OPTIMIZING
  -> RESULT_READY
  -> APPLIED / DONE
```

Fehlerzustände:

```text
INVALID_MEASUREMENT
INSUFFICIENT_ROTATION_COVERAGE
MODEL_INVALID
PRIORITY_MODE_INVALID
ROTATION_CONSTRAINT_CONFLICT
```

Der Optimierer selbst soll als deterministische, zustandslose Kernfunktion implementiert werden. Zustandslogik und Kommunikation liegen außerhalb des mathematischen Kernmoduls.

---

## 25. Qualitätsbewertung des Gesamtmodells

Ein `model_quality` von `0..1` soll mindestens berücksichtigen:

- Anteil gültiger Scheiben,
- Winkelabdeckung der Kameramessung,
- Ellipsenfit-Fehler,
- Stabilität des Drehwinkel-Trackings,
- Größe interpolierter Lücken,
- Kalibrierstatus.

Der exakte Score kann später empirisch kalibriert werden.

Wichtig ist zunächst eine klare Schwellwertlogik:

```text
quality >= Q_OK       -> Optimierung freigeben
Q_WARN <= quality     -> Ergebnis mit Warnstatus
quality < Q_MIN       -> kein Drehkommando
```

---

## 26. Sicherheits- und Plausibilitätsregeln

Mindestens folgende Prüfungen sind vorzusehen:

- `a >= b > 0`,
- Durchmesser im plausiblen Anlagenbereich,
- Mittelpunktversatz im plausiblen Bereich,
- keine unrealistischen Sprünge zwischen Nachbarscheiben,
- ausreichende Anzahl gültiger Scheiben,
- Kandidatenwinkel innerhalb mechanischer Grenzen,
- `gain_mm >= 0` bis auf numerische Toleranz, weil aktuelle Lage Teil des Suchraums ist,
- bei zu geringer Qualität kein Stellbefehl.

---

## 27. Konfigurationsparameter für Version 1

Empfohlene zentrale Konfiguration:

```text
priority_source = FRONTEND | PLC   # aktueller Default: FRONTEND
slice_spacing_mm
x_eval_start_mm
x_eval_end_mm
smoothing_window_slices
coarse_angle_step_deg
fine_angle_step_deg
fine_search_half_width_deg
score_tolerance_mm
min_gain_mm
min_rotation_deg
min_valid_slice_ratio
min_model_quality
max_interpolation_gap_mm
rotation_positive_limit_deg
rotation_negative_limit_deg
```

Alle Parameter sollen mit Einheit dokumentiert und in Logs/Snapshots mitgespeichert werden.

---

## 28. Pseudocode des Kernalgorithmus

```text
function optimize(stem_model, priority_mode, constraints, config):
    model = preprocess(stem_model, config)

    if not model.is_valid:
        return invalid_result(...)

    axis = vertical if priority_mode == VORSCHNITT_KRITISCH else horizontal

    score_current = score(model, delta_angle=0, axis=axis)

    candidates = build_candidate_angles(constraints, config)

    best = None

    for delta in candidates:
        s = score(model, delta, axis)
        best = choose_better(best, delta, s, score_tolerance=config.score_tolerance_mm)

    best = refine_locally(best, model, axis, constraints, config)

    gain = best.score - score_current

    if gain < config.min_gain_mm or abs(best.delta) < config.min_rotation_deg:
        return result(
            should_rotate=false,
            delta_angle_deg=0,
            gain_mm=max(0, gain),
            ...
        )

    return result(
        should_rotate=true,
        delta_angle_deg=best.delta,
        gain_mm=gain,
        ...
    )
```

### 28.1 Score-Funktion

```text
function score(model, delta_angle, axis):
    min_span = +infinity
    limiting_slice = null

    for slice in model.valid_slices:
        rotated = rotate_slice(slice, delta_angle)
        span = symmetric_center_chord(rotated, axis)

        if span < min_span:
            min_span = span
            limiting_slice = slice.x

    return Score(min_span, limiting_slice)
```

---

## 29. Teststrategie

### 29.1 Unit-Tests Geometrie

Pflichtfälle:

1. Kreis, perfekt zentriert → alle Winkel gleich.
2. Kreis, seitlich versetzt → erwartete Verbesserung bei geeigneter Drehung.
3. Ellipse, zentriert → große Achse wird bei vertikaler Priorität vertikal ausgerichtet.
4. Ellipse, zentriert → große Achse wird bei horizontaler Priorität horizontal ausgerichtet.
5. Ellipse + Mittelpunktversatz → korrekter Kompromiss zwischen Krümmung und Ovalität.
6. 180°-Symmetrie des Scores.
7. Maschinenachse außerhalb Ellipse → ungültiger/0-Score.
8. Numerische Stabilität nahe Tangentialfall.

### 29.2 Unit-Tests Optimierer

- aktuelle Lage immer im Suchraum,
- `gain_mm` nicht negativ außerhalb numerischer Toleranz,
- Tie-Breaker bevorzugt kleineren Drehweg,
- Mindestgewinn verhindert unnötige Drehung,
- mechanische Winkelgrenzen werden eingehalten,
- identische Eingaben liefern identische Ergebnisse.

### 29.3 Modelltests

Synthetische Stämme erzeugen:

- gerade und rund,
- stark oval,
- stark gekrümmt,
- Krümmungs- und Ovalitätsmaximum gleichgerichtet,
- Krümmungs- und Ovalitätsoptimum gegeneinander verdreht,
- wechselnde Krümmung über `x`,
- wechselnde Ovalität über `x`,
- lokale Engstelle,
- verrauschte Messwerte,
- Datenlücken.

### 29.4 UI-Tests

Prüfen:

- bei `priority_source = FRONTEND` ist der Modus zwischen `VORSCHNITT_KRITISCH` und `NACHSCHNITT_KRITISCH` manuell umschaltbar,
- der aktive `priority_source` ist sichtbar und eindeutig,
- ein Moduswechsel kann denselben gültigen Stamm ohne neue Messung neu optimieren,
- `VORSCHNITT_KRITISCH` zeigt nur oben/unten bzw. `z` als Priorität,
- `NACHSCHNITT_KRITISCH` zeigt nur links/rechts bzw. `y` als Priorität,
- Hauptwert ist `gain_mm`, nicht ein fiktiver Reservewert,
- Drehrichtung und Vorzeichen eindeutig,
- `NO ROTATION` bei zu kleinem Gewinn,
- ungültiges Modell erzeugt keinen Drehbefehl,
- limitierende Position stimmt mit Optimierergebnis überein.

---

## 30. Abnahmekriterien für Version 1

Version 1 gilt fachlich als umgesetzt, wenn:

1. ein Scheibenmodell aus synthetischen oder real aufgenommenen Stammdaten eingelesen werden kann,
2. jede Scheibe mindestens `x`, `cy`, `cz`, `a`, `b`, `phi` enthält,
3. die exakte Mittelsehnen-Geometrie für beide Achsen berechnet wird,
4. der Optimierer abhängig vom aktiven `priority_mode` den besten Drehwinkel bestimmt, unabhängig davon, ob dieser aktuell aus dem Frontend oder später aus der SPS kommt,
5. der aktuelle Winkel Bestandteil des Suchraums ist,
6. `gain_mm` als Differenz zwischen aktueller und optimierter Geometrie berechnet wird,
7. bei zu kleinem Gewinn keine unnötige Drehung ausgegeben wird,
8. die HMI Drehung + Geometriegewinn sofort erkennbar darstellt,
9. ein Stamm-Längsbild und ein kritischer Querschnitt verfügbar sind,
10. die Software keinerlei Einschnittplan oder Produktdimension benötigt,
11. Ergebnisse reproduzierbar geloggt und offline nachgerechnet werden können.

---

## 31. Diagnose- und Logging-Anforderungen

Pro Stamm soll ein reproduzierbarer Datensatz gespeichert werden können:

```text
stem_id
utc/local timestamp
priority_source
priority_mode
raw/current rotation reference
slice_model before preprocessing
slice_model after preprocessing
configuration snapshot
candidate score curve over angle
selected delta_angle_deg
gain_mm
limiting_x_current_mm
limiting_x_optimal_mm
model_quality
reason_code
optional camera frame references
```

Besonders wertvoll ist die gespeicherte Score-Kurve:

```text
angle_deg -> G_log(angle)
```

Damit kann später nachvollzogen werden, ob ein gewählter Winkel ein scharfes Maximum oder ein breites, unkritisches Plateau war.

---

## 32. Simulationsmodus

Vor der SPS-Anbindung soll ein Offline-Simulator existieren. Zusätzlich muss die reale Frontend-Anwendung bereits als Prioritätsquelle verwendet werden können.

Er muss:

- künstliche Scheibenstämme erzeugen können,
- reale gespeicherte Scheibenmodelle laden können,
- Prioritätsmodus im Frontend/Simulator umschalten können,
- Winkel-Score-Kurve zeigen,
- Ergebniswinkel und `gain_mm` ausgeben,
- HMI-Visualisierung mit denselben ViewModels wie die Produktion speisen.

Der Simulator ist ein zentraler Bestandteil der Inbetriebnahme und nicht nur ein Entwicklerwerkzeug.

---

## 33. Empfohlene Softwarearchitektur

Logische Trennung:

```text
Camera / Measurement
        |
        v
Slice Model Builder
        |
        v
Model Preprocessing
        |
        v
Pure Geometry Core
        |
        v
Rotation Optimizer
        |
        +------> Diagnostics / Logger
        |
        v
Runtime / Priority Source Adapter
        |
        +---- Frontend manual (Entwicklung/Inbetriebnahme)
        |
        +---- PLC (später)
        |
        v
HMI ViewModel / Visualization
```

### 33.1 Pure Geometry Core

Keine Kamera-, SPS- oder UI-Abhängigkeiten.

Verantwortlich für:

- Ellipsenmathematik,
- virtuelle Rotation,
- Achsenschnitt,
- symmetrische Mittelsehne,
- numerische Robustheit.

### 33.2 Rotation Optimizer

Keine UI-Abhängigkeiten.

Verantwortlich für:

- Kandidatensuche,
- Scorebildung über `x`,
- Tie-Breaking,
- Mindestgewinn,
- mechanische Winkelgrenzen,
- Ergebnisobjekt.

### 33.3 Measurement Adapter

Verantwortlich für die Umwandlung bestehender Kameraresultate in das normierte `StemSliceModel`.

### 33.4 HMI

Arbeitet ausschließlich mit einem ViewModel bzw. Ergebnisobjekt. Die mathematische Berechnung darf nicht in UI-Code dupliziert werden.

Während `priority_source = FRONTEND` stellt die HMI zusätzlich die manuelle Auswahl von `VORSCHNITT_KRITISCH` bzw. `NACHSCHNITT_KRITISCH` bereit und übergibt lediglich den gewählten Enum-Wert an die Runtime. Bei `priority_source = PLC` ist diese Auswahl read-only/deaktiviert.

---

## 34. Numerische und technische Hinweise

- intern Winkel bevorzugt in Radiant rechnen,
- API/SPS/UI darf Grad verwenden,
- `double`/64-Bit-Fließkomma im Geometriekern bevorzugen,
- Diskriminante der quadratischen Gleichung mit numerischer Toleranz behandeln,
- Winkel normalisieren,
- periodische Winkeldaten beim Glätten unwrapen,
- keine Rundung auf ganze Millimeter innerhalb des Kerns,
- Rundung nur an der UI-/SPS-Grenze gemäß benötigter Auflösung.

---

## 35. Fachliches Beispiel

Angenommen, während Entwicklung/Inbetriebnahme wird im Frontend gewählt (später kann exakt derselbe Wert von der SPS kommen):

```text
priority_mode = VORSCHNITT_KRITISCH
```

Der aktuelle Stammzustand wird intern als `Δθ = 0°` geführt.

Der Optimierer berechnet den vertikalen Stamm-Score über alle Scheiben für die Kandidatenwinkel. Das Maximum liegt bei:

```text
Δθ_opt = +18°
```

Der intern berechnete Score verbessert sich beispielsweise um:

```text
ΔG = +34 mm
```

Die Hauptanzeige zeigt ausschließlich:

```text
PRIORITÄT: VORSCHNITT / OBEN-UNTEN
DREHEN: +18°
GEOMETRIEGEWINN: +34 mm
```

Die absoluten aktuellen und optimierten Scorewerte bleiben Diagnoseinformationen.

Es wird **nicht** behauptet, dass 34 mm Produktreserve entstanden sind. Die korrekte Aussage ist:

> Durch die vorgeschlagene Stammrotation verbessert sich die limitierende symmetrisch nutzbare Stammgeometrie in der von der aktiven Prioritätsquelle priorisierten Richtung um 34 mm.

---

## 36. Warum diese Vereinfachung fachlich sinnvoll ist

Die Architektur trennt zwei Wissensbereiche sauber:

### Frontend-Testvorgabe bzw. später SPS / Produktionssteuerung bestimmt:

- welcher Einschnitt gefahren wird,
- ob Vorschnitt oder Nachschnitt toleranter ist,
- wo mehr Waldkante bzw. Rückreihung zulässig ist,
- welche Richtung deshalb mehr geometrische Sicherheit benötigt.

### Kamera-/Optimierungssystem weiß:

- wie der konkrete Stamm gekrümmt ist,
- wie oval er ist,
- wie sich der Durchmesser über die Länge verändert,
- wie diese Geometrie aktuell orientiert ist,
- welche Rotation die gewünschte Achse geometrisch verbessert.

Dadurch bleibt der Optimierer klein, nachvollziehbar und unabhängig von Produktdaten.

---

## 37. Mögliche spätere Erweiterungen

Nicht für Version 1 erforderlich, aber architektonisch offenhalten:

- gewichtete Mischpriorität, z. B. 70 % Vorschnitt / 30 % Nachschnitt,
- sekundäre Verlustbegrenzung der nicht priorisierten Achse,
- alternative Querschnittsmodelle neben Ellipsen,
- mehrere Konturstützpunkte pro Scheibe,
- echte 3D-Messdaten als besserer `StemSliceModel`-Lieferant,
- dynamische Zentrieroptimierung zusätzlich zur Drehoptimierung,
- Einschnittplan-basierte Simulation als separate, optionale höhere Optimierungsebene,
- lernende Parametrierung der Mindestgewinne anhand realer Produktionsdaten.

Diese Erweiterungen dürfen die einfache Kern-API `StemSliceModel + priority_mode -> OptimizationResult` nicht unnötig verkomplizieren.

---

## 38. Kurzfassung der Kernlogik

```text
1. Kamera beobachtet den Stamm während der Drehung.
2. Stamm wird als Stapel grober Ellipsenscheiben modelliert.
3. Jede Scheibe enthält Mittelpunkt, Durchmesser/Ovalität und Ovalitätswinkel.
4. Prioritätsquelle sagt nur: oben/unten oder links/rechts priorisieren; zunächst Frontend, später SPS.
5. Software dreht das Scheibenmodell virtuell durch mögliche Winkel.
6. Pro Winkel wird die symmetrisch nutzbare Mittelsehne jeder Scheibe berechnet.
7. Die schlechteste Scheibe bestimmt den Stamm-Score.
8. Gewählt wird der Winkel mit dem größten Score in der priorisierten Richtung.
9. Angezeigt werden relative Drehung und Geometriegewinn gegenüber der aktuellen Lage.
10. Kein Einschnittplan und keine Produktdimension sind dafür erforderlich.
```
