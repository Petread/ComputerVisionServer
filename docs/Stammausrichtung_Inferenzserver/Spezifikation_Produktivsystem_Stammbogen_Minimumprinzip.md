# Spezifikation – Produktivsystem Stammausrichtung nach dem Krümmungsminimum

## 0. Status und Vorrang

Dieses Dokument beschreibt die verbindliche Zielarchitektur für das Produktivsystem zur Stammausrichtung im bestehenden **Inferenz Server**.

Es ersetzt für diesen Anwendungsfall ältere Ansätze, die primär auf folgenden Annahmen beruhten:

- direkte 3D-Rekonstruktion der Stammachse,
- manuelle Auswahl eines „richtigen Frames“,
- `good / acceptable / bad` als primäres Winkel-Trainingsziel,
- direkte Ableitung einer räumlichen Bogenrichtung aus einem einzelnen schrägen Bild,
- Suche der richtigen Stellung erst durch wiederholte Testdrehungen im ersten Walzenpaar,
- Rekonstruktion stark verdeckter Stammfragmente innerhalb der Maschine.

Die produktive Hauptentscheidung wird **vor dem ersten Vorzentrierungs-Walzenpaar** aus der Einwurfsequenz berechnet.

---

# 1. Fachliches Ziel

Der Stamm wird vom Querförderer in den Längsförderer eingeworfen. Dabei rollt er mehr oder weniger stark um seine Längsachse.

Eine fest montierte schräge Kamera beobachtet den Stamm:

1. vor dem Einwurf,
2. während des Einwurfs und Rollens,
3. nach dem Aufliegen im Längsförderer,
4. bis zu einer definierten Entscheidungslinie vor der starken Maschinenverdeckung.

Während der beobachteten Rotation wird fortlaufend die **horizontal projizierte Stammkrümmung** berechnet.

Die physische Stammkrümmung selbst verändert sich beim Rollen nicht. Es verändert sich ihre horizontale Projektion:

- Bogen liegt horizontal: große sichtbare Seitenkrümmung,
- Bogen steht schräg: mittlere Seitenkrümmung,
- Bogenebene steht vertikal: minimale Seitenkrümmung.

Das System bestimmt daher:

- maximale beobachtete beziehungsweise gefittete horizontale Krümmung,
- minimale beobachtete beziehungsweise gefittete horizontale Krümmung,
- Rollphase des Minimums,
- aktuelle Rollphase nach dem Aufliegen,
- kürzeste Korrekturdrehung zurück zum Minimum.

Beispielausgabe:

```json
{
  "decision": "ROTATE",
  "recommended_rotation_deg_signed": -43.5,
  "recommended_rotation_deg_abs": 43.5,
  "direction": "COUNTER_CLOCKWISE",
  "curvature_min_mm": 4.2,
  "curvature_max_mm": 37.8,
  "curvature_span_mm": 33.6,
  "target_phase_deg": 71.0,
  "current_phase_deg": 114.5,
  "confidence": 0.88
}
```

---

# 2. Wichtige geometrische Klarstellung

## 2.1 Anlagenkoordinatensystem

Nach intrinsischer Entzerrung und Prozesskalibrierung wird ein Anlagenkoordinatensystem definiert:

- **X:** Längsrichtung des Längsförderers beziehungsweise des Stammes,
- **Y:** horizontale Querrichtung,
- **Z:** physische Vertikale.

Die Kamera bleibt trotzdem eine 2D-Kamera.

Die Kalibrierung liefert:

- Projektion der Anlagenachsen im Bild,
- metrische Abbildung auf eine gewählte Referenzebene,
- konsistente Prozesszonen und Messrichtungen,
- eine reproduzierbare horizontale Y-Messrichtung.

Sie liefert **keine eindeutige XYZ-Koordinate für jeden beliebigen Bildpixel**, weil keine Tiefe gemessen wird.

Für dieses Produktivsystem ist das nicht erforderlich. Gemessen wird nur die kalibrierte beziehungsweise projektive horizontale Krümmung in Y-Richtung über die Rollphase.

