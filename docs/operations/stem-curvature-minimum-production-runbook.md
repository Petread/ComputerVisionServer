# Curvature-Minimum: Produktions- und Rollback-Runbook

Dieses Runbook beschreibt die Einfuehrung des Algorithmus
`CURVATURE_PHASE_MINIMUM` in der bestehenden Inferenzserver-Anwendung. Es erzeugt
keine zweite Anwendung und ersetzt keine bestehende Pipelineversion. Messungen mit
Kamera, SPS und RTX 4500 Ada erfolgen auf dem Zielserver; auf dem Entwicklungsrechner
sind nur statische, Unit- und Frontend-Build-Pruefungen belastbar.

## Unveraenderliche Sicherheitsgrenzen

- Alte Pipelineversionen und `stem.orientation.v2` bleiben unveraendert lesbar.
- Die neue Variante publiziert `stem.orientation.v3`; alte Resultate werden nicht als
  Minimumprinzip interpretiert.
- `shadow_mode_enabled`, `shadow_evaluation_approved`,
  `stem_orientation_command_enabled` und echte SPS-Schreibfreigaben sind standardmaessig
  `false`.
- Ohne gueltige Prozess-, Rotations- und Referenzvalidierung entsteht kein SPS-Befehl.
- Vision ist weder Sicherheitssteuerung noch Ersatz fuer SPS-seitige Winkel-, Zeit-,
  Verriegelungs- oder Not-Halt-Grenzen.
- Nach dem Curvature Freeze Gate wird keine neue Kruemmung aufgenommen. Eine optionale
  Nachfuehrung verwendet ausschliesslich die weiterlaufende Mantelphase.

## Updatevorbereitung

1. Bestehendes Metadatenbackup mit dem vorhandenen Backupjob erstellen. PostgreSQL ist
   die Quelle fuer Pipelineversionen, Kalibrierungsartefakte, Shadow-Resultate und Reviews.
2. Aktive Pipelineversionen, Kanalzuordnungen und den aktuellen Release-Pointer notieren.
3. Den vorhandenen Windows-Offline-Installer beziehungsweise das vorhandene
   Windows-Updatepaket bauen. Der Export prueft explizit Wheels fuer NumPy,
   `opencv-python-headless`, `python-snap7` und `pypylon`.
4. Das Paket ohne Netzverbindung auf dem Zielsystem anwenden. Der WSL2-Release-Check
   importiert OpenCV/NumPy und prueft Registry, Payloads, Executor, Result-Service und
   Admin-Frontend des Release-Kandidaten vor der Aktivierung.
5. Keine bestehende Pipeline in-place aendern. Eine neue unveraenderliche Version aus
   der freigegebenen bisherigen Version erzeugen.

Es ist keine neue Datenbankmigration fuer Shadow Mode oder SPS-Dry-Run erforderlich.
Shadow-Feedback verwendet die bestehende append-only Tabelle
`stem_orientation_reviews`; Sessions verwenden die bestehende `result_records`-Historie.

## Kamera- und Prozesskalibrierung

Die Pipeline muss in dieser Reihenfolge binden:

1. `preprocessing.camera_undistort` mit gueltiger intrinsischer Kalibrierung.
2. `preprocessing.process_coordinate_rectify` mit gueltigem
   `RuntimeImageViewProfile` fuer die kanonische Modell-/Labelansicht und optionalem
   `StemCurvatureProcessCalibration` fuer Anlagen-X/Y, dokumentiertes Z,
   Referenzebene, Candidate ROI, Curvature Observation ROI, Surface Tracking ROI und
   Prozessgates.

