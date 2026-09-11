# Codex-Prompts – Produktivsystem Stammausrichtung nach dem Krümmungsminimum

## Verwendung und Vorrang

Lege die Datei

`docs/stem-orientation/Spezifikation_Produktivsystem_Stammbogen_Minimumprinzip.md`

im aktuellen Repository ab.

Ergänze in der vorhandenen `AGENTS.md`, dass diese Spezifikation bei widersprüchlichen älteren Stammausrichtungsdokumenten Vorrang hat.

Die Prompts gelten für den bereits weiterentwickelten aktuellen Code. Die ursprünglich analysierte ZIP ist älter. Codex muss daher in jedem Schritt zuerst die real vorhandenen Dateien, Nodes, Verträge und Migrationen prüfen.

Führe die Prompts einzeln aus.

---

# Prompt 06C – Migrationsaudit vom bisherigen Vertikalsolver zum Minimumprinzip

```markdown
# Aufgabe: Aktuellen Stammausrichtungsstand analysieren und auf das Krümmungsminimum-Prinzip migrieren

Ändere in diesem Prompt keinen Produktivcode.

Lies zuerst:

- `docs/stem-orientation/Spezifikation_Produktivsystem_Stammbogen_Minimumprinzip.md`
- `AGENTS.md`
- alle vorhandenen Stammausrichtungsdokumente

Suche im gesamten Repository insbesondere nach:

- `stem`
- `bow`
- `curvature`
- `orientation`
- `vertical`
- `target_frame`
- `target_phase`
- `surface_rotation`
- `surface_phase`
- `temporal`
- `decision_gate`
- `good`
- `acceptable`
- `bad`
- `occlusion`
- `fragment`

Analysiere mindestens:

- `backend/packages/cv-runtime/src/cv_runtime/context.py`
- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`
- `backend/packages/cv-runtime/src/cv_runtime/handlers.py`
- vorhandene fachliche Dateien unter
  `backend/packages/cv-runtime/src/cv_runtime/nodes/`
- `backend/services/realtime-executor/src/realtime_executor/live.py`
- Pipeline Designer
- Runtime Preview
- Result History
- aktuelle Kalibrierungs-UI
- aktuelle Label-Studio-Integration
- aktuelle Tests und Migrationen

## Verbindliche Zielarchitektur

1. Keine 3D-Rekonstruktion.
2. Anlagenkoordinaten X/Y/Z werden kalibriert, aber nur die horizontale projektive Y-Krümmung wird gemessen.
3. Während des Einwurfs werden mehrere Rollstellungen beobachtet.
4. Pro Frame wird horizontale Krümmung berechnet.
5. Mantelflächenmerkmale liefern die kumulative Rollphase.
6. Eine periodische Kurve `Krümmung über Rollphase` liefert Minimum und Maximum.
7. Nach dem Aufliegen wird die aktuelle Phase bestimmt.
8. Am Decision Gate entsteht der signierte Winkel zurück zum Minimum.
9. Keine subjektive Auswahl eines richtigen Frames.
10. Keine Krümmungsrekonstruktion aus stark verdeckten Fragmenten.
11. TCN/GRU ist nicht Voraussetzung für V1.

## Erstelle

`docs/stem-orientation/20-curvature-minimum-migration-audit.md`

Mit:

### Ist-Inventar

- Dateipfad
- Symbol/Node
- Ein-/Ausgang
- State
- UI
- Tests

### Keep / Change / Deprecate / Remove Later

Bewerte insbesondere:

- direkte Vertikalpfeil-Logik,
- manuelles Zielintervall,
- `good / acceptable / bad` als Winkelziel,
- bisheriges Rotationstracking,
- Fragment-Merging,
- bestehende Resultverträge,
- bestehende Gates und Kalibrierungen.

### Konkrete Ziel-Dateikarte

Für Prompts 07A bis 07G:

- geänderte Dateien,
- neue Dateien,
- Migrationen,
- Tests,
- Rückwärtskompatibilität.

### Offene Punkte

Nur echte Punkte, die der Code nicht auflöst.

## Regeln

- keine Codeänderung,
- keine zweite Anwendung,
- keine erfundenen Komponenten,
- reale Dateipfade und Symbole nennen,
- keine ML-Plattform planen,
- nach dem Audit beenden.
```

---

# Prompt 07A – Anlagenkoordinaten, Referenzebene und Prozessgates

```markdown
# Aufgabe: Kalibriertes X/Y-Prozesskoordinatensystem und Messgates implementieren