## 2.2 Referenzebene

Die Prozesskalibrierung verwendet eine definierte Referenzebene, bevorzugt nahe der mittleren Stammachsenhöhe im Beobachtungsbereich.

Mögliche Ausbaustufen:

- eine feste Referenzebene,
- Referenzebene je Durchmesserklasse,
- empirische Höhen-/Durchmesserkorrektur.

Alle ausgegebenen Millimeterwerte sind deshalb zunächst als:

```text
kalibrierte projektive horizontale Krümmung
```

zu verstehen.

Die Orientierung zum Minimum ist die primäre Prozessgröße. Die absolute Krümmungsangabe muss separat mit Referenzkörpern validiert werden.

---

# 3. Produktprinzip

## 3.1 Zwei getrennte Informationen

Das System kombiniert zwei voneinander getrennte Signale.

### A. Geometriesignal

Aus der Stammmaske pro Frame:

```text
Wie groß ist die horizontale Seitenkrümmung in dieser Rollstellung?
```

### B. Mantelflächensignal

Aus Rinde, Ästen, Rissen und Oberflächenmerkmalen:

```text
Wie weit und in welche Richtung hat sich der Stamm seit der vorherigen Beobachtung gedreht?
```

Die Geometrie bestimmt die Zielphase. Die Mantelflächenanalyse bestimmt die aktuelle Phase und die Drehrichtung.

## 3.2 Ablauf

```text
Stammkandidat erkannt
        ↓
Session starten
        ↓
Stamm rollt beim Einwurf
        ↓
pro Frame:
  Maske + Mittellinie + horizontale Krümmung
  Mantelmerkmale + relative Rollphase
        ↓
Krümmung über Rollphase fitten
        ↓
Minimum und Maximum bestimmen
        ↓
Stamm liegt im Längsförderer
        ↓
aktuelle Mantelphase bestimmen / wieder einrasten
        ↓
Decision Gate erreicht
        ↓
Korrektur zur Minimalphase berechnen
        ↓
Ergebnis einfrieren
```

---

# 4. Mess- und Prozesszonen

Im entzerrten Bild werden versioniert gespeichert:

- `candidate_roi`
- `curvature_observation_roi`
- `surface_tracking_roi`
- `session_start_gate`
- `curvature_freeze_gate`
- `decision_gate`
- `decision_margin_px`
- `conveyor_direction`
- `process_reference_plane`
- `rotation_reference_view`
- `positive_rotation`

## 4.1 Session Start Gate

Eine Session beginnt, wenn:

- ein eindeutiger Stammkandidat die Startlinie erreicht,
- die aktive Instanz ausgewählt ist,
- ausreichend Stammfläche sichtbar ist,
- Segmentierungs- und Bildqualität Mindestwerte erfüllen.

## 4.2 Curvature Freeze Gate

Ab dieser Linie wird keine neue Gesamtkrümmung mehr in das Kurvenmodell aufgenommen, falls die Stammkontur danach zunehmend verdeckt oder abgeschnitten wird.

Die Mantelflächen- und Phasenverfolgung darf weiterlaufen.

## 4.3 Decision Gate

Spätestens an dieser Linie wird:

- der letzte gültige Phasenstand bestimmt,
- das Minimum der Krümmungskurve festgelegt,
- die Korrekturdrehung berechnet,
- das Ergebnis eingefroren.

Hinter dieser Linie wird keine neue Hauptentscheidung mehr erzeugt.

## 4.4 Keine Fragmentrekonstruktion in V1

V1 implementiert ausdrücklich nicht:

- keine amodale Segmentierung,
- kein Verbinden großer verdeckter Stammfragmente,
- keine Gesamtkrümmung aus wenigen Reststücken innerhalb der Maschine.

Eine kleinere sichtbare Mantelfläche darf optional noch zur Phasenverfolgung während der tatsächlichen Walzendrehung verwendet werden.

---

# 5. Kamerakalibrierung

## 5.1 Intrinsische Kalibrierung

Erforderlich:

- Kameramatrix,
- radiale Verzeichnung,
- tangentiale Verzeichnung,
- Auflösung,
- Kalibrierungsfehler,
- Version und Erstellzeit.

Die Runtime-Reihenfolge lautet:

```text
Rohframe
→ intrinsisch entzerren
→ Prozesskoordinaten anwenden
→ Segmentierung
→ Geometrie und Tracking
```

## 5.2 Prozesskoordinaten

Im entzerrten Bild werden mindestens folgende Referenzen definiert:

- X-Richtung des Längsförderers,
- Y-Richtung quer zum Längsförderer,
- Referenzpolygon beziehungsweise Referenzpunkte mit bekannten X/Y-Koordinaten,
- Referenzebene,
- Z-Richtung als dokumentierte physische Achse,
- Blickrichtung für Uhrzeigersinn/Gegenuhrzeigersinn.

Empfohlen wird eine Homographie:

```text
entzerrtes Bild
→ kalibrierte X/Y-Referenzebene
```

## 5.3 Kalibrierungsvalidierung

Die Kalibrierung ist ungültig bei:

- falscher Bildauflösung,
- zu hohem Reprojektionsfehler,
- fehlender X-/Y-Richtung,
- leerem Messbereich,
- falscher Reihenfolge der Gates,
- verschobener Kamera,
- fehlender Rotationskalibrierung für Gradwerte.

Die Runtime arbeitet fail closed.

---

# 6. Segmentierung und aktiver Stamm

## 6.1 Segmentierungsmodell

Das vorhandene Segmentierungsmodell erkennt alle sichtbaren Stämme innerhalb des relevanten Bildbereichs.

Labelregel:

> In jedem Trainingsbild werden alle sichtbaren Stämme im verwendeten Trainingsbereich als getrennte Instanzen gelabelt.

Nicht gelabelte sichtbare Stämme würden sonst als Hintergrund trainiert.

## 6.2 Active Stem Selector

Der aktive Stamm wird anhand folgender Informationen gewählt:

- Position zu den Prozesszonen,
- Bewegungsrichtung,
- Reihenfolge vom Querförderer zum Einwurf,
- Trackhistorie,
- Größe und Achsrichtung,
- Erscheinungsmerkmale.

Die Auswahl muss auch dann stabil bleiben, wenn mehrere Stämme gleichzeitig sichtbar sind.

## 6.3 Track über den Einwurf

Der Track muss einen sprunghaften Übergang verkraften:

- Beschleunigung,
- Abwurf,
- kurze Verdeckung,
- Rollen,
- Lagewechsel,
- Bewegungsunschärfe.

Verwendbar sind:

- Kalman-/Bewegungsmodell,
- Masken-IoU,
- Position entlang der Förderachse,
- Form- und Längenmerkmale,
- Erscheinungsdeskriptor,
- bekannte Stammreihenfolge.

Ein Trackwechsel führt zu `UNCERTAIN`.

---

# 7. Horizontale Krümmung pro Frame

## 7.1 Rectified Mask

Die aktive Stammaske wird in den kalibrierten X/Y-Raum beziehungsweise in eine lokale Stammansicht überführt.

## 7.2 Mittellinie

Für diskrete X-Positionen:

\[
y_c(x)=\frac{y_\text{left}(x)+y_\text{right}(x)}{2}
\]

Robuste Verarbeitung:

- Maskenmorphologie,
- Ausreißerentfernung,
- Breitenplausibilität,
- Medianfilter,
- geglättete Spline,
- verringerte Gewichtung der Stammenden,
- Unterdrückung kurzer Astwülste.

## 7.3 Referenzlinie

Die globale Schräglage wird entfernt.

Mögliche Referenz:

- robuste Sehne zwischen inneren Endbereichen,
- robuste lineare Regression,
- PCA-Hauptachse mit kontrollierter Endpunktbehandlung.

Die Methode wird versioniert und durch Tests festgelegt.

\[
r(x)=y_c(x)-y_\text{ref}(x)
\]

## 7.4 Krümmungskennwerte

