# Umsetzungsplan und Codex-Prompts: Stamm-Eindrehoptimierung

**Zweck dieses Dokuments:** Praktischer Implementierungsfahrplan mit größeren, direkt verwendbaren Codex-Prompts für ein bestehendes Krümmungs-/Kameraprojekt.  
**Voraussetzung:** Die fachliche Referenz ist `stammoptimierung_spezifikation.md`.  
**Arbeitsweise:** Jeder Prompt ist als größeres Arbeitspaket gedacht. GPT-5.6 Sol/Codex soll das vorhandene Repository zuerst verstehen, bestehende Komponenten wiederverwenden und Änderungen mit Tests absichern.

---

# 1. Empfohlene Umsetzungsreihenfolge

## Phase 0 – Repository verstehen und Integrationspunkte festlegen

Ziel:

- bestehende Architektur des Krümmungsprojekts erfassen,
- Kamera-, Segmentierungs-, Kalibrier- und Drehwinkelkomponenten finden,
- vorhandene UI-, Logging-, Test- und SPS-Strukturen identifizieren,
- Technologie-Stack nicht erraten, sondern aus dem Repository ableiten,
- Integrationsplan erstellen.

Ergebnis:

- kurze technische Bestandsaufnahme,
- Liste wiederverwendbarer Komponenten,
- konkrete Datei-/Modulplanung,
- keine unnötige Neuentwicklung.

---

## Phase 1 – Reiner Geometriekern und Optimierer

Ziel:

- `StemSliceModel` definieren,
- Ellipsenmathematik implementieren,
- virtuelle Rotation implementieren,
- symmetrische Mittelsehnenberechnung implementieren,
- Stamm-Score berechnen,
- Winkelsuche implementieren,
- Tie-Breaking und Mindestgewinn umsetzen,
- umfassende synthetische Tests erstellen.

Wichtig:

Diese Phase muss **ohne Kamera, SPS und UI** testbar sein.

---

## Phase 2 – Kamera-/Messdaten in Scheibenmodell überführen

Ziel:

- bestehende Stammsegmentierung wiederverwenden,
- Beobachtungen über `x` und Drehwinkel sammeln,
- aus projizierter Mitte und sichtbarer Breite je Scheibe ein Ellipsenmodell fitten,
- Modellqualität bestimmen,
- Glättung/Interpolation/Plausibilitätschecks implementieren,
- gespeicherte Messsequenzen offline reproduzierbar auswerten.

---

## Phase 3 – Runtime, Frontend-Prioritätsvorgabe und Zustandsmaschine

Ziel:

- Prioritätsmodus zunächst manuell aus dem Frontend übernehmen,
- die Prioritätsquelle so abstrahieren, dass später die SPS denselben `PriorityMode` liefern kann,
- Messzyklus steuern,
- Modellbau und Optimierung auslösen,
- `rotation_delta_deg`, `gain_mm`, Qualität und Status bereitstellen,
- bei ungültigem Modell oder zu kleinem Gewinn sicher keine Drehung anfordern,
- Datensatz pro Stamm loggen.

---

## Phase 4 – Visualisierung / HMI

Ziel:

- Prioritätsumschalter im Frontend für Entwicklung/Inbetriebnahme sowie Anzeige der aktiven Quelle,
- klare Entscheidungskachel,
- Stamm-Längsansicht,
- kritischer Querschnitt als Current-vs-Optimized-Overlay,
- Differenz `ΔG_i` über Stammlänge,
- Hauptanzeige nur mit Drehwinkel und Geometriegewinn in priorisierter Richtung,
- Diagnoseansicht für absolute interne Werte.

---

## Phase 5 – Simulator, Replay und Inbetriebnahme

Ziel:

- synthetische Stammgeneratoren,
- Replay realer Messungen,
- Scorekurve über Winkel,
- Vergleich Messung/Modell/Entscheidung,
- Export von Diagnosefällen,
- automatisierte Regressionstests mit gespeicherten Referenzstämmen.

---

# 2. Arbeitsregeln für Codex

Diese Regeln gelten für alle nachfolgenden Prompts:

1. **Bestehenden Stack respektieren.** Keine neue UI-, Test-, Logging- oder Konfigurationsbibliothek einführen, wenn das Repository bereits eine passende Lösung hat.
2. **Keine Einschnittlogik implementieren.** Keine Bretter, Produkte, Schnittbilder oder Produktreserven modellieren.
3. **Die aktive Prioritätsquelle liefert nur die Prioritätsrichtung.** Für Entwicklung/Test/Inbetriebnahme ist das Frontend die Quelle; später die SPS. Kernmodi: `VORSCHNITT_KRITISCH` und `NACHSCHNITT_KRITISCH`.
4. **Geometrischer Kern bleibt rein.** Keine Kamera-, Netzwerk-, SPS- oder UI-Abhängigkeiten im Kern.
5. **Testbarkeit vor Performance-Mikrooptimierung.** Erst korrekte Referenzimplementierung und Tests, danach optimieren.
6. **Keine stillen Annahmen.** Einheiten, Winkelkonventionen und Koordinaten explizit in Code und Tests dokumentieren. Verbindlich: `x` längs, `y` horizontal/quer, `z` vertikal, Scheiben in `y-z`, Rotation um `x`.
7. **Keine Duplikation der Mathematik in der UI.** UI erhält fertige ViewModels/Resultate.
8. **Reproduzierbarkeit.** Jeder Stamm soll offline aus gespeicherten Eingangsdaten erneut berechnet werden können.
9. **Rückwärtskompatibilität.** Bestehende Krümmungsfunktionen nicht unnötig verändern oder entfernen.
10. **Kleine, klar begründete Abweichungen von der Spezifikation sind erlaubt**, wenn der vorhandene Stack es erfordert; sie müssen im Abschlussbericht genannt werden.

---

# 3. Codex Prompt 0 – Repository-Audit und Integrationsplan