Im Intrinsikeditor zeigt die Anwendung den unverkleinerten Quellframe des gebundenen
Kamerakanals. Dazu wird nur der unabhaengige Kamera-Stream gestartet; die
Inferenz-Runtime darf gestoppt und die Pipeline noch ungueltig sein. Der Frame kommt aus
dem vorhandenen `camera-media`-Shared-Memory-Ring. Jede gewuenschte Brettlage wird mit
`Use current live frame` oder `Capture new board observation` uebernommen, ohne den Stream
zu unterbrechen oder die Kamera ein zweites Mal zu oeffnen. Sind Stream und Runtime
gestoppt, kann `Capture new board observation` die Kamera alternativ exklusiv fuer genau
einen Frame oeffnen. Die Standardkonfiguration
`9 x 6` bezeichnet innere Checkerboard-Ecken und benoetigt daher ein Brett mit `10 x 7`
Feldern. Mindestens fuenf, besser zehn bis fuenfzehn, scharfe und ueber das Vollbild
verteilte Lagen erfassen, danach das Profil berechnen und anwenden. Eine noch ungueltige
Kalibrierung darf als Entwurfsversion gespeichert, aber nicht produktiv publiziert werden.
3. `inference.segmentation` mit aktivierter View-Profil-Kompatibilitaetspruefung.
4. `analysis.active_stem_tracker` fuer genau einen aktiven Track.
5. `analysis.stem_measurement_eligibility` vor dem Label-Studio-Capture.
6. `analysis.stem_horizontal_curvature` fuer horizontale projektive Y-Kruemmung.
7. `analysis.stem_surface_phase` mit versionierter Gradkalibrierung.
8. `analysis.stem_curvature_phase_solver` fuer das eingefrorene V3-Winkelresultat.

### Eingebaute Runtimevorlage

Im bestehenden Pipeline Designer steht die Built-in-Vorlage
`Stammausrichtung – Krümmungsminimum` mit dem stabilen Slug
`template-realtime-stem-curvature-minimum` zur Verfügung. Beim Instanziieren werden
die oben genannten Nodes einschließlich `analysis.stem_mask_geometry`, aller
erforderlichen Portkanten, Diagnoseoverlay, kanonischer Liveansicht und
`stem.orientation.v3`-Projektionen für Live View und Result History erzeugt.

Die Vorlage ist absichtlich fail closed: `shadow_mode_enabled=true`,
`shadow_evaluation_approved=false`, Artefaktaufzeichnung aus und kein SPS-Node. Vor
dem Validieren und Publizieren sind nur die standortabhängigen Ressourcen zu binden:

- Intrinsikprofil in `undistort`,
- Image-View- und Prozesskalibrierung in `canonical_view`,
- das zum View-Profil kompatible Stammsegmentierungsmodell in `segment_stem`,
- die Gradkalibrierung in `track_surface_phase`,
- für die spätere objektive Freigabe das Validierungsartefakt in `solve_orientation`.

Der erwartete Profilname `stem-measurement-view-v1` ist bereits konsistent in
`canonical_view` und `measurement_eligibility` eingetragen. Wird eine andere ID
verwendet, müssen beide Bindungen gemeinsam in einer neuen Pipelineversion geändert
werden. Dataset-/Label-Studio-Capture ist nicht Bestandteil der produktiven
Analysevorlage; dafür wird der im Messkorridor-Dokument beschriebene bestehende
Capture-Pfad mit demselben kanonischen Frame verwendet.

Die Release-Preflights pruefen den Image-View-Kern und den Eligibility-Node. Dafuer
sind keine neue Dependency und keine neue Datenbankmigration erforderlich; NumPy und
OpenCV gehoeren bereits zum bestehenden `cv-runtime`-Wheelbundle. Details und
Labelregeln stehen in `docs/stem-orientation/canonical-measurement-view.md`.

Vor Freigabe pruefen:

- Bildaufloesung entspricht beiden Kalibrierungen.
- Prozess- und Phasenkalibrierung referenzieren denselben Kanal und rectified Bildraum.
- Reprojektionsfehler und Homographie sind gueltig.
- Gates liegen in Prozessreihenfolge und der Messbereich ist nicht leer.
- Betrachtungsstandpunkt, positive Drehung und SPS-Richtungskonvention stimmen ueberein.
- Kamerahalterung und Fokus sind markiert; ein mechanischer Eingriff erzwingt eine neue
  Kalibrierungs- und Pipelineversion.

## Objektive Referenzvalidierung

Im Solver muss ein unveraendertes
`stem.minimum-principle-validation.v1`-Artefakt gebunden sein. Nur
`VALIDATION_PASSED` ist SPS-faehig. Artifact-ID, Version, Hash sowie Prozess- und
Phasenkalibrierung werden in `stem.orientation.v3` eingefroren.

Der Bericht muss mindestens den geraden Referenzzylinder, den gebogenen Referenzkoerper
und markierte Teststaemme abdecken. Subjektive Ziel-Frames und geschaetzte Winkel sind
keine Ground Truth.