Nutze Prompt 06C.

## Ziel

Erweitere den bestehenden Kalibrierungsweg um:

- X-Richtung entlang Längsförderer,
- Y-Richtung horizontal quer,
- dokumentierte Z-Richtung,
- Referenzpunkte/Polygon mit bekannten X/Y-Werten,
- Homographie beziehungsweise bestehende passende Transformation,
- Referenzebene,
- Candidate ROI,
- Curvature Observation ROI,
- Surface Tracking ROI,
- Session Start Gate,
- Curvature Freeze Gate,
- Decision Gate,
- Decision Margin,
- Drehrichtungskonvention.

Alle Geometrien gelten im intrinsisch entzerrten Bild.

## Vorher lesen

- vorhandener `camera_undistort`-Node
- vorhandene Kalibrierungsschemas
- Pipeline Registry
- Pipeline Designer
- grafische Property-Editoren für ROI, Zonen und Richtungen
- aktueller Stammcode

## Implementiere

### Vertrag

Ein versioniertes Schema, fachlich etwa:

`StemCurvatureProcessCalibration`

Mit mindestens:

- ID und Schema-Version
- Bildauflösung
- Intrinsic-Calibration-Referenz
- X/Y-Referenzpunkte
- Homographiematrix oder reproduzierbare Ableitung
- Referenzebene
- ROI- und Gate-Geometrien
- Conveyor Direction
- Rotation Reference View
- Positive Rotation
- optionale Durchmesser-/Höhenklasse
- Reprojektionsfehler
- Provenienz

### UI

Auf einem entzerrten Standbild oder Livebild:

- Referenzpunkte anklicken und metrische X/Y-Werte eingeben,
- X/Y-Achsen anzeigen,
- ROIs und Gates zeichnen,
- Blickrichtung und positive Drehung festlegen,
- Kalibrierung validieren und versioniert speichern.

### Runtime

Erzeuge eine zentrale Utility/API, die:

- Bildpunkte nach X/Y abbildet,
- Masken oder Konturpunkte in den rectified Raum transformiert,
- Gates in beiden Koordinatensystemen prüft.

### Validierung

Fail closed bei:

- Auflösungsmismatch,
- schlechtem Reprojektionsfehler,
- degenerierter Homographie,
- falscher Gate-Reihenfolge,
- leerem Messbereich,
- verschobener Kamera.

## Nicht implementieren

- noch keine Krümmungsformel,
- kein Phasentracking,
- kein Solver,
- keine SPS.

## Tests

- bekannte Punktabbildungen,
- Homographie-Roundtrip,
- Gate-Reihenfolge,
- normalisierte Koordinaten,
- Serialisierung,
- Frontendeditierung,
- Pipeline speichern/laden.

## Akzeptanz

Ein Benutzer kann das Anlagenkoordinatensystem und alle Messphasen in der bestehenden Anwendung reproduzierbar konfigurieren.
```

---

# Prompt 07B – Robuste horizontale Krümmung pro Frame

```markdown
# Aufgabe: `analysis.stem_horizontal_curvature` implementieren

## Ziel

Berechne aus der aktiven Stammaske die horizontal projizierte Krümmung im kalibrierten X/Y-Raum.

## Eingang

- entzerrter Frame
- aktive Stammmaske
- Track-ID
- Prozesskalibrierung
- Qualitätsmetadaten

## Algorithmus

1. Maske bereinigen.
2. Maske/Kontur in X/Y-Referenzraum transformieren.
3. Für diskrete X-Stationen linke und rechte Y-Grenze bestimmen.
4. Mitte:
   `y_center = (y_left + y_right) / 2`
5. Breitenprofil bilden.
6. Ausreißer und Astwülste robust entfernen.
7. Mittellinie glätten.
8. Referenzlinie bestimmen.
9. Residualprofil:
   `r(x) = y_center(x) - y_reference(x)`
10. Krümmungskennwerte berechnen.

## Verbindliche Ausgänge

- `signed_bow_mm`
- `curvature_rms_mm`
- `curvature_maxabs_mm`
- `curvature_peak_to_peak_mm`
- normalisierte Werte
- `centerline_profile`
- `width_profile`
- `visible_length_ratio`
- `common_segment_descriptor`
- `geometry_confidence`
- `bow_shape_hint`
- Kalibrierungs- und Algorithmusversion