```text
Du arbeitest in einem bestehenden Projekt zur kamerabasierten Krümmungserkennung von Rundholzstämmen. Wir erweitern dieses Projekt um eine einfache geometrische Eindrehoptimierung.

Lies zuerst die beigefügte/ins Repository gelegte Fachspezifikation `docs/Stammoptimierung und 3D Messung/stammoptimierung_spezifikation.md` vollständig. Nimm noch keine größeren Architekturänderungen vor, bevor du den bestehenden Code verstanden hast.

Ziel dieses Arbeitspakets:
1. Repository-Struktur verstehen.
2. Bestehende Komponenten für Kameraaufnahme, Stammsegmentierung, Krümmungsberechnung, Kalibrierung, Drehwinkeltracking, UI, Konfiguration, Logging, Tests und gegebenenfalls SPS-Kommunikation identifizieren.
3. Festlegen, wie die neue Funktion mit möglichst wenig Eingriff integriert werden kann.
4. Einen konkreten Implementierungsplan im Repository dokumentieren.

Fachliche Randbedingungen:
- Kein Einschnittplan wird in dieser Software analysiert.
- Keine Produktbreiten, Produktstärken oder Produktlängen werden benötigt.
- Für Entwicklung/Test/Inbetriebnahme wird im Frontend vorgegeben, ob die Geometrie oben/unten (`VORSCHNITT_KRITISCH`) oder links/rechts (`NACHSCHNITT_KRITISCH`) priorisiert werden soll. Später liefert die SPS denselben fachlichen Wert.
- Der Stamm wird als Folge von elliptischen Scheiben modelliert.
- Verbindliches Koordinatensystem: `x` = Stamm-Längsachse/Förderrichtung, `y` = horizontal/quer, `z` = vertikal. Jede Scheibe liegt in der `y-z`-Ebene und der Stamm dreht um `x`.
- Kernparameter je Scheibe: x, cy, cz, a, b, phi, quality, valid.
- Die spätere Ausgabe ist eine relative Drehung `delta_angle_deg` und ein `gain_mm` in der priorisierten Richtung.

Vorgehen:
A. Untersuche Buildsystem, Hauptanwendung, Module/Packages, Testframework und vorhandene Konventionen.
B. Suche gezielt nach:
   - Segmentierungs-/Maskenlogik,
   - Konturextraktion,
   - Pixel-mm-Kalibrierung,
   - Stamm-Mittellinie/Krümmung,
   - Rotationswinkel/Drehverfolgung,
   - Frame-/Messzyklusverwaltung,
   - Konfigurationssystem,
   - Logging/Datenspeicherung,
   - HMI/GUI/Web-Frontend,
   - SPS/OPC-UA/ADS/Modbus/sonstiger Steuerungsschnittstelle (nur zur späteren Integration).
C. Dokumentiere, welche Teile direkt wiederverwendet werden können und wo Adapter sinnvoll sind.
D. Schlage eine Modulstruktur vor, die mindestens logisch trennt:
   - measurement / slice model builder,
   - preprocessing,
   - geometry core,
   - rotation optimizer,
   - runtime / priority source adapter (Frontend jetzt, PLC später),
   - visualization/view model,
   - diagnostics/replay.
E. Definiere für jede geplante neue Datei/Komponente kurz Aufgabe, Inputs, Outputs und Abhängigkeiten.
F. Identifiziere Risiken oder offene technische Punkte, die aus dem Repository tatsächlich hervorgehen. Keine spekulativen Blocker erfinden.
G. Prüfe, wie Winkel und Koordinaten aktuell definiert sind. Die verbindliche Fachkonvention ist `x` längs, `y` horizontal/quer, `z` vertikal und Drehung um `x`. Falls Altcode andere Namen verwendet, über Adapter sauber abbilden statt die Fachkonvention zu verwässern.
H. Prüfe, ob reale Messaufzeichnungen/Testbilder vorhanden sind, die später für Replay/Regression genutzt werden können.

Lieferumfang:
- Erstelle oder aktualisiere eine Datei `docs/stem_rotation_optimization_implementation_plan.md` (Pfad an bestehende Doku-Struktur anpassen, falls nötig).
- Darin: Ist-Architektur, Integrationspunkte, geplante Module, Datenfluss, Teststrategie, Risiken und konkrete Reihenfolge.
- Führe in diesem Prompt noch keine große Kernimplementierung durch, außer kleine notwendige Hilfen für die Analyse.
- Am Ende im Chat/Abschlussbericht: wichtigste gefundene Dateien, empfohlene Architektur und eventuelle Abweichungen von der Spezifikation.

Qualitätsanspruch:
Der Plan muss so konkret sein, dass der nächste Codex-Lauf direkt mit der Geometrieimplementierung beginnen kann, ohne erneut das gesamte Repository analysieren zu müssen.
```

---

# 4. Codex Prompt 1 – Geometriekern, Score und Drehoptimierer komplett implementieren

