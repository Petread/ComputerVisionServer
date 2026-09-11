# Replay, Validierung und Inbetriebnahme der Stamm-Eindrehoptimierung

## Zweck und Einsatzgrenze

Diese Werkzeuge prüfen gespeicherte und synthetische Stammdaten offline. Sie starten
weder die Anwendung noch Kamera-, SPS- oder Webdienste. Der produktive Rechenweg bleibt
dabei unverändert:

`Priority-Control -> Priority-Resolver -> Runtime-Node -> Preprocessing/Geometry/Optimizer -> HMI-ViewModel`

Die gemeinsame Implementierung liegt in
`backend/packages/cv-runtime/src/cv_runtime/stem_rotation_validation.py`. Sie ruft den
registrierten Node `analysis.stem_rotation_optimizer` und dessen produktiven Handler
auf. Die Werkzeuge enthalten keine zweite Score- oder Optimierungsformel. Alle Winkel
sind signierte relative Drehungen um Maschinen-`+x`; die Betrachtungsrichtung und die
positive Drehrichtung werden mit dem Modell beziehungsweise Ergebnis versioniert.

Reale Stämme haben Vorrang. Synthetische Stämme sind Entwicklungs- und
Fehlerisolationshilfen und werden in Berichten als `synthetic:*` gekennzeichnet. Im
Repository ist zurzeit keine reale, freigegebene Stammmessung eingecheckt. Vor einer
Produktionsfreigabe müssen deshalb Zielserver-Replays mit repräsentativen realen
Stämmen ergänzt und fachlich referenziert werden.

## Unterstützte reale Eingaben

Die Replay-Werkzeuge erkennen diese vorhandenen Produktionsformate:

1. `stem.rotation-optimization.v1`: bevorzugtes Result-History-Exportformat. Es enthält
   Originalresultat, Roh- und verarbeitete Scheiben, aktuelle Rotationsreferenz und
   Kalibrierungsbindungen, Priority-Control, Konfiguration, mechanische Grenzen, Scorekurve und
   Versionsinformationen. Optional aktivierte Rohbeobachtungen werden ebenfalls mitgeführt.
2. `stem.slice-model.v1`: eingefrorenes Scheibenmodell ohne ursprüngliches
   Optimierungsergebnis.
3. `stem.projection-observations.v1`: kalibrierte Winkel-/Center-/Breitenbeobachtungen.
   Dieses Format läuft zuerst durch den produktiven `StemSliceModel`-Builder und ist
   deshalb für Fit- und Preprocessing-Analysen wertvoller als ein bereits verarbeitetes
   Modell.

Auch übliche Result-History-Hüllen mit `result`, `derived_preview` oder
`derived_outputs[].payload.result` werden gelesen. Unbekannte Formate werden
abgewiesen; es gibt keine heuristische Interpretation von Pixelwerten oder Winkeln.

## Replay eines realen Stamms

Die Befehle werden in einer eingerichteten Entwicklungs-/Diagnoseumgebung des Servers
ausgeführt, nachdem das Update eingespielt wurde. Auf dem Bearbeitungs-PC muss und soll
die Anwendung nicht gestartet werden.

```bash
uv run python scripts/dev/replay_stem_rotation_optimization.py \
  result.json \
  --output replay-report.json \
  --export-dir diagnostic-package
```

Der Report enthält das gespeicherte Original, das neu berechnete Produktivresultat,
numerische Differenzen sowie Warnungen bei abweichender Softwareversion,
Konfiguration, Priority-Mode oder Konfigurationshash. Eine identische Konfiguration
muss dieselbe fachliche Entscheidung, Scorekurve und limitierende Position liefern;
der Offline-Kontext verwendet dazu den gespeicherten Messzeitpunkt deterministisch.

Eine Testkonfiguration ist ein JSON-Objekt mit Node-Konfigurationswerten:

```json
{
  "score_tolerance_mm": 0.5,
  "min_gain_mm": 4.0,
  "min_rotation_deg": 1.5,
  "rotation_negative_limit_deg": -70.0,
  "rotation_positive_limit_deg": 80.0
}
```