## Vergleichbarer Stammabschnitt

Implementiere eine klare Strategie:

- bevorzugt vollständige beziehungsweise ausreichend große sichtbare Stammstrecke,
- definierter innerer Längsanteil,
- oder über den Track mitgeführter physischer Abschnitt.

Krümmungswerte nicht vergleichen, wenn die gemeinsame Abdeckung unter der Grenze liegt.

## Gültigkeitsbereich

- nur im Curvature Observation ROI,
- keine neuen Geometriebeobachtungen nach Curvature Freeze Gate,
- keine Fragmentrekonstruktion in der Maschine.

## Overlay

- rectified Achsen,
- Mittellinie,
- Referenzlinie,
- Residualprofil,
- aktuelle Krümmungswerte,
- Sichtbarkeits- und Confidencewerte.

## Tests

Synthetische Masken:

- gerade,
- einfacher Bogen positiv/negativ,
- unterschiedliche Schräglage,
- Translation,
- Perspektivtransformation,
- Astwülste,
- S-Bogen,
- unterschiedliche sichtbare Längen,
- abgeschnittene Enden,
- Rauschen.

## Akzeptanz

Für denselben synthetischen Stamm bleibt die berechnete horizontale Krümmung nach zulässiger Translation und Schräglage stabil. Gegenläufige Bögen liefern unterschiedliche Vorzeichen.
```

---

# Prompt 07C – Mantelflächenphase, Re-Lock und Gradkalibrierung

```markdown
# Aufgabe: `analysis.stem_surface_phase` als zustandsbehafteten Node implementieren

## Ziel

Bestimme die tatsächliche Rollphase des aktiven Stammes während Einwurf, Aufprall und Aufliegen.

## Eingang

- entzerrter Frame
- aktive Stammaske
- Track-ID
- Stammachse/Geometrie
- Surface Tracking ROI
- Rotationskalibrierung

## Zwei Trackingpfade

### Inkrementell

- Features in erodierter Stammaske
- optischer Fluss oder Keypointmatching
- globale Translation/Achsänderung kompensieren
- robuste relative Phase
- kumulative Phase integrieren

### Keyframe-Re-Lock

- markante Mantelansichten mit Phase speichern
- nach Aufprall oder Featureverlust aktuelle Ansicht mit Keyframes vergleichen
- nur bei eindeutiger plausibler Übereinstimmung wieder einrasten
- Re-Lock-Ereignis und Confidence speichern

## Gradkalibrierung

Implementiere einen Inbetriebnahmeworkflow mit markiertem Teststamm oder Referenzzylinder.

Speichere versioniert:

- Messpaare rohe Phase ↔ bekannter Winkel
- Fit
- Fehler
- gültigen Bild-/Durchmesserbereich
- Erstellzeit
- Kalibrierungs-ID

Ohne gültige Kalibrierung:

- rohe Phase ausgeben,
- keinen Grad-Korrekturwert zulassen.

## State

- Track-ID
- vorheriger Frameausschnitt
- Featurepunkte
- Keyframes
- kumulative Phase
- letzte sichere Phase
- Re-Lock-Status
- Fehlzähler

Reset bei Trackwechsel, Sessionende oder unplausiblem Sprung.

## Stateful-Integration

Registriere den Node in der vorhandenen Stateful-Liste des Realtime Executors.

## Payload

Mindestens:

- `delta_phase_deg`
- `cumulative_phase_deg`
- rohe Phase
- Richtung
- Trackingmodus
- Re-Lock-Status
- Featureanzahl
- Inlierquote
- Fitfehler
- Confidence
- Kalibrierungs-ID
- Reason Codes

## Tests

- reine Translation,
- bekannte Rotation,
- Kombination,
- abrupter Aufprall,
- kurze Bildlücke,
- Re-Lock zu richtigem Keyframe,
- mehrdeutiger Re-Lock,
- Trackwechsel,
- fehlende Kalibrierung.

## Akzeptanz

Die Phase bleibt über eine komplette Testsequenz stabil oder die Runtime meldet eindeutig `PHASE_TRACK_LOST`; sie erfindet keine Gradwerte.
```

---

# Prompt 07D – Krümmung-über-Phase-Solver

```markdown
# Aufgabe: `analysis.stem_curvature_phase_solver` implementieren

## Ziel

Führe horizontale Krümmungsbeobachtungen und Mantelphase zusammen und bestimme Minimum, Maximum und Zielphase.