```text
Implementiere jetzt den mathematischen Kern der Stamm-Eindrehoptimierung entsprechend `stammoptimierung_spezifikation.md` und dem im vorherigen Schritt erstellten Integrationsplan.

Wichtig: Dieses Arbeitspaket soll den kompletten reinen Geometriekern und Optimierer inklusive Tests liefern. Kamera, SPS und HMI werden noch nicht gekoppelt.

Fachliches Ziel:
Wir haben einen Rundholzstamm als Folge elliptischer Scheiben. Für jede Scheibe sind bekannt:
- x [mm] als Position entlang der Stamm-Längsachse
- cy [mm] als horizontaler Mittelpunktversatz in y
- cz [mm] als vertikaler Mittelpunktversatz in z
- a, b [mm] als Ellipsen-Halbachsen mit a >= b > 0
- phi als Orientierung der großen Ellipsenachse
- quality
- valid

Die aktuelle Stammorientierung wird für die Optimierung als relative Lage `delta = 0` betrachtet.

Jede Scheibe liegt in der `y-z`-Ebene. Für die 2D-Geometrie ist `center=(cy,cz)^T`. Der reale und virtuelle Stamm dreht um die Längsachse `x`.

Für einen Kandidatenwinkel delta muss die komplette Scheibe in der `y-z`-Ebene um den Ursprung gedreht werden:
- center': R(delta) * center
- phi' = phi + delta
- a und b bleiben unverändert

Planunabhängige Kennzahl:
Für eine gewünschte Achse wird die Gerade durch die Maschinenachse geschnitten.
- vertical axis `u=(0,1)` = `z`-Richtung für `VORSCHNITT_KRITISCH`
- horizontal axis `u=(1,0)` = `y`-Richtung für `NACHSCHNITT_KRITISCH`

Ellipse:
(p-c)^T M (p-c) = 1
M = R(phi) * diag(1/a^2, 1/b^2) * R(phi)^T

Gerade p(t)=t*u einsetzen:
A = u^T M u
B = -2*u^T M c
C = c^T M c - 1
A*t^2 + B*t + C = 0

Aus den beiden Schnittpunkten t_neg <= 0 <= t_pos wird die symmetrisch nutzbare Geometrie:
r_sym = min(t_pos, -t_neg)
G_slice = 2*r_sym

Falls kein reeller Schnitt existiert oder der Ursprung nicht zwischen den beiden Schnittpunkten liegt, ist der symmetrische Bereich für diesen Fall 0 bzw. der Fall entsprechend ungültig zu markieren.

Der Stamm-Score eines Winkels ist nach Vorverarbeitung:
G_log(delta) = min(G_slice_i(delta)) über alle gültigen Scheiben im Evaluationsbereich.

Prioritätsmodi:
- `VORSCHNITT_KRITISCH`: vertikalen Score maximieren.
- `NACHSCHNITT_KRITISCH`: horizontalen Score maximieren.

Optimierung:
1. delta=0 muss immer Kandidat sein.
2. Wegen 180°-Symmetrie genügt ein 180°-Suchraum, jedoch müssen mechanische Limits und die vorhandene Winkelkonvention des Projekts respektiert werden.
3. Implementiere eine robuste Grob-/Feinsuche, standardmäßig z.B. 1° grob und 0,25° oder 0,1° fein, konfigurierbar.
4. Bei praktisch gleichen Scores (`score_tolerance_mm`) den kleineren absoluten Drehweg bevorzugen.
5. Optional ein Plateau-/Stabilitätskriterium als weiterer Tie-Breaker, falls es sauber in die bestehende Architektur passt.
6. `gain_mm = score_optimal - score_current`.
7. Wenn `gain_mm < min_gain_mm` oder `abs(delta) < min_rotation_deg`, `should_rotate=false` und kein unnötiger Stellwinkel ausgeben.
8. Kandidaten auf mechanische positive/negative Winkelgrenzen beschränken.

Implementiere passende Domänenmodelle. Nutze Namenskonventionen und Sprache des bestehenden Projekts. Empfohlene Konzepte:
- StemSlice
- StemSliceModel / StemModel
- PriorityMode
- OptimizationConfig
- RotationConstraints
- SliceScore / LogScore
- OptimizationResult

`OptimizationResult` soll mindestens enthalten:
- valid
- mode
- delta_angle_deg
- gain_mm
- should_rotate
- limiting_x_current_mm
- limiting_x_optimal_mm
- score_current_mm (Diagnose)
- score_optimal_mm (Diagnose)
- model_quality oder Platzhalter, falls die Measurement-Phase noch nicht existiert
- reason_code

Vorverarbeitung:
Implementiere oder bereite eine klare Schnittstelle für:
- regelmäßiges x-Raster,
- Entfernen ungültiger Ausreißer,
- kurze Lücken interpolieren,
- robuste Glättung von cy, cz, a, b,
- phi korrekt als zyklische Winkelgröße behandeln / unwrapen,
- Evaluationsbereich x_eval_start/x_eval_end.

Falls es im aktuellen Stack bereits geeignete Signal-/Filterfunktionen gibt, wiederverwenden. Ansonsten eine kleine, nachvollziehbare Lösung implementieren. Nicht überkomplizieren.

Pflichttests:
1. Perfekt zentrierter Kreis: Score unabhängig vom Winkel, Optimierer bevorzugt 0°.
2. Zentrierte Ellipse, vertikale Priorität: große Achse vertikal.
3. Zentrierte Ellipse, horizontale Priorität: große Achse horizontal.
4. Kreis mit Mittelpunktversatz: Rotationswirkung auf die jeweilige Achse korrekt.
5. Ellipse + Mittelpunktversatz: erwarteter Kompromiss aus Krümmung und Ovalität.
6. Mehrere Scheiben: schlechteste Scheibe bestimmt Score.
7. Limitierende Scheibe darf zwischen aktueller und optimaler Lage wechseln.
8. 180°-Symmetrie numerisch prüfen.
9. Ursprung außerhalb Ellipse / kein symmetrischer Schnitt: sauberer Fehler- oder Nullfall.
10. Tangential-/nahe-Tangentialfall numerisch stabil.
11. Tie-Breaking wählt bei Scores innerhalb Toleranz den kleineren Drehweg.
12. Mindestgewinn verhindert unnötige Drehung.
13. Mechanische Winkelgrenzen werden eingehalten.
14. Ergebnis ist deterministisch.
15. `gain_mm` ist nicht negativ, solange 0° ein erlaubter Kandidat ist, abgesehen von definierter Fließkommatoleranz.

Zusätzliche Testdaten:
Erzeuge kleine synthetische StemModel-Builder/Fixtures für:
- gerade/rund,
- stark oval,
- stark gekrümmt,
- Krümmung und Ovalität gleichgerichtet,
- Krümmung und Ovalität gegeneinander verdreht,
- lokale Engstelle,
- verrauschte Scheiben,
- kurze Datenlücke.

Entwicklungsregeln:
- Reiner Kern ohne Kamera-/SPS-/UI-Abhängigkeiten.
- Intern möglichst double/64-bit verwenden.
- Winkel intern bevorzugt in Radiant, externe Schnittstellen dürfen Grad verwenden.
- Keine unnötigen Rundungen im Kern.
- Mathematische Formeln in Code-Kommentaren oder Doku kurz nachvollziehbar dokumentieren.
- Keine Produkt- oder Einschnittlogik einführen.
- Keine fiktive `reserve_mm`-Variable verwenden; fachlich korrekt `available_geometry`, `symmetric_span`, `gain_mm` o.ä.

Optional aber sehr erwünscht:
- Hilfsfunktion, die für einen ganzen Winkelbereich eine `angle -> score`-Kurve erzeugt. Diese wird später für Diagnose und HMI gebraucht.
- Benchmark/Performance-Test mit realistisch vielen Scheiben und Kandidatenwinkeln.

Am Ende:
1. Alle relevanten Tests ausführen.
2. Linter/Formatter/Typechecker gemäß Repository ausführen.
3. Kurze Zusammenfassung der neuen Module, öffentlichen APIs, mathematischen Entscheidungen und Testergebnisse liefern.
4. Eventuelle Unterschiede zur Spezifikation explizit nennen.
```

---

# 5. Codex Prompt 2 – Kamera-/Messpipeline zum Scheibenmodell erweitern