```bash
uv run python scripts/dev/replay_stem_rotation_optimization.py \
  result.json \
  --config trial-config.json \
  --priority-mode NACHSCHNITT_KRITISCH \
  --output replay-with-trial-config.json
```

Der Moduswechsel verwendet denselben
`RuntimeStemRotationPriorityControl`-Vertrag wie die Frontendquelle und führt den
normalen Optimierungspfad mit demselben Modell erneut aus. Er erzeugt keine
Kameramessung und keinen SPS-Schreibzugriff. In der realen HMI erfolgt die Wahl über
den vorhandenen Umschalter; bei `priority_source=PLC` bleibt dieser read-only.

## Diagnosepaket

`--export-dir` schreibt drei kleine Dateien:

- `stem-rotation-diagnostic-package.json`: Modell, Konfiguration, Priority-Mode,
  Versionen, Qualitätsdaten, Ergebnis, Scorekurve und das produktive HMI-ViewModel;
- `stem-slices.csv`: verarbeitete Scheiben in Maschinen-mm;
- `angle-score-curve.csv`: Winkel, Score und limitierende Position.

Rohvideos werden nicht dupliziert. Falls zur Fehleranalyse erforderlich, bleiben sie
im bestehenden Capture-/Datasetpfad und werden über dessen Referenz und Retention
verwaltet.

## Synthetische Stämme

Der Simulator verlangt entweder `--input` oder ausdrücklich `--synthetic`. Beispiel:

```bash
uv run python scripts/dev/simulate_stem_rotation.py \
  --synthetic curved_ellipse \
  --priority-mode VORSCHNITT_KRITISCH \
  --length-mm 4000 \
  --slice-spacing-mm 50 \
  --diameter-start-mm 240 \
  --axis-ratio 0.75 \
  --ellipse-angle-deg 25 \
  --curvature-amplitude-mm 35 \
  --curvature-direction-deg 90 \
  --negative-limit-deg -70 \
  --positive-limit-deg 80 \
  --seed 20260821 \
  --output simulator-report.json \
  --export-dir simulator-package
```

Verfügbare Fälle sind:

- `straight_round`, `curved_round`, `centered_ellipse`, `curved_ellipse`;
- `varying_ovality_angle`, `varying_curvature_direction`, `taper`;
- `local_bottleneck`.

Zusätzlich lassen sich Mittelpunkt-, Halbachsen- und Winkelrauschen, Ausreißer sowie
kurze und lange Datenlücken in mm, Grad und Scheibenanzahl setzen. Derselbe Seed
erzeugt denselben Eingang. Der Generator erzeugt ausschließlich kanonische
Scheibengeometrie; Rasterung, Glättung, Score und Optimierung bleiben produktive APIs.

## Scorekurve interpretieren

Die Diagnosekurve zeigt `angle_deg -> score_mm` für die priorisierte Achse. Der Score
ist das Minimum der symmetrisch nutzbaren Geometrie über alle gültigen Scheiben.

- `0°` ist die gemessene aktuelle Lage.
- Der markierte Kandidat ist das ausgewählte Optimum innerhalb der mechanischen
  Grenzen.
- Eine breite, flache Spitze ist ein Plateau. Innerhalb von
  `score_tolerance_mm` wird absichtlich der kleinere absolute Drehweg bevorzugt.
- `gain_mm` ist die Differenz zwischen ausgewähltem und aktuellem Stammscore, nicht
  die größte lokale Verbesserung einer einzelnen Scheibe.
- Die `DeltaG`-Längskurve im HMI-ViewModel wird vom Backend geliefert und im Simulator
  nicht neu berechnet.

`score_tolerance_mm` ist damit ein Stellweg-/Score-Tie-Breaker und kein Messfehler.
`min_gain_mm` ist die fachliche Totzone für den globalen Geometriegewinn. Beide Werte
müssen anhand realer Wiederholmessungen beurteilt werden: zuerst Messstreuung und
Plateauform bestimmen, danach Totzone und Toleranz so wählen, dass kleine Änderungen
nicht zu wechselnden Stellbefehlen führen.