## Eingang pro Beobachtung

- Track-ID
- Frame-ID / Timestamp
- cumulative phase
- `signed_bow_mm`
- `curvature_rms_mm`
- weitere Geometriewerte
- Geometrieconfidence
- Phasenconfidence
- gemeinsame Abdeckung
- Position zu Curvature Freeze und Decision Gate

## State

- Session-ID
- aktive Track-ID
- gültige Beobachtungen
- beobachtete Phasenspanne
- Fitparameter
- bisheriges Minimum/Maximum
- Zielphase
- Confidence
- Zustand
- Reason-Code-Historie

## Solver

### Signed Fit

Robust gewichtet:

`B(phi) = b0 + b1*cos(phi) + b2*sin(phi)`

### Energy Fit

Robust gewichtet:

`K(phi)^2 = c0 + c1*cos(2*phi) + c2*sin(2*phi)`

Die Zielphase minimiert K über 0–180°.

### Ausgänge

- beobachtetes Min/Max
- gefittetes Min/Max
- `curvature_span`
- Zielphase
- Maximalphase
- Fitresiduum
- Ziel beobachtet/extrapoliert
- Eindeutigkeit des Minimums
- Bogenrelevanz

## Regeln

- nach Curvature Freeze Gate keine neuen Krümmungspunkte aufnehmen,
- Phase darf weiter aktualisiert werden,
- Minimum nur bei genügend Phasenspanne und stabilem Fit,
- kein Winkel bei flacher/mehrdeutiger Kurve,
- S-Bogen mit Restminimum kennzeichnen,
- nahezu gerader Stamm → `BOW_NOT_RELEVANT`.

## Tests

- bekannte periodische Kurven,
- positive/negative Phasen,
- modulo 180°,
- Teilbeobachtung,
- beobachtetes Minimum,
- zulässige Extrapolation,
- unzureichende Rotation,
- Ausreißer,
- S-Bogen-Energie,
- flaches Minimum.

## Akzeptanz

Aus einer gültigen Sequenz entstehen reproduzierbar Minimalphase, Maximalphase sowie Min-/Max-Krümmung mit Confidence.
```

---

# Prompt 07E – Sessionablauf, Decision Gate und Winkelresultat

```markdown
# Aufgabe: Produktiven Sessionzustandsautomaten und Winkelresultat implementieren

## Ziel

Verbinde Active Stem, Krümmungsnode, Phasentracker und Solver zu einem vollständigen Vor-Einzug-Ablauf.

## Zustände

- `WAITING`
- `CANDIDATE`
- `OBSERVING_CURVATURE`
- `TRACKING_PHASE`
- `READY_TO_DECIDE`
- `RESULT_READY`
- optional `TRACKING_TO_TARGET`
- `DONE`

## Ablauf

### Sessionstart

Nur bei eindeutigem Track und Mindestqualität.

### Curvature Freeze

- Krümmungsmodell einfrieren,
- Zielphase behalten,
- Phase weiterverfolgen.

### Aufliegen

Erkenne stabile oder ausreichend sicher verfolgte aktuelle Phase.

### Decision Gate

Berechne:

`delta = wrap_to_minus_90_plus_90(target_phase - current_phase)`

Erzeuge:

- `ROTATE`
- `NO_ROTATION`
- `UNCERTAIN`

## Resultvertrag

Ermittle die nächste freie Vertragsversion im aktuellen Repository.

Felder mindestens gemäß Spezifikation:

- Korrekturwinkel
- Richtung
- Min/Max/Span der Krümmung
- Ziel-/Aktuellphase
- Rotationsspanne
- Target observed
- Re-Lock
- Confidences
- Kalibrierungs-IDs
- Reason Codes
- Algorithmus `CURVATURE_PHASE_MINIMUM`

Alte Verträge rückwärtskompatibel behandeln.

## Deadline

Das Ergebnis muss vor dem ersten Walzenpaar bereitstehen.

Speichere:

- Decision Timestamp
- verbleibende Zeit/Distanz, falls verfügbar
- Result Expiry

## Tests

- normaler Ablauf,
- kein relevanter Bogen,
- unzureichende Rotation,
- Trackwechsel,
- Phase nach Aufprall wieder eingerastet,
- Decision Gate,
- Frames hinter Gate ignoriert,
- Pipeline Reset,
- zwei Stämme nacheinander.

## Akzeptanz