```text
Erweitere nun das bestehende Kameraprojekt so, dass aus der vorhandenen Krümmungs-/Segmentierungsinformation ein `StemSliceModel` für den bereits implementierten Optimierer erzeugt werden kann.

Lies zuerst:
- `stammoptimierung_spezifikation.md`
- den Implementierungsplan aus Phase 0
- die bereits implementierten Domänenmodelle/Geometry-APIs aus Phase 1

Ziel dieses Arbeitspakets:
Die bestehende Kamera bleibt die primäre Sensorquelle. Wir wollen kein millimetergenaues 3D-Mesh, sondern pro Position entlang `x` eine robuste Ellipsennäherung in der `y-z`-Querschnittsebene mit Mittelpunktversatz `cy/cz`.

Benötigtes Endmodell pro Scheibe:
- x
- cy
- cz
- a
- b
- phi
- quality
- valid

Nutze vorhandene Funktionen für:
- Stammsegmentierung,
- Kontur/Maske,
- Kalibrierung Pixel -> mm,
- Stammposition entlang x,
- Krümmung/Mittellinie,
- Rotation/Drehwinkeltracking.

WICHTIG:
Erfinde nicht parallel eine zweite Segmentierung oder einen zweiten Drehwinkeltracker, wenn es im Projekt bereits belastbare Komponenten gibt. Kapsle bestehende Ergebnisse über Adapter.

Messkonzept:
1. Während einer kontrollierten Stammrotation werden für diskrete x-Scheiben Beobachtungen gesammelt.
2. Pro Beobachtung sollen mindestens vorliegen:
   - x,
   - Rotationswinkel relativ zu einer Messreferenz,
   - projizierte Stammmitte in der messbaren Bildachse,
   - sichtbare Stamm-/Silhouettenbreite,
   - Qualitätswert.
3. Aus mehreren Winkeln wird je Scheibe ein einfaches räumliches Modell geschätzt:
   - Mittelpunktversatzvektor cy/cz aus dem winkelabhängigen Verlauf der projizierten Mittellinie,
   - a/b/phi aus dem winkelabhängigen Verlauf der sichtbaren Breite.
4. Die genaue Fit-Methode soll klein, mathematisch nachvollziehbar und testbar sein. Bevorzuge Least-Squares/robuste Regression und bekannte Ellipsen-Projektionsbeziehungen gegenüber einem ML-Modell.
5. Das Ergebnis muss mit Fit-Residual/Qualitätswert versehen werden.

Winkelabhängige Mittellinie:
Prüfe, wie das bestehende Projekt Krümmung während einer Rotation bereits ermittelt. Falls passend, nutze ein harmonisches Modell der Form
projected_center(theta) = offset0 + A*cos(theta) + B*sin(theta)
oder die mathematisch äquivalente Darstellung, um die beiden räumlichen Komponenten des Mittelpunktversatzes zu rekonstruieren.

Winkelabhängige Breite:
Für eine Ellipse mit Halbachsen a,b und Orientierung phi variiert die Silhouettenbreite periodisch mit dem Drehwinkel. Implementiere den Fit so, dass a, b und phi robust geschätzt werden können. Verwende die korrekte Projektion einer Ellipse; nicht nur Max/Min einzelner verrauschter Messpunkte.

Falls die konkrete Kameraperspektive des Projekts eine andere Projektionsgeometrie erfordert, leite sie aus der vorhandenen Kalibrierung ab und dokumentiere die Abweichung. Keine ungeprüfte Orthografie-/Parallelprojektion erzwingen, wenn das Projekt perspektivisch kalibriert ist.

x-Raster:
- Standardmäßig 50 mm, konfigurierbar.
- Beobachtungen der Bildkontur sauber auf dieses Raster abbilden.
- Endbereiche mit geringer Messqualität markieren.

Qualität und Gültigkeit:
Pro Scheibe mindestens bewerten:
- Anzahl Beobachtungen,
- Winkelabdeckung,
- Segmentierungsqualität,
- Fit-Residual Mitte,
- Fit-Residual Breite/Ellipse,
- Plausibilität a>=b>0,
- lokale Konsistenz mit Nachbarscheiben.

Gesamtmodell:
- gültiger Scheibenanteil,
- maximale Datenlücke,
- Winkelabdeckung,
- mittlere/hohe Fitfehler
zu einem nachvollziehbaren `model_quality` und Status zusammenführen.

Vorverarbeitung:
Nutze die in Phase 1 bereitgestellte Preprocessing-Schnittstelle oder integriere sie sauber:
- kurze Lücken interpolieren,
- Ausreißer verwerfen,
- cy/cz/a/b glätten,
- phi unwrapen und glätten,
- echte längere Engstellen nicht wegglätten.

Offline-Replay:
Baue eine Möglichkeit, gespeicherte Messsequenzen oder extrahierte Beobachtungen erneut durch den Model Builder laufen zu lassen, ohne Live-Kamera. Falls das Repository bereits einen Replay-/Recording-Mechanismus hat, erweitere ihn.

Debug-/Diagnosedaten:
Für jede Scheibe optional speicherbar:
- Rohbeobachtungen angle/center/width/quality,
- Fitparameter,
- Residuen,
- finaler Slice-Datensatz.

Tests:
1. Synthetische Winkelbeobachtungen für einen Kreis -> a≈b, phi irrelevant/stabil behandelt.
2. Synthetische Ellipse -> a,b,phi innerhalb definierter Toleranz rekonstruieren.
3. Verschobene Mittellinie -> horizontalen/vertikalen Mittelpunktversatz `cy,cz` aus dem harmonischen Verlauf korrekt rekonstruieren.
4. Rauschen -> robuste Schätzung bleibt stabil.
5. Fehlende Winkelbereiche -> Qualität sinkt; bei zu geringer Abdeckung invalid.
6. Einzelne Ausreißer -> Fit darf nicht kippen.
7. Mehrere x-Scheiben mit langsam variierender Geometrie -> Modell folgt Verlauf.
8. Kurze Lücke -> Interpolation.
9. Lange Lücke -> Gesamtmodell degraded/invalid gemäß Konfiguration.
10. Pixel-mm-Kalibrierung wird verwendet und Einheiten sind nachweisbar korrekt.

Integrationstest:
Erzeuge aus einer synthetischen Messsequenz zuerst ein StemSliceModel und führe dieses danach durch den echten RotationOptimizer aus Phase 1. Prüfe, dass die erwartete Eindrehentscheidung entsteht.

Nicht tun:
- kein Einschnittprogramm,
- keine Brettsimulation,
- kein monokulares KI-Depth-Modell,
- kein unnötiges 3D-Mesh,
- keine UI-Mathematik.

Am Ende:
- Tests/Linter ausführen,
- reale Integrationspunkte benennen,
- beschreiben, welche bestehende Krümmungslogik wiederverwendet wurde,
- bekannte Messgrenzen dokumentieren,
- ein kleines Beispiel eines gespeicherten/replaybaren Scheibenmodells bereitstellen, sofern der Projektstil dies zulässt.
```

---

# 6. Codex Prompt 3 – Runtime, Frontend-Prioritätsquelle, Zustandsmodell und Logging