## Monte-Carlo-Robustheit

```bash
uv run python scripts/dev/analyze_stem_rotation_robustness.py \
  --input result.json \
  --iterations 200 \
  --center-noise-std-mm 1.0 \
  --axis-noise-std-mm 1.5 \
  --angle-noise-std-deg 1.0 \
  --output robustness.json
```

Ausgegeben werden Verteilungen von Winkel und Gain, der Anteil
`should_rotate`, ungültige Resultate sowie Häufigkeit und Wechselrate der limitierenden
Scheibe. Das Tool stört die gespeicherten Scheiben deterministisch und ruft pro
Realisierung den produktiven Runtimepfad auf. Die Rauschwerte müssen aus realen
Wiederholmessungen abgeleitet werden; die CLI-Defaults sind nur eine Entwicklerprobe.

Beim Referenzlauf vom 21. August 2026 mit dem synthetischen kombinierten Stamm und 100
Realisierungen waren alle Ergebnisse gültig und `should_rotate=true`. Der Winkel lag
zwischen 76,4° und 80,6° bei einer Standardabweichung von rund 0,78°; `gain_mm` lag
zwischen 92,25 mm und 103,74 mm. Die limitierende Scheibe wechselte in 90 % der Fälle.
Damit war die Entscheidung stabil, die exakte Markierung der einzelnen limitierenden
Scheibe aber rauschempfindlich. Bei realen Stämmen sind daher zusätzlich eine
limitierende Region und benachbarte Scheiben zu beurteilen.

## Parametersweep

```bash
uv run python scripts/dev/sweep_stem_rotation_parameters.py \
  --input result.json \
  --output parameter-sweep.json
```

Ohne `--sweep` werden jeweils einzeln variiert:

- `slice_spacing_mm`: 25 / 50 / 100;
- `smoothing_window_slices`: 1 / 3 / 5;
- Grobschritt: 0,5° / 1° / 2°;
- Feinschritt: 0,1° / 0,25° / 0,5°;
- `score_tolerance_mm`: 0,25 / 1 / 2;
- `min_gain_mm`: 1 / 3 / 5;
- `min_rotation_deg`: 0,5° / 1° / 2°.

Eigene Reihen lassen sich beispielsweise mit
`--sweep min_gain_mm=2,3,4 --sweep score_tolerance_mm=0.5,1.0` angeben. Es handelt sich
bewusst um Einzelfaktorvergleiche und nicht um automatische Parameteroptimierung.

Bei neu gespeicherten Optimierungsergebnissen verwendet ein Raster-/Glättungssweep die
mitgeführten Rohscheiben vor dem Preprocessing. Bei älteren Resultaten ohne `raw_slices` fällt
das Werkzeug rückwärtskompatibel auf die verarbeiteten Scheiben zurück und meldet
`PREPROCESSING_SWEEP_USES_STORED_PROCESSED_SLICES_AS_INPUT`. Für eine vollständige erneute
Fitbewertung bleiben `stem.projection-observations.v1` beziehungsweise explizit aktivierte
Rohbeobachtungen die bessere Quelle.

## Performance

```bash
uv run python scripts/benchmark/stem_rotation_optimizer_benchmark.py
```

Der Benchmark misst den vollständigen produktiven Priority-/Runtime-/Optimizer-/HMI-
Pfad, nicht nur eine Ersatzformel. Der lokale Offline-Lauf vom 21. August 2026 ergab bei
1° Grob-, 0,1° Feinsuche und ±90° Grenzen folgende Medianwerte:

| Stammlänge | Raster | Scheiben | Median |
|---:|---:|---:|---:|
| 3 m | 25 mm | 121 | 245,6 ms |
| 3 m | 50 mm | 61 | 125,9 ms |
| 3 m | 100 mm | 31 | 66,1 ms |
| 6 m | 25 mm | 241 | 480,7 ms |
| 6 m | 50 mm | 121 | 240,7 ms |
| 6 m | 100 mm | 61 | 125,4 ms |