Jede Session endet mit einem nachvollziehbaren Winkel, `NO_ROTATION` oder `UNCERTAIN`. Kein Zustand bleibt unkontrolliert zwischen zwei Stämmen erhalten.
```

---

# Prompt 07F – Diagnoseoverlay und Sessionaufzeichnung

```markdown
# Aufgabe: Produktivsystem transparent diagnostizierbar machen

## Ziel

Integriere die neue Methode in Livepreview, Result History und optionale Sessionaufzeichnung.

## Overlay

Zeige:

- Prozess-ROIs und Gates
- X-/Y-Achsen
- aktive Stamm-ID
- Mittellinie und Referenzlinie
- aktuelles Residualprofil
- aktuelle Krümmung
- Mantelphase
- beobachtete Phasenspanne
- gefittete Min-/Max-Kurve
- Zielphase
- aktuelle Phase
- Korrekturwinkel
- Confidence
- Reason Codes

## Diagnosegraph

X-Achse:

`kumulative Rollphase in Grad`

Y-Achse:

`horizontale Krümmung in mm`

Zeige:

- Messpunkte,
- gewichtete/unberücksichtigte Punkte,
- Fit,
- Minimum,
- Maximum,
- aktuelle Phase,
- Decision Phase.

## Sessionartefakte

Optional und konfigurierbar:

- entzerrtes Video
- Overlayvideo
- `observations.jsonl`
- `phase_trace.json`
- `fit.json`
- finaler Resultvertrag
- Kalibrierungs-IDs

## Label Studio

Keine neue subjektive Ziel-Frame-Annotation einführen.

Die bestehende Standard-Label-Studio-Funktion bleibt für Stammsegmentierung.

Ein optionales technisches Sequenzreview darf nur Plausibilität/Fehlerursachen erfassen.

## Tests

- Resultserialisierung
- unsichere Session
- zwei Sessions
- Overlay ohne Fit
- Re-Lock
- Frontendgraph
- Speicherbegrenzung und Cleanup

## Akzeptanz

Ein Entwickler kann bei jeder Entscheidung nachvollziehen, welche Krümmungspunkte und Phasen zum Ergebnis geführt haben.
```

---

# Prompt 08 – Referenzkörper, Kalibrierung und objektive Abnahme

```markdown
# Aufgabe: Objektive Kalibrierungs- und Validierungswerkzeuge implementieren

## Ziel

Validiere das Minimumprinzip ohne subjektive Ziel-Frames.

## Referenzaufbauten

### A. Gerader Referenzzylinder

Prüft:

- Homographie,
- Nullkrümmung,
- Translation,
- Phasentracking.

### B. Gebogener Referenzkörper

Mit bekannter Krümmung und Umfangsmarkierungen.

Prüft:

- Krümmungs-Minimum,
- Krümmungs-Maximum,
- Zielphase,
- Korrekturwinkel.

### C. Markierte Teststämme

Prüfen reale Rinde, Durchmesser und Licht.

## Kalibrierungsassistent

Erfasse:

- bekannte Winkelstände,
- tatsächliche Markierung,
- Bildposition,
- Durchmesserklasse,
- berechnete Phase,
- Fehler.

Erzeuge versionierte Kalibrierungsartefakte.

## Bericht

JSON und HTML/Markdown:

- Median/P90/P95 Winkelfehler
- Vorzeichenfehler
- Min-/Max-Krümmungswiederholbarkeit
- Fehler nach Phasenspanne
- Fehler nach Durchmesser
- Fehler nach Bildposition
- Re-Lock-Erfolg
- Unsicherheitsquote

## Abnahmestatus

- `VALIDATION_PASSED`
- `VALIDATION_FAILED`
- `INSUFFICIENT_DATA`

## Akzeptanz

Die Ein-Kamera-Methode besitzt messbare, reproduzierbare Genauigkeitskennwerte und kann bei schlechter Beobachtbarkeit fail closed arbeiten.
```

---

# Prompt 09 – Shadow Mode und Produktionsbewertung

```markdown
# Aufgabe: Shadow-Mode für reale Stämme produktionsnah betreiben

## Ziel

Sammle und bewerte reale Sessions ohne SPS-Schreibzugriff.

## Implementiere

- Aktivierung pro Pipelineversion
- automatische Sessionstatistik
- Winkelverteilung
- NO_ROTATION/UNCERTAIN-Anteil
- Confidenceverteilung
- Reason-Code-Verteilung
- Laufzeit bis Decision Result
- Kamera-/Kalibrierungsdrift
- Export eines Bewertungsberichts