```text
Integriere den vorhandenen StemSliceModel-Builder und RotationOptimizer in den Laufzeitablauf der Anwendung. Für Entwicklung, Test und Inbetriebnahme existiert noch KEINE SPS-Verbindung. Der Prioritätsmodus muss deshalb zunächst im Frontend vorgegeben werden können. Die Architektur muss aber so aufgebaut sein, dass später eine SPS exakt denselben fachlichen PriorityMode liefern kann, ohne Änderungen am Geometriekern oder Optimierer.

Lies vor Änderungen:
- `stammoptimierung_spezifikation.md`
- Implementierungsplan
- Geometry/Optimizer-API
- Measurement/StemSliceModel-API
- vorhandene Runtime-/Frontend-Struktur des Repositories
- vorhandene SPS-/Feldbusstruktur nur zur Vorbereitung, falls bereits vorhanden

Verbindliches Koordinatensystem:
- x = Stamm-Längsachse / Förderrichtung
- y = Querachse / horizontal links-rechts
- z = Vertikalachse / oben-unten
- Stammscheiben liegen in der y-z-Ebene
- Rotation erfolgt um x
- limitierende Längspositionen heißen `limiting_x_*`

Fachliches Ziel:
Die Optimierungssoftware kennt keine Produktgeometrie. Sie erhält nur einen PriorityMode:
- NONE
- VORSCHNITT_KRITISCH -> vertikale Geometrie entlang z / oben-unten maximieren
- NACHSCHNITT_KRITISCH -> horizontale Geometrie entlang y / links-rechts maximieren

1. Priority-Source-Abstraktion
Implementiere eine kleine klare Abstraktion, z.B. `PriorityModeProvider`/`PrioritySource`, sodass Runtime und Optimierer nicht wissen müssen, woher der Modus stammt.

Mindestens zwei fachliche Quellen vorsehen:
- FRONTEND: jetzt vollständig implementieren und als Standard für Entwicklung/Test/Inbetriebnahme verwenden.
- PLC: Schnittstelle/Adaptergrenze vorbereiten; nur dann konkret anbinden, wenn das Repository bereits eine nutzbare SPS-Verbindung besitzt. Keine SPS-Verbindung erfinden oder für dieses Arbeitspaket voraussetzen.

Konfiguration etwa:
priority_source = FRONTEND | PLC

Aktueller Default:
priority_source = FRONTEND

2. Frontend-Vorgabe
Das Frontend muss `priority_mode` setzen können:
- VORSCHNITT_KRITISCH
- NACHSCHNITT_KRITISCH
- optional NONE

Eine Modusänderung soll, sofern ein gültiges aktuelles StemSliceModel vorhanden ist, eine Neuberechnung mit genau demselben Modell auslösen können. Eine neue Kameramessung darf nicht erforderlich sein, nur weil zwischen Vorschnitt- und Nachschnittpriorität umgeschaltet wird.

Die Runtime soll die aktive Quelle und den Modus eindeutig führen:
- priority_source
- priority_mode

3. Fachliche Eingänge der Runtime
Für die aktuelle Softwarephase mindestens:
- optimization_enable
- priority_source
- priority_mode aus dem Provider
- stem_id optional
- Messzyklus Start/Ende oder vorhandene äquivalente Events
- mechanische Drehgrenzen aus lokaler Konfiguration

Später dürfen dieselben Werte ganz oder teilweise aus der SPS kommen.

4. Ergebnisdaten
Bereitstellen:
- result_ready
- result_valid
- should_rotate
- rotation_delta_deg
- gain_mm
- model_quality
- reason_code
- limiting_x_current_mm / limiting_x_optimal_mm für Diagnose
- interne Scores optional für Diagnose

5. Zustandsmaschine
Implementiere passend zum bestehenden Runtime-Stil logisch etwa:
IDLE
-> MEASURING
-> MODEL_BUILDING
-> OPTIMIZING
-> RESULT_READY
-> DONE/APPLIED

Fehler/Abbruchzustände mindestens:
- optimization disabled
- invalid priority mode
- insufficient measurement
- insufficient angle coverage
- invalid stem model
- optimizer failed
- low model quality
- low gain / no rotation needed
- mechanical rotation constraint conflict

6. Sicherheits-/Konsistenzlogik
- Bei ungültigem oder qualitativ unzureichendem Modell niemals eine gültige Drehempfehlung ausgeben.
- Bei `gain_mm < min_gain_mm`: `should_rotate=false`.
- Bei Drehwinkel unter `min_rotation_deg`: `should_rotate=false`.
- `rotation_delta_deg` eindeutig gemäß der festgelegten positiven Rotation um x.
- Alte Resultate beim Start eines neuen Stammes invalidieren.
- Eine Frontend-Modusänderung darf kein altes Resultat unter neuem Modus anzeigen: Resultat invalidieren/recompute atomar und `result_ready` erst für die neue Berechnung setzen.
- Source-Wechsel FRONTEND <-> PLC darf keine mehrdeutige Priorität erzeugen. Genau eine Quelle ist aktiv.

7. Logging pro Stamm/Berechnung
Mindestens speichern:
- stem_id / cycle id
- timestamp
- priority_source
- priority_mode
- verbindliche Koordinaten-/Winkelkonvention bzw. Softwareversion
- Konfigurationssnapshot
- Modellqualität
- final verwendetes StemSliceModel oder Referenz
- angle->score curve, sofern verfügbar
- score current / optimal
- gain_mm
- delta_angle_deg
- limiting_x_current_mm / limiting_x_optimal_mm
- should_rotate
- reason_code
- Mess-/Fit-Qualitätsdaten soweit praktikabel

Replay muss mit denselben Daten möglich sein.

8. Konfiguration
Integriere mindestens:
- priority_source (Default FRONTEND)
- slice_spacing_mm
- x_eval_start_mm
- x_eval_end_mm
- smoothing_window_slices
- coarse_angle_step_deg
- fine_angle_step_deg
- fine_search_half_width_deg
- score_tolerance_mm
- min_gain_mm
- min_rotation_deg
- min_valid_slice_ratio
- min_model_quality
- max_interpolation_gap_mm
- rotation positive/negative limits

9. Vorbereitung der späteren SPS-Anbindung
Dokumentiere eine kleine fachliche Mapping-Schnittstelle, aber mache die aktuelle Funktion nicht von SPS-Hardware abhängig. Später soll die SPS im Wesentlichen nur `priority_mode` und ggf. Enable/Zyklus-/Grenzsignale liefern. Der Optimierer bleibt unverändert.

Wenn bereits eine echte SPS-Abstraktion im Repository existiert, darfst du einen nicht-aktivierten/konfigurierbaren `PlcPriorityModeProvider` vorbereiten. Keine Testlogik in Produktionsadapter mischen.

10. Tests
- Defaultquelle ist FRONTEND.
- Frontend kann Vorschnittmodus setzen -> vertikale z-Priorität.
- Frontend kann Nachschnittmodus setzen -> horizontale y-Priorität.
- Umschalten des Modus auf demselben gültigen Stamm führt zu neuer Optimierung ohne neue Messung.
- Resultat trägt korrekten `priority_source` und `priority_mode`.
- Neues Stem-Event invalidiert altes Ergebnis.
- Ungültiges Modell -> kein Rotate.
- Low gain -> kein Rotate.
- Gute Messung -> Result Ready mit korrektem Winkel/Gain.
- Mechanische Limits werden eingehalten.
- Replay enthält Quelle und Modus.
- Falls ein PLC-Provider vorbereitet wird: Source-Umschaltung ist eindeutig und Frontend kann PLC-Werte nicht überschreiben.

Am Ende:
1. Tests, Linter, Typechecker ausführen.
2. Prioritätsquellen und deren Datenfluss dokumentieren.
3. Ablaufbeschreibung für einen Stammzyklus und für einen reinen Moduswechsel auf demselben Stamm ergänzen.
4. Explizit dokumentieren, dass aktuell keine SPS-Verbindung für die Funktion benötigt wird.
5. Alle Safety-/Fallback-Pfade kurz zusammenfassen.
```