Diese Werte sind Entwicklungsrechner-Messungen, keine Zielserver-SLA. Der Benchmark
setzt keinen automatischen Grenzwert. Zielservermessungen unter realer Parallel- und
Lastsituation bleiben Teil der Inbetriebnahme.

## Versionierte Regressionen

Die erste kleine Sammlung liegt in
`tests/fixtures/stem_rotation_validation/reference_cases.v1.json`. Sie definiert
Eingang, Priority-Mode, relevante Konfiguration und tolerante Bereiche für Winkel,
Gain sowie limitierende Regionen. Abgedeckt sind Kreis, vertikal und horizontal
priorisierte Ellipse, gekrümmter Kreis und ein Bogen-/Ovalitätskompromiss. Plateaus
werden absichtlich nicht mit einem einzigen exakten Winkel festgeschrieben.

Ein problematischer realer Stamm wird wie folgt gesichert:

1. Result-History-Resultat und, falls verfügbar, kalibrierte
   Projektionsbeobachtungen exportieren.
2. Replay mit unveränderter Konfiguration ausführen und deterministische
   Übereinstimmung prüfen.
3. Diagnosepaket erzeugen und fachlich bewerten; Blickrichtung, positive Drehung,
   Priority-Mode, Modellqualität und mechanische Grenzen mit sichern.
4. Erwartete Winkel-, Gain- und limitierende Regionsbereiche unabhängig festlegen.
   Erwartungswerte nicht lediglich aus dem aktuellen Code kopieren.
5. Kleine pseudonymisierte JSON-/CSV-Artefakte nach Review versionieren. Große Videos
   über den bestehenden Dataset-/Capturepfad referenzieren.
6. Den Fall zu den Regressionstests hinzufügen und unter beiden Prioritätsmodi prüfen,
   sofern fachlich sinnvoll.

## Vorgeschlagene Startwerte

Diese Werte entsprechen den bisherigen Repository-Defaults und sind nur Startwerte
für die reale Inbetriebnahme, keine validierten Produktionswerte:

| Parameter | Startwert |
|---|---:|
| `slice_spacing_mm` | 50 mm |
| `smoothing_window_slices` | 3 |
| `coarse_angle_step_deg` | 1,0° |
| `fine_angle_step_deg` | 0,1° |
| `fine_search_half_width_deg` | 2,0° |
| `score_tolerance_mm` | 1,0 mm |
| `min_gain_mm` | 3,0 mm |
| `min_rotation_deg` | 1,0° |
| `max_interpolation_gap_mm` | 100 mm |
| Drehgrenzen | -90° / +90° |

Vor Freigabe sind mindestens gerade, stark gebogene, ovale, konische, lokal verengte
und grenzwertig sichtbare reale Stämme mehrfach aufzunehmen. Die finalen Drehgrenzen
müssen aus der realen Mechanik kommen. SPS-Schreibzugriffe bleiben in dieser Phase
deaktiviert; Resultate sind Empfehlung beziehungsweise Shadow-Ergebnis.

## Bekannte Grenzen

- Es gibt noch keinen eingecheckten realen Referenzstamm; synthetische Abnahme ersetzt
  keine Zielservervalidierung.
- Scheibenrauschen im Monte-Carlo-Lauf approximiert die Messstreuung, ersetzt aber kein
  erneutes Segmentieren realer Frames. Rohbeobachtungs-Replay ist dafür vorzuziehen.
- Die exakte limitierende Scheiben-ID kann bei nahezu gleich schlechten Nachbarscheiben
  springen, obwohl Winkel und Gain stabil bleiben.
- Ein neues `stem.rotation-optimization.v1` enthält Roh- und verarbeitete Scheiben sowie
  optional Rohbeobachtungen, aber keine vollständige Rohvideosequenz. Perspektiv-, Masken- und
  Winkelspurfehler müssen weiterhin mit den bestehenden Capture-/Messdiagnosen untersucht
  werden.
- Die gemessenen Laufzeiten stammen nicht vom Produktionsserver und berücksichtigen
  keine konkurrierende Kamera-/Inferenzlast.