## Manuelles Feedback

Optional darf ein Bediener nur erfassen:

- plausibel
- unplausibel
- unsicher
- technischer Fehlergrund

Kein manuelles Ziel-Frame und kein geschätzter Winkel als Ground Truth.

## Akzeptanz

Mehrere hundert Sessions können ohne Speicher- oder Zustandsprobleme ausgewertet werden. Fehlfälle sind kategorisierbar.
```

---

# Prompt 10 – SPS-Dry-Run und optionale Phasenregelung beim Drehen

```markdown
# Aufgabe: Winkelresultat sicher an den vorhandenen SPS-Pfad anbinden

## Voraussetzung

- Referenzvalidierung bestanden,
- Shadow Mode fachlich freigegeben.

## Ziel

Nutze ausschließlich den vorhandenen Siemens-SPS-Ausgabepfad.

## Dry-Run-Vertrag

- Session-/Track-ID
- signierter Sollwinkel
- Richtung
- Zielphase
- aktuelle Phase
- Confidence
- Gültigkeitsdauer
- Kalibrierungs-/Algorithmusversion
- Reason Codes

## Freigabegates

Kein Befehl bei:

- `UNCERTAIN`
- Result abgelaufen
- Trackwechsel
- Kalibrierung ungültig
- Confidence zu gering
- Winkel außerhalb Prozessgrenze
- Validierung nicht bestanden
- Runtimefehler

## Optionale geschlossene Phasenregelung

Falls nach dem Greifen ausreichend Mantelfläche sichtbar ist:

- Phasentracker weiterverwenden,
- keine neue Krümmung berechnen,
- Drehung stoppen, sobald Zielphase erreicht ist,
- maximale Drehzeit und maximaler Winkel SPS-seitig begrenzen.

## Nicht zulässig

- Vision als Sicherheitssteuerung,
- automatische Freigabe ohne SPS-Grenzen,
- offene Suche über viele Testimpulse.

## Akzeptanz

Im Dry Run ist klar, wann und welcher Hauptdrehbefehl bereitgestanden wäre. Optionale Phasenregelung ist separat aktivierbar.
```

---

# Prompt 11 – Installer, Update und Produktionshärtung

```markdown
# Aufgabe: Minimumprinzip über den vorhandenen Windows-Updatepfad produktionsreif ausrollen

## Prüfe

- Python-/OpenCV-Abhängigkeiten
- Runtime Node Registry
- Stateful Node Liste
- Payload- und Resultverträge
- Datenbankmigrationen
- Frontendbuild
- Kalibrierungsartefakte
- Backup/Rollback
- Session Cleanup
- Offlinebetrieb
- Kamera Reconnect
- Pipeline Restart
- Result Expiry

## Rückwärtskompatibilität

- ältere Pipelines laufen weiter,
- alte Stammausrichtungsverträge nicht stillschweigend umdeuten,
- klare Migration zur neuen Algorithmusvariante,
- fehlende neue Kalibrierung führt fail closed,
- keine SPS-Schreibfunktion automatisch aktivieren.

## Performance

Auf der RTX 4500 Ada und Windows-11-Zielumgebung messen:

- Segmentierungszeit
- Geometriezeit
- Phasentrackingzeit
- Solverzeit
- End-to-End-Latenz
- Speicherauslastung
- Langzeitstabilität

## Dokumentation

- Kamera- und Prozesskalibrierung
- Rotationskalibrierung
- Messzonen
- Shadow Mode
- Validierungsbericht
- Reason Codes
- SPS-Dry-Run
- Rollback

## Akzeptanz

Die Funktion ist als versioniertes Update installierbar, rückrollbar und beeinträchtigt bestehende Inferenzserver-Funktionen nicht.
```

---

# Optionale spätere Erweiterung – Temporal-KI

Ein TCN oder eine GRU wird erst erwogen, wenn die deterministische Methode im Shadow Mode konkrete systematische Grenzen zeigt.

Das Modell erhält strukturierte Folgen aus:

- Krümmungsprofilen,
- Phasenspuren,
- Qualitätswerten,
- Trackzuständen.

Es ersetzt nicht:

- Kalibrierung,
- Phasenmessung,
- Fail-closed-Gates,
- Decision Gate,
- objektive Referenzvalidierung.

Ein Video-Transformer ist keine V1-Anforderung.