---

# 7. Codex Prompt 4 – HMI/Visualisierung vollständig umsetzen

```text
Implementiere jetzt die Bedien- und Diagnosevisualisierung für die Stamm-Eindrehoptimierung. Nutze das im Repository vorhandene UI-Framework und die bestehenden Design-/Komponenten-Konventionen. Keine neue Frontend-Technologie einführen.

Lies zuerst `stammoptimierung_spezifikation.md`, insbesondere die Kapitel zur Visualisierung.

Ziel:
Ein Bediener soll in wenigen Sekunden verstehen:
1. welche Richtung aktuell priorisiert wird und aus welcher Quelle der Modus stammt,
2. wie weit gedreht werden soll,
3. welchen geometrischen Gewinn die Drehung bringt,
4. wie der Stamm vereinfacht geometrisch aussieht,
5. wo die limitierende Stelle liegt.

WICHTIGE FACHLICHE REGEL:
Die UI darf nicht behaupten, eine Produktreserve zu kennen. Sie kennt keinen Einschnittplan.
Verwende Begriffe wie:
- Geometriegewinn
- nutzbare Geometrie
- priorisierte Richtung
- limitierende Scheibe
Nicht als Hauptbegriff verwenden:
- Produktreserve
- Brettreserve
- Vorschnittreserve in mm
- Nachschnittreserve in mm

Hauptansicht:
A. Prioritätswahl + Entscheidungskachel
- Bei `priority_source = FRONTEND`: gut sichtbarer Umschalter für `VORSCHNITT_KRITISCH` und `NACHSCHNITT_KRITISCH`.
- Aktive Quelle anzeigen, z.B. `Quelle: Frontend (Test/Inbetriebnahme)`.
- Bei späterem `priority_source = PLC`: Umschalter read-only/deaktiviert, SPS-Modus nur anzeigen.
- Priorität: `VORSCHNITT / OBEN-UNTEN` oder `NACHSCHNITT / LINKS-RECHTS`
- große Drehanweisung, z.B. `+18°`
- klare Richtung / Pfeil gemäß Winkelkonvention
- großer Hauptwert `+34 mm Geometriegewinn`
- bei `should_rotate=false`: klar `Keine Korrekturdrehung` bzw. vorhandene passende Formulierung

B. Stamm-Längsansicht
- schematischer Stamm entlang x
- Durchmesser-/Ovalitätsentwicklung sinnvoll abstrahieren
- Mittellinie/Krümmung anzeigen
- aktuelle limitierende Position markieren
- optimale limitierende Position optional zusätzlich markieren
- keine fotorealistische 3D-Darstellung erforderlich

C. Kritischer Querschnitt
- Maschinenachse fest im Ursprung
- aktuelle Ellipse als dünne/gestrichelte Kontur
- optimierte Ellipse als deutlichere Kontur
- priorisierte Achse hervorheben
- Drehpfeil von aktuell nach optimiert
- symmetrisch nutzbare Mittelsehne für die priorisierte Achse visualisieren
- keine Produkt-/Brettkontur einzeichnen

D. Differenz entlang der Stammlänge x
Berechne nicht erneut Geometrie in der UI. Verwende vom Backend/ViewModel vorbereitete Werte:
DeltaG_i = G_i(optimal) - G_i(current)
über x.

Darstellung:
- x auf horizontaler Achse
- Null-Linie
- positive Bereiche = lokaler Geometriegewinn
- negative Bereiche = lokale Verringerung
- globale Hauptkennzahl `gain_mm` separat lassen
- limitierende Position(en) markieren

Hauptanzeige absolute Werte:
Zeige in der Hauptansicht NICHT zwingend:
`current score -> optimal score`.
Der Benutzer hat sich ausdrücklich für die reduzierte Darstellung entschieden.
Prominent ist nur:
- relative Drehung
- Differenz/Gewinn in der priorisierten Richtung

Diagnoseansicht:
Darf zusätzlich enthalten:
- score_current_mm
- score_optimal_mm
- model_quality
- limiting_x_current_mm
- limiting_x_optimal_mm
- angle->score curve
- Anzahl gültiger Scheiben
- Fit-/Messqualität
- aktive Konfiguration

Angle->score curve:
Falls bereits aus Phase 1 geliefert, visualisiere sie nur in Diagnose, nicht zwingend im Hauptbild.
Markiere:
- 0° current
- selected optimum
- score tolerance / plateau optional

State handling:
- IDLE
- MEASURING
- MODEL_BUILDING
- OPTIMIZING
- RESULT_READY
- INVALID/ERROR
müssen jeweils eine verständliche Darstellung haben.
Keine alten Resultate sichtbar lassen, wenn ein neuer Messzyklus läuft, außer klar als historisch markiert.

Responsiveness/Performance:
- UI darf bei einer neuen Optimierung nicht blockieren.
- Zeichnung mit typischer Scheibenanzahl flüssig halten.
- Geometrie für die Anzeige im Backend/ViewModel vorbereiten, wenn das bestehende UI dadurch einfacher bleibt.

Accessibility:
- Farbe nicht als einziges Unterscheidungsmerkmal.
- aktuelle/optimierte Kontur auch über Linienstil unterscheiden.
- Texte/Einheiten klar und groß genug für industrielle Bedienoberfläche.

Tests:
1. Bei `priority_source = FRONTEND` kann der Benutzer zwischen Vorschnitt- und Nachschnittmodus umschalten.
2. Vorschnittmodus -> vertikale z-Prioritätsanzeige.
3. Nachschnittmodus -> horizontale y-Prioritätsanzeige.
4. +Winkel und -Winkel -> Pfeil/Drehrichtung korrekt.
5. gain=0 / low gain -> Keine Korrekturdrehung.
6. invalid model -> kein Drehwert als gültige Empfehlung darstellen.
7. aktuelle und optimale Ellipse stammen aus ViewModel/Backend und sind geometrisch konsistent.
8. limitierende Scheibe korrekt markiert.
9. DeltaG_i-Plot übernimmt Daten ohne UI-eigene Neuberechnung.
10. Diagnoseansicht zeigt absolute Scores, Hauptansicht nicht.
11. Snapshot/visual regression tests verwenden, falls im Projekt etabliert.

ViewModel:
Falls noch nicht vorhanden, definiere eine UI-spezifische, read-only Struktur ungefähr mit:
- priorityLabel
- priorityAxis
- shouldRotate
- rotationDeltaDeg
- rotationDirectionLabel/Icon
- gainMm
- stemLongitudinalGeometry[]
- currentCriticalCrossSection
- optimalCriticalCrossSection
- deltaGeometryByX[]
- limiting positions entlang x
- prioritySource
- quality/status
- diagnostics optional

Die UI darf nicht direkt die Geometrie-/Optimierungsalgorithmen aufrufen. Eine Frontend-Modusänderung geht über Runtime/Controller/State-Management und verwendet danach den normalen Optimierungspfad.

Am Ende:
- Anwendung bauen/testen,
- vorhandene UI-Tests ausführen,
- kurze Beschreibung der neuen Ansicht liefern,
- Screenshots nur erzeugen, wenn das Projekt dafür bereits einen automatisierten Weg besitzt,
- bekannte Darstellungsgrenzen nennen.
```