Mindestens:

### Vorzeichenbehaftete Hauptkrümmung

```text
signed_bow_mm
```

Sie beschreibt, auf welche horizontale Seite die dominante Bogenprojektion zeigt.

### RMS-Seitenkrümmung

\[
K_\mathrm{rms}
=
\sqrt{
\frac{\sum_i w_i r_i^2}
{\sum_i w_i}
}
\]

### Maximale Sagitta

\[
K_\mathrm{maxabs}
=
\max_i |r_i|
\]

### Peak-to-Peak

\[
K_\mathrm{p2p}
=
\max_i r_i-\min_i r_i
\]

### Normierte Werte

- Krümmung relativ zur sichtbaren Länge,
- Krümmung relativ zum mittleren Durchmesser.

## 7.5 Vergleichbarer Stammabschnitt

Krümmungswerte verschiedener Frames dürfen nur verglichen werden, wenn sie sich auf einen ausreichend vergleichbaren physischen Stammabschnitt beziehen.

V1 bevorzugt Frames, in denen:

- beide Endbereiche oder ein definierter großer Anteil sichtbar sind,
- die aktive Stammachse nicht den ROI-Rand berührt,
- mindestens ein konfigurierter Längsanteil gemeinsam beobachtet wird.

Alternativ wird ein einmal festgelegter Objektabschnitt über den Track mitgeführt.

## 7.6 Frame Payload

Mindestens:

```json
{
  "track_id": "stem-1842",
  "frame_id": "frame-5012",
  "timestamp_ns": 123456789,
  "signed_bow_mm": -13.4,
  "curvature_rms_mm": 15.1,
  "curvature_maxabs_mm": 28.3,
  "curvature_peak_to_peak_mm": 42.0,
  "curvature_normalized": 0.0081,
  "centerline_profile": [],
  "width_profile": [],
  "visible_length_ratio": 0.89,
  "common_segment_ratio": 0.77,
  "geometry_confidence": 0.92,
  "calibration_id": "..."
}
```

---

# 8. Mantelflächen- und Rollphasentracking

## 8.1 Aufgabe

Der Tracker bestimmt pro Frame:

- relative Rollbewegung,
- kumulative Rollphase,
- Drehrichtung,
- Konfidenz,
- Re-Lock nach dem Aufprall.

## 8.2 Oberflächenmerkmale

Verwendbar:

- Äste und Astansätze,
- Rindenfetzen,
- Risse,
- helle Holzstellen,
- lokale Textur,
- Konturunregelmäßigkeiten.

Die Merkmale werden in einer erodierten Stamminstanz ausgewertet.

## 8.3 Zwei komplementäre Modi

### Inkrementelles Tracking

Für aufeinanderfolgende Frames:

- optischer Fluss,
- lokale Keypointkorrespondenzen,
- robuste Bewegungsschätzung,
- Translation kompensieren,
- Rollphase integrieren.

### Keyframe-Re-Lock

Während der Session werden markante Oberflächenansichten als Keyframes gespeichert.

Nach dem Aufprall oder einem kurzzeitigen Trackverlust:

- aktuelle Oberfläche mit Keyframes vergleichen,
- beste plausible Phasenposition bestimmen,
- nur bei ausreichendem Match wieder einrasten.

## 8.4 Gradkalibrierung

Pixel- oder Deskriptorbewegung wird nicht ungeprüft als Gradwert interpretiert.

Ein markierter Teststamm oder Referenzzylinder mit bekannten Winkelmarken erzeugt eine versionierte Kalibrierung.

Die Runtime speichert:

- rohe Phase,
- kalibrierten Winkel,
- Kalibrierungs-ID,
- Featureanzahl,
- Inlierquote,
- Re-Lock-Status,
- Konfidenz.

## 8.5 Phase Payload

```json
{
  "track_id": "stem-1842",
  "frame_id": "frame-5012",
  "delta_phase_deg": 6.7,
  "cumulative_phase_deg": 114.5,
  "direction": "CLOCKWISE",
  "tracking_mode": "INCREMENTAL",
  "relocked": false,
  "feature_count": 83,
  "inlier_ratio": 0.81,
  "phase_confidence": 0.86,
  "rotation_calibration_id": "..."
}
```