## Shadow Mode

1. In einer neuen Solver-Pipelineversion `shadow_mode_enabled=true` setzen.
2. Der Realtime Executor setzt fuer diese Version `plc_writes_allowed=false`, auch wenn
   die Umgebung SPS-Schreiben generell erlaubt.
3. Pro Session wird genau das erste eingefrorene Entscheidungsereignis kompakt behalten.
   Framebilder und Masken werden dadurch nicht automatisch Datenbankhistorie.
4. Retention und `max_records` der bestehenden Kanal-History begrenzen die Datenmenge.
   Optionale Sessionvideos unterliegen zusaetzlich `session_recording_maximum_frames` und
   `session_recording_retention_days`.
5. In Result History eine konkrete Pipelineversion filtern. Die Shadow-Auswertung zeigt
   Winkel-, Entscheidungs-, Confidence-, Reason-Code- und Latenzverteilungen sowie
   belegbare Kamera-/Kalibrierungsdriftsignale.
6. Bedienerfeedback ist auf `plausibel`, `unplausibel`, `unsicher` und einen optionalen
   technischen Fehlergrund beschraenkt.
7. Nach fachlicher Abnahme eine neue Pipelineversion erstellen und darin
   `shadow_evaluation_approved=true` setzen. Dieser Schalter aktiviert keine SPS-Ausgabe.

## SPS-Dry-Run

Es wird ausschliesslich `output.siemens_sps_write` verwendet. Fuer die Abnahme:

- `enabled=true`
- `dry_run=true`
- `stem_orientation_command_enabled=true`
- explizite Mappingzeilen auf vorhandene SPS-Adressen

Der Status `siemens_sps_write_status.stem_orientation_command` enthaelt:

- `command_id`, Session- und Track-ID
- signierten Sollwinkel und Richtung
- Ziel- und aktuelle Phase
- Overall Confidence und `valid_until`
- Prozess-, Rotations- und Validierungsidentitaeten
- Algorithmus- und Komponenten-Versionen
- Reason Codes, Gatezustand und One-Shot-Kennzeichen

Ein Hauptdrehbefehl wird nur einmal pro `command_id` als bereit gemeldet. Die SPS muss die
`command_id` dennoch selbst deduplizieren. Das Gate blockiert bei `UNCERTAIN`,
`NO_ROTATION`, Ablauf, Trackwechsel, ungueltiger Kalibrierung, niedriger Confidence,
Prozesswinkelueberschreitung, nicht bestandener Referenzvalidierung, fehlender
Shadow-Freigabe, Deadlinefehler oder Runtimefehler.

Erst nach separat autorisierter Anlagenabnahme darf `dry_run=false` gesetzt werden. Die
zusaetzliche globale Runtime-/Umgebungsfreigabe bleibt erforderlich; ein Update setzt sie
nicht automatisch.

## Optionale Phasenregelung

Die Option bleibt separat deaktiviert. Vor Aktivierung muessen in der SPS unabhaengige
maximale Drehzeit und maximaler Winkel implementiert und anschliessend in der
Pipelineversion mit `sps_rotation_limits_confirmed=true` bestaetigt sein.

Mit `closed_phase_control_enabled=true` liefert der bestehende SPS-Node unter
`phase_control`:

- aktuelle und Zielphase,
- signierten Phasenfehler,
- Phasenconfidence,
- `stop_requested`,
- dokumentierte SPS-Grenzen und Reason Codes.

`phase_control.stop_requested` darf auf eine explizite SPS-Mappingzeile gelegt werden.
Bei Track-Mismatch, Phasenverlust, niedriger Confidence, nicht eingefrorener Kruemmung
oder fehlender SPS-Grenzbestaetigung bleibt die Regelung nicht verfuegbar. Es gibt keine
offene Testimpuls-Suche.

## Zielserver-Performanceabnahme

Keine Sollwerte als Messwerte eintragen. Auf dem Windows-11-/WSL2-Zielsystem mindestens
mehrere hundert reale Sessions und einen Langzeitlauf erfassen.

Die bestehende Engine schreibt pro Frame `timing.node_timings_ms` und
`timing.inference_timings_ms`. Daraus fuer die produktive Pipeline Median, P90 und P95
ermitteln fuer:

- Segmentierungsnode,
- `analysis.stem_horizontal_curvature`,
- `analysis.stem_surface_phase`,
- `analysis.stem_curvature_phase_solver`,
- End-to-End `timing.total_ms` und Sessionstart bis Decision Timestamp.

Parallel auf dem Zielserver protokollieren:

- GPU-Auslastung und -Speicher mit `nvidia-smi`,
- RSS/Working Set des Realtime Executors,
- Result- und Sessionartefakt-Speicherwachstum,
- Kamera-Reconnects, Frame-Drops, Phase-Re-Locks und `UNCERTAIN`-Anteil.

Abnahmeprotokoll:

| Kennwert | Median | P90 | P95 | Grenzwert | Status |
|---|---:|---:|---:|---:|---|
| Segmentierung [ms] |  |  |  |  |  |
| Geometrie/Kruemmung [ms] |  |  |  |  |  |
| Phasentracking [ms] |  |  |  |  |  |
| Solver [ms] |  |  |  |  |  |
| End-to-End pro Frame [ms] |  |  |  |  |  |
| Session bis Decision [ms] |  |  |  |  |  |
| GPU-Speicher [MiB] |  |  |  |  |  |
| Executor RSS [MiB] |  |  |  |  |  |

## Reconnect-, Restart- und Ablaufpruefung

- Kamera kurz trennen und wieder verbinden. Erwartung: Reconnect ueber den bestehenden
  Sourcepfad; die laufende Session endet fail closed, kein alter Winkel wird ausgegeben.
- Realtime Executor waehrend einer Session neu starten. Erwartung: In-Memory-Track-,
  Phasen-, Solver- und SPS-One-Shot-State ist leer; erst eine neue gueltige Session kann
  entscheiden.
- Zwei Staemme nacheinander fahren. Erwartung: unterschiedliche Session-/Track-IDs und
  kein uebernommener Solver- oder Phasenstate.
- Ein Result bis nach `result_expiry` liegen lassen. Erwartung:
  `STEM_SPS_RESULT_EXPIRED`, keine Mappingzeile schreibt.
- Ohne Prozess- oder Phasenkalibrierung starten. Erwartung: Pipelinevalidierung oder
  Runtime fail closed.

## Reason-Code-Triage

- `STEM_SPS_REFERENCE_VALIDATION_*`: Referenzartefakt fehlt, ist ungueltig oder nicht
  bestanden.
- `STEM_SPS_SHADOW_EVALUATION_NOT_APPROVED`: neue Pipelineversion ohne dokumentierte
  fachliche Shadow-Freigabe.
- `STEM_SPS_CALIBRATION_INVALID`: Prozess- oder Rotationsidentitaet fehlt.
- `STEM_SPS_CONFIDENCE_TOO_LOW`: Overall Confidence unter Pipelinegrenze.
- `STEM_SPS_ANGLE_OUTSIDE_PROCESS_LIMIT`: Winkel fehlt, ist null oder liegt ausserhalb
  der Prozessgrenze.
- `STEM_SPS_DIRECTION_CONVENTION_INVALID`: Blickrichtung, positive Drehrichtung oder
  physische Richtung passt nicht zum signierten Winkel.
- `STEM_SPS_RESULT_EXPIRED`: Decision Result ist nicht mehr gueltig.
- `STEM_SPS_TRACK_CHANGED`: Track innerhalb derselben Session wechselte.
- `PHASE_CONTROL_*`: optionale Nachfuehrung ist deaktiviert oder fail closed.

## Rollback

1. SPS-Ausgabe deaktiviert lassen beziehungsweise die Anlagenfreigabe entziehen.
2. Betroffenen Kanal stoppen.
3. Kanal auf die zuvor dokumentierte unveraenderliche Pipelineversion zurueckbinden.
4. Bei Releaseproblemen den vorhandenen Windows-Rollbackpfad beziehungsweise
   `deployment/rollback.ps1` verwenden; keine Dateien oder Datenbanken manuell ersetzen.
5. PostgreSQL und persistente State-Verzeichnisse nicht loeschen. Kalibrierungen,
   Pipelineversionen, Shadow-Resultate und Reviews muessen Update und Rollback ueberleben.
6. Kanal starten, Kamera-Reconnect und einen alten, nicht umgedeuteten Pipelinepfad
   pruefen.
7. Erst danach eine neue korrigierte Minimumprinzip-Pipelineversion anlegen.