---

# 8. Codex Prompt 5 – Simulator, Replay, Validierung und Inbetriebnahme-Werkzeuge

```text
Baue jetzt einen produktionsnahen Simulator/Replay-Modus und eine Validierungsstrecke für die Stamm-Eindrehoptimierung. Ziel ist, die Logik vor und während der Inbetriebnahme mit künstlichen und realen Stämmen nachvollziehbar prüfen zu können.
Wobei vorrang reale Stämme haben! 
Nutze ausschließlich die bereits implementierten produktiven APIs für:
- StemSliceModel
- Preprocessing
- Geometry Core
- RotationOptimizer
- HMI ViewModel
Die Simulation darf keine zweite Schattenimplementierung der Mathematik enthalten.

1. Synthetischer Stammgenerator
Implementiere Generatoren/Fixtures mit reproduzierbarem Seed für:
- runder gerader Stamm,
- runder gekrümmter Stamm,
- zentrierte Ellipse,
- elliptischer gekrümmter Stamm,
- Ovalitätswinkel entlang x variierend,
- Krümmungsrichtung entlang x variierend,
- Durchmesser/Taper entlang x,
- einzelne echte lokale Engstelle,
- Messrauschen,
- einzelne Ausreißer,
- kurze und lange Datenlücken.

Parameter sollen in realen Einheiten mm/deg angegeben werden können.

2. Offline-Optimizer-Simulator
Für ein StemSliceModel:
- Priority Mode über dieselbe Frontend-/Runtime-Schnittstelle umschalten,
- mechanische Winkelgrenzen setzen,
- angle->score curve berechnen,
- optimum markieren,
- delta_angle_deg und gain_mm ausgeben,
- limiting slices zeigen,
- dieselben HMI-Komponenten/ViewModels wie Produktion verwenden, soweit technisch möglich.

3. Replay realer Stämme
Nutze das Loggingformat aus Phase 3.
Ein gespeicherter Stamm muss erneut geladen und mit:
- gespeicherter Konfiguration,
- alternativ geänderter Testkonfiguration
berechnet werden können.

Beim Replay anzeigen/ausgeben:
- gespeichertes Originalresultat,
- neu berechnetes Resultat,
- Differenzen,
- Warnung bei abweichender Software-/Konfigurationsversion.

4. Referenz-/Regressionstestfälle
Lege eine kleine, versionierte Sammlung deterministischer Referenzfälle an.
Für jeden Fall speichere/definiere:
- Eingangsstamm,
- Prioritätsmodus,
- relevante Konfiguration,
- erwarteter Winkelbereich,
- erwarteter Gain-Bereich,
- erwartete limitierende Region.

Die Tests sollen robust gegen minimale Floating-Point-Unterschiede sein und keine unnötig exakten Winkelwerte erzwingen, wenn ein Plateau vorliegt.

5. Monte-Carlo/Robustheit
Ergänze einen Entwickler-Test/Tool, das auf einem Basisstamm viele Rauschrealisierungen erzeugt und auswertet:
- Verteilung delta_angle_deg,
- Verteilung gain_mm,
- Häufigkeit wechselnder limitierender Scheiben,
- Anteil `should_rotate`.

Ziel ist zu erkennen, ob die Entscheidung bei realistischem Messrauschen springt.

6. Parametrierhilfe
Erzeuge eine Auswertung, mit der während der Inbetriebnahme folgende Parameter untersucht werden können:
- slice_spacing_mm
- smoothing_window_slices
- angle step coarse/fine
- score_tolerance_mm
- min_gain_mm
- min_rotation_deg

Es genügt eine Entwickler-/Diagnosefunktion; keine automatische Selbstoptimierung erforderlich.

7. Performance
Messe für realistische Größenordnungen:
- z.B. 3-6 m Stammlänge entlang x,
- 25-100 mm Scheibenabstand,
- vollständiger Winkelsuchraum,
- Grob-/Feinsuche.

Nutze bestehendes Benchmarking, falls vorhanden. Vermeide vorzeitige Optimierungen. Falls Performance unzureichend ist, optimiere nur nach Profiling und erhalte die getestete Referenzlogik.

8. Export
Falls im Projekt sinnvoll, ermögliche Export eines Diagnosepakets mit:
- stem model
- config
- priority mode
- result
- score curve
- quality metadata
als JSON/CSV/Projektformat. Keine riesigen Rohvideos erzwingen.

9. Dokumentation
Erstelle eine Inbetriebnahme-Doku mit:
- wie ein Replay gestartet wird,
- wie ein synthetischer Stamm erzeugt wird,
- wie Vorschnitt/Nachschnitt über die Frontend-Prioritätsvorgabe umgeschaltet wird,
- wie die Scorekurve interpretiert wird,
- wie min_gain_mm und score_tolerance_mm beurteilt werden,
- wie ein problematischer Stamm als Regressionstest gesichert wird.

Abnahmetests:
- Ein zentrierter Kreis erzeugt keinen relevanten Drehgewinn.
- Eine zentrierte Ellipse wird entsprechend dem Priority Mode ausgerichtet.
- Ein gekrümmter Stamm zeigt erwartete Richtungsabhängigkeit.
- Ein kombinierter Stamm demonstriert einen realen Kompromiss zwischen Bogen und Ovalität.
- Replay desselben Inputs mit gleicher Config ist deterministisch.
- UI im Simulator erhält dieselben Ergebnisobjekte wie im Produktionspfad.

Am Ende:
- alle Tests/Benchmarks ausführen,
- auffällige Instabilitäten dokumentieren,
- konkrete Vorschläge für erste Inbetriebnahme-Defaults liefern, aber als Vorschläge/Startwerte kennzeichnen, nicht als endgültig validierte Produktionswerte.
```

---

# 9. Optionaler Codex Prompt 6 – Abschlussreview vor Produktionsversuch