---

# 9. Krümmung über Rollphase

## 9.1 Beobachtungsfolge

Jede gültige Beobachtung enthält:

\[
(\varphi_t, B_t, K_t, q_t)
\]

mit:

- \(\varphi_t\): kumulative Rollphase,
- \(B_t\): vorzeichenbehaftete horizontale Krümmung,
- \(K_t\): nichtnegative horizontale Krümmungsenergie,
- \(q_t\): Qualitätsgewicht.

## 9.2 Planarer Basissolver

Für einen einfachen Bogen:

\[
B(\varphi)
=
b_0+b_1\cos(\varphi)+b_2\sin(\varphi)
\]

Robuster gewichteter Fit.

## 9.3 Allgemeiner Energiesolver

Da die minimale Stellung alle 180° wiederkehrt:

\[
K^2(\varphi)
=
c_0+c_1\cos(2\varphi)+c_2\sin(2\varphi)
\]

Optional weitere harmonische Terme nur nach validierter Notwendigkeit.

## 9.4 Minimum und Maximum

\[
\varphi_\min
=
\arg\min_{\varphi\in[0^\circ,180^\circ)}
K(\varphi)
\]

\[
\varphi_\max
=
\arg\max_{\varphi\in[0^\circ,180^\circ)}
K(\varphi)
\]

Ausgänge:

- `curvature_min_mm`
- `curvature_max_mm`
- `curvature_span_mm`
- `target_phase_deg`
- `maximum_phase_deg`
- `fit_residual`
- `fit_confidence`
- `target_observed`

## 9.5 Relevanter Bogen

Wenn:

\[
K_\max-K_\min
\]

nicht deutlich größer als Messrauschen und Modellresiduum ist, lautet das Ergebnis:

```text
BOW_NOT_RELEVANT
```

Dann ist die Rollorientierung für die Vorzentrierung nicht relevant oder die beobachtete Rotation war unzureichend.

## 9.6 S-Bogen und nichtplanare Krümmung

Auch bei S-Bögen kann die horizontale Gesamtenergie ein Minimum besitzen.

Das System gibt zusätzlich aus:

- Restkrümmung im Minimum,
- Verhältnis `K_min / K_max`,
- Bogenkohärenz,
- Kennzeichnung `NON_PLANAR_OR_S_BOW`.

Ist das Minimum flach oder instabil, wird keine scheinpräzise Gradzahl erzeugt.

---

# 10. Entscheidung nach dem Aufliegen

## 10.1 Aktuelle Phase

Nach dem Aufliegen wird die aktuelle Mantelphase bestimmt:

- fortgesetztes inkrementelles Tracking,
- oder Keyframe-Re-Lock.

## 10.2 Korrekturwinkel

\[
\Delta\varphi
=
\operatorname{wrap}_{[-90^\circ,90^\circ)}
\left(
\varphi_\min-\varphi_\text{current}
\right)
\]

Die Richtung wird aus Vorzeichen und konfigurierter Blickrichtung bestimmt.

## 10.3 Zustände

- `ROTATE`
- `NO_ROTATION`
- `UNCERTAIN`

`NO_ROTATION` bei:

- Bogen nicht relevant,
- oder Winkel innerhalb der Toleranz.

## 10.4 Confidence

Gesamtvertrauen aus mindestens:

- Segmentierungsqualität,
- Trackstabilität,
- Geometriequalität,
- beobachteter Rotationsspanne,
- Phasenqualität,
- Re-Lock-Qualität,
- Fitresiduum,
- Eindeutigkeit des Minimums,
- Sichtbarkeit am Decision Frame.

---

# 11. Zustandsautomat