```text
Führe einen vollständigen technischen Review der implementierten Stamm-Eindrehoptimierung gegen `stammoptimierung_spezifikation.md` durch.

Ziel ist kein neues Feature, sondern ein produktionsnahes Audit vor einem ersten Maschinenversuch.

Prüfe systematisch:

A. Fachliche Trennung
- Keine Einschnittplan- oder Brettlogik im Optimierer.
- Priority Mode kommt eindeutig aus genau einer aktiven Quelle; aktuell FRONTEND, später optional PLC.
- UI nennt den berechneten Wert Geometriegewinn/nutzbare Geometrie und nicht fälschlich Produktreserve.

B. Koordinaten/Winkel
- Koordinatensystem durchgängig: x = Stamm-Längsachse, y = horizontal/quer, z = vertikal; Scheiben in y-z; Rotation um x.
- positive Drehrichtung durchgehend konsistent.
- Grad/Radiant sauber getrennt.
- 180°-Symmetrie und mechanische Winkelabbildung korrekt.

C. Mathematik
- Ellipsenmatrix korrekt.
- virtuelle Rotation von center und phi korrekt.
- Mittelsehnen-Schnittquadratik korrekt.
- symmetrischer Span korrekt.
- globale Minimum-Logik korrekt.
- Tie-Breaking und min gain korrekt.

D. Datenqualität
- ungültige Scheiben sauber behandelt.
- Interpolation begrenzt.
- Glättung löscht keine relevanten echten Engstellen.
- model_quality wirkt als Freigabekriterium.

E. Runtime / Priority Source / spätere SPS
- stale results ausgeschlossen.
- ungültige Messung kann keine gültige Drehempfehlung erzeugen.
- `priority_source = FRONTEND` funktioniert ohne SPS-Verbindung vollständig.
- Moduswechsel im Frontend berechnet denselben gültigen Stamm ohne neue Messung neu.
- Source-Wechsel ist eindeutig; nie zwei aktive Quellen gleichzeitig.
- spätere PLC-Adaptergrenze ist vorbereitet, ohne den Optimierer daran zu koppeln.
- falls bereits SPS-Code existiert: Handshake sowie Restart/Disconnect-Verhalten sicher.

F. UI
- Hauptansicht sofort verständlich.
- nur Delta/Gain prominent.
- Prioritätsrichtung sichtbar.
- kritische Scheibe und Stammform nachvollziehbar.
- Diagnosewerte von Hauptwerten getrennt.

G. Reproduzierbarkeit
- pro Stamm ausreichender Snapshot.
- Replay identisches Ergebnis bei identischer Version/Config.
- angle->score curve verfügbar.

H. Tests
- alle in der Spezifikation genannten Kernfälle abgedeckt.
- fehlende Tests ergänzen.
- kritische Grenzfälle und numerische Stabilität prüfen.

I. Performance
- Laufzeit für realistische Stammlänge/Scheibenanzahl messen.
- unnötige Allokationen/Hotspots nur bei tatsächlichem Bedarf optimieren.

J. Dokumentation
- Priority-Source-Schnittstelle und spätere SPS-Signale,
- Konfigurationsparameter mit Einheiten,
- Winkelkonvention,
- Diagnose-/Replay-Ablauf,
- bekannte Einschränkungen
müssen dokumentiert sein.

Lieferumfang:
1. Review-Report mit PASS/WARN/FAIL je Bereich.
2. Gefundene FAIL-Punkte direkt beheben, sofern ohne fachliche Neubewertung möglich.
3. WARN-Punkte mit konkreter Empfehlung dokumentieren.
4. Gesamte Testsuite ausführen.
5. Einen kompakten Maschinenversuchs-Check erstellen:
   - Kalibrierung gültig?
   - Drehwinkeltracking gültig?
   - Modellqualität ausreichend?
   - Frontend-Priorität korrekt und aktive Quelle eindeutig?
   - Drehrichtung mit Maschine verifiziert?
   - min_gain sinnvoll?
   - Logging/Replay aktiv?
6. Keine neuen Features hinzufügen, die nicht für Korrektheit oder sichere Inbetriebnahme nötig sind.
```

---

# 10. Empfohlene Paketierung für GPT-5.6 Sol / Codex

Da größere Arbeitspakete möglich sind, ist folgende Aufteilung sinnvoll:

### Paket A
Prompt 0 allein: Repository-Audit.

### Paket B
Prompt 1 komplett: Geometriekern + Optimierer + Tests.

### Paket C
Prompt 2 komplett: Kamera -> Scheibenmodell + Replay-Grundlage.

### Paket D
Prompt 3 und Prompt 4 können bei überschaubarem Repository gemeinsam ausgeführt werden:
- Runtime + Frontend-Prioritätsquelle + SPS-ready Abstraktion,
- HMI.

Die echte SPS-Anbindung ist für die aktuelle Entwicklungsphase nicht erforderlich und kann später als separates Paket erfolgen.

### Paket E
Prompt 5: Simulator/Replay/Validierung.

### Paket F
Prompt 6: Abschlussreview.

---

# 11. Sinnvolle Zwischen-Abnahmepunkte

## Nach Paket B
Noch ohne reale Kamera muss ein synthetischer Stamm korrekt optimiert werden können.

Minimaler Demonstrator:

```text
Input:
- 100 Scheiben entlang x
- x/cy/cz/a/b/phi
- PriorityMode

Output:
- delta_angle_deg
- gain_mm
- limiting_x_current
- limiting_x_optimal
- score curve
```

Wenn dieser Teil nicht zuverlässig funktioniert, nicht mit der Kameraintegration fortfahren.

## Nach Paket C
Aus gespeicherten/Live-Kameradaten muss ein plausibles Scheibenmodell entstehen und der gleiche Optimierer darauf laufen.

## Nach Paket D
Ein kompletter Softwarezyklus ohne echte Sägemaschinenbewegung muss möglich sein:

```text
Frontend/Simulator -> Priority Mode -> Measurement -> Model -> Optimize -> HMI -> Result
```

## Nach Paket E
Reale Problemstämme müssen speicherbar, replaybar und als Regressionstest verwendbar sein.

---

# 12. Empfohlene erste Inbetriebnahme-Defaults

Diese Werte sind **Startwerte**, keine bereits validierten Produktionswerte:

```text
slice_spacing_mm              = 50
coarse_angle_step_deg         = 1.0
fine_angle_step_deg           = 0.25
fine_search_half_width_deg    = 2.0
score_tolerance_mm            = 1.0
min_gain_mm                   = 5.0
min_rotation_deg              = 2.0
smoothing_window_slices       = 3
```

`x_eval_start/end`, Qualitätsgrenzen und Interpolationslücken müssen anhand der realen Kamerageometrie und Anlagenmechanik festgelegt werden.

---

# 13. Fachliche Definition, die in jedem Codex-Lauf erhalten bleiben muss

Der wichtigste Satz des Projekts lautet:

> Die Software optimiert nicht den Einschnitt. Sie optimiert ausschließlich die Orientierung der gemessenen Stammgeometrie relativ zur festen Zentrierachse in einer von der aktiven Prioritätsquelle vorgegebenen Prioritätsrichtung (aktuell Frontend, später SPS).

Und der wichtigste ausgegebene Wert lautet:

> `gain_mm = nutzbare Geometrie im optimierten Winkel - nutzbare Geometrie in aktueller Lage`, jeweils für die von der aktiven Prioritätsquelle priorisierte Achse.

Diese beiden Regeln sollen als Architekturgrenze erhalten bleiben.