```text
WAITING
  ↓
CANDIDATE
  ↓
OBSERVING_CURVATURE
  ↓ Curvature Freeze Gate
TRACKING_PHASE
  ↓ Stamm liegt / aktuelle Phase stabil
READY_TO_DECIDE
  ↓ Decision Gate
RESULT_READY
  ↓ optional Walzendrehung
TRACKING_TO_TARGET
  ↓
DONE
```

## 11.1 `OBSERVING_CURVATURE`

- Geometrie und Phase sammeln.
- Minimum-/Maximum-Fit laufend aktualisieren.

## 11.2 `TRACKING_PHASE`

- Keine neue Gesamtkrümmung bei schlechter Kontursicht.
- Zielphase bleibt gespeichert.
- Mantelphase wird weiterverfolgt.

## 11.3 `TRACKING_TO_TARGET` optional

Während der ersten Walzendrehung darf die sichtbare Mantelfläche zur Phasenregelung genutzt werden, auch wenn keine Gesamtkrümmung mehr messbar ist.

Ziel:

```text
Stoppe, sobald aktuelle Phase die gespeicherte Minimalphase erreicht.
```

Diese Phase ist fachlich getrennt von der Krümmungsberechnung.

---

# 12. Runtime-Pipeline im bestehenden Inferenz Server

Empfohlene Nodes:

```text
source.channel_frame
→ preprocessing.camera_undistort
→ preprocessing.process_coordinate_rectify
→ inference.segmentation
→ analysis.active_stem_tracker
→ analysis.stem_horizontal_curvature
→ analysis.stem_surface_phase
→ analysis.stem_curvature_phase_solver
→ output.overlay
→ output.result
→ später optional output.siemens_sps_write
```

## 12.1 Zustandsbehaftete Nodes

Mindestens:

- `analysis.active_stem_tracker`
- `analysis.stem_surface_phase`
- `analysis.stem_curvature_phase_solver`

Sie verwenden den vorhandenen `RuntimeExecutionContext.state` und müssen im Realtime Executor als stateful registriert sein.

---

# 13. Resultvertrag

Die tatsächliche Versionsnummer muss Codex anhand des aktuellen Codes als nächste freie Version festlegen.

Logischer Zielvertrag:

```json
{
  "contract": "stem.orientation.<next>",
  "algorithm": "CURVATURE_PHASE_MINIMUM",
  "session_id": "stem-session-1842",
  "track_id": "stem-1842",
  "decision": "ROTATE",
  "recommended_rotation_deg_signed": -43.5,
  "recommended_rotation_deg_abs": 43.5,
  "direction": "COUNTER_CLOCKWISE",
  "rotation_reference_view": "LOOKING_IN_CONVEYOR_DIRECTION",
  "curvature_min_mm": 4.2,
  "curvature_max_mm": 37.8,
  "curvature_span_mm": 33.6,
  "curvature_min_normalized": 0.0011,
  "curvature_max_normalized": 0.0094,
  "target_phase_deg": 71.0,
  "maximum_phase_deg": 161.0,
  "current_phase_deg": 114.5,
  "observed_rotation_span_deg": 66.0,
  "target_observed": true,
  "phase_relocked_after_impact": true,
  "fit_residual": 0.08,
  "geometry_confidence": 0.92,
  "phase_confidence": 0.86,
  "fit_confidence": 0.91,
  "overall_confidence": 0.88,
  "calibration_id": "...",
  "rotation_calibration_id": "...",
  "algorithm_version": "curvature-phase-minimum-v1",
  "reason_codes": []
}
```

---

# 14. Label Studio und Daten

## 14.1 Produktivsystem V1

Für V1 ist kein manuelles temporales Ziel-Frame-Label erforderlich.

Label Studio wird primär für die Stammsegmentierung verwendet.

Die Zielphase entsteht deterministisch aus:

- horizontaler Krümmung,
- Mantelphase,
- periodischem Fit.

## 14.2 Automatische Sessionaufzeichnung

Für Diagnose und spätere Weiterentwicklung speichert die Runtime optional:

- Roh- oder entzerrtes Video,
- Overlayvideo,
- Masken,
- Geometriezeitreihe,
- Phasenspur,
- Fitparameter,
- Resultvertrag,
- Kalibrierungs-IDs.

## 14.3 Optionales Sequenzreview

Ein späteres allgemeines Sequenzreview darf technische Labels erfassen:

- verwendbar,
- unbrauchbar,
- Segmentierungsfehler,
- Trackfehler,
- Phasenfehler,
- unzureichende Rotation,
- Solver plausibel/unplausibel.

Es darf nicht wieder zu einer subjektiven Ziel-Frame-Ground-Truth werden.

---

# 15. Fail-closed Reason Codes

Mindestens:

```text
CALIBRATION_MISSING
PROCESS_RECTIFICATION_INVALID
ROTATION_CALIBRATION_MISSING
ACTIVE_STEM_AMBIGUOUS
TRACK_SWITCHED
INSUFFICIENT_VALID_CURVATURE_FRAMES
INSUFFICIENT_COMMON_STEM_SEGMENT
INSUFFICIENT_ROTATION_SPAN
CURVATURE_FIT_UNSTABLE
CURVATURE_MINIMUM_NOT_UNIQUE
BOW_NOT_RELEVANT
NON_PLANAR_OR_S_BOW
PHASE_TRACK_LOST
PHASE_RELOCK_FAILED
DECISION_FRAME_MISSING
OCCLUDED_BEFORE_CURVATURE_FREEZE
LOW_OVERALL_CONFIDENCE
```

Bei Unsicherheit kein Winkel.

---

# 16. Validierung

## 16.1 Referenzkörper

Verwende:

- geraden Referenzzylinder,
- gebogenen Referenzkörper,
- markierte Teststämme mit bekannten Umfangswinkeln.

Prüfe:

- reine Translation erzeugt keine Rollphase,
- bekannte Rotation wird korrekt gemessen,
- Krümmungsminimum wird reproduzierbar gefunden,
- Richtung ist korrekt,
- absolute Krümmungswerte sind ausreichend stabil,
- verschiedene Bildpositionen und Durchmesser.

## 16.2 Shadow Mode

Vor SPS-Aktivierung:

- mehrere hundert reale Sessions,
- Winkel- und Confidence-Verteilung,
- Unsicherheitsquote,
- Fehlfälle,
- Wiederholbarkeit,
- Drift der Kamera,
- Vergleich mit Bediener und Prozesseinzug.

## 16.3 Zielwerte für erste Abnahme

Projektziele, vor Produktionsfreigabe zu bestätigen:

- Medianfehler Rollwinkel ≤ 5°,
- P95 Rollwinkel ≤ 10°,
- keine Vorzeichenverwechslung,
- Krümmungsminimum reproduzierbar innerhalb ±5°,
- Winkelentscheidung nur bei gültiger Confidence,
- definierte maximale Laufzeit bis Decision Gate.

---

# 17. SPS- und Walzenintegration

## 17.1 Primärer Feed-forward-Befehl

Vor dem Walzenkontakt:

```text
Drehrichtung + Sollwinkel + Confidence
```

## 17.2 Ausführung

Die sichere Maschinenlogik bleibt in der SPS.

Mögliche Ausführung:

- Walzenencoder,
- kalibrierte Zeit-/Wegsteuerung,
- Mantelphasen-Feedback aus der Kamera.

## 17.3 Bevorzugte Regelung

Wenn nach dem Greifen ausreichend Mantelfläche sichtbar bleibt:

```text
drehen
→ aktuelle Phase verfolgen
→ bei Zielphase stoppen
```

Dafür ist keine erneute Krümmungsmessung nötig.

---

# 18. Nicht-Ziele von V1

- keine 3D-Punktwolke,
- keine vollständige XYZ-Messung jedes Stammpunkts,
- keine zweite Produktionskamera,
- keine Fragmentrekonstruktion in der Maschine,
- kein subjektives Ziel-Frame-Label,
- kein TCN/GRU als Voraussetzung,
- kein Video-Transformer,
- keine aktive SPS-Schreibung ohne Shadow- und Referenzvalidierung.
