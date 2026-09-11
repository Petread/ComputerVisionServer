# Implementierungsplan: geometrische Eindrehoptimierung für Rundholz

Stand der Repository-Analyse: 20. August 2026

## 1. Ziel und Abgrenzung

Dieser Plan beschreibt die Integration der Fachspezifikation
`docs/Stammoptimierung und 3D Messung/stammoptimierung_spezifikation.md` in den
bestehenden Inferenzserver. Er ersetzt weder die vorhandene Plattform noch die bereits
implementierte Stammausrichtung nach dem Krümmungsminimum.

Die neue Funktion optimiert ausschließlich die gemessene Stammgeometrie relativ zur
festen Maschinen-/Zentrierachse. Sie analysiert keinen Einschnittplan und benötigt keine
Produktbreiten, Produktstärken oder Produktlängen. Das Ergebnis ist eine relative,
signierte Drehung `delta_angle_deg` und ein geometrischer Gewinn `gain_mm` in genau einer
priorisierten Richtung:

- `VORSCHNITT_KRITISCH`: oben/unten, also Maschinen-`z`,
- `NACHSCHNITT_KRITISCH`: links/rechts, also Maschinen-`y`.

Die erste Ausbaustufe bleibt Empfehlung beziehungsweise Shadow-Ergebnis. Der vorhandene
SPS-Schreibpfad wird nicht freigeschaltet und nicht auf das neue Ergebnis erweitert.

## 2. Kurzentscheidung zur Architektur

Die Funktion wird als Erweiterung der bestehenden Registry-Pipeline implementiert:

1. Kamera, kanonische Ansicht, Segmentierung, aktive Stamminstanz, Messkorridor,
   Prozesskalibrierung und Mantelphasentracking werden unverändert wiederverwendet.
2. Ein neuer zustandsbehafteter Slice-Model-Node sammelt die bereits metrischen
   `StemHorizontalCurvaturePayload`-Profile zusammen mit der kalibrierten
   `StemSurfacePhasePayload` und der Track-/Bewegungsinformation.
3. Reine Python-Module bauen und bereinigen daraus ein versioniertes Scheibenmodell.
4. Ein stateless Optimizer bewertet dieses eingefrorene Modell für den aktuell wirksamen
   Prioritätsmodus.
5. Der manuelle Prioritätswert wird als kleine, typisierte Channel-Control-Eigenschaft im
   vorhandenen PostgreSQL-/Alembic-Pfad gespeichert und vom Realtime-Executor in den
   `RuntimeExecutionContext` übernommen.
6. Die bestehende Admin-Web-App zeigt Auswahl, Entscheidung, Längsansicht,
   Grenzquerschnitt und lokale Scorekurve. Es entsteht keine zweite UI.
7. Result History und der vorhandene Temporal-/Diagnostik-Recorder speichern Entscheidung,
   Modellreferenzen und Replayartefakte. Es entsteht keine zweite Datenbank oder
   Recorder-Anwendung.

Die vorhandene Pipelinevorlage
`template-realtime-stem-curvature-minimum` bleibt für bestehende Pipelineversionen und
Vergleichsmessungen erhalten. Für die geometrische Optimierung wird im selben
`cv_db.pipeline_store` eine neue, auf denselben Mess-Nodes basierende Vorlage angelegt.
Gemeinsame Vorlagenabschnitte sollen dabei in einen internen Builder extrahiert werden,
damit Kamera-, Segmentierungs- und Kalibrierkonfiguration nicht kopiert auseinanderläuft.

## 3. Tatsächliche Ist-Architektur

### 3.1 Build, Anwendungen und Tests

| Bereich | Bestand | Bedeutung für die Erweiterung |
|---|---|---|
| Python-Monorepo | `pyproject.toml`, uv-Workspace, Python >= 3.11 | Neue Backendlogik bleibt in bestehenden Workspace-Paketen. |
| Runtime-Domäne | `backend/packages/cv-runtime` | Reine Geometrie, Payloads und Node-Handler gehören hierhin. |
| Gemeinsame API-Verträge | `backend/packages/cv-contracts` | Channel-Control- und API-Modelle werden hier ergänzt. |
| Status-/Task-Enums | `backend/packages/cv-status` | Nur echte plattformweite Statuswerte gehören hierhin; der fachliche Prioritätsmodus kann als typisierter Runtime-Vertrag geführt werden. |
| Persistenz | `backend/packages/cv-db`, `backend/migrations` | Bestehendes PostgreSQL und Alembic verwenden; keine SQLite-/Nebendatenbank. |
| Liveausführung | `backend/services/realtime-executor` | Bestehende Frameordnung, State, Result History und Recorder verwenden. |
| Runtime-Administration | `backend/services/runtime-admin-api` | Bestehenden Channel-Vertrag und Auditpfad erweitern. |
| Frontend | `frontend/apps/admin-web` | Bestehende Channel-Detail- und Result-History-Seiten erweitern. |
| Frontend-Verträge | `frontend/packages/schemas`, `frontend/packages/api-client` | OpenAPI-Konvention und bestehender `api.channels.update`-Pfad. |
| Tests | `pytest` unter `tests/unit/backend`, Vitest im Admin-Web | Neue Tests in den vorhandenen Suites ergänzen. |
| Frontend-Build | pnpm 10, Skripte in `package.json` | `lint:frontend`, `test:frontend`, `build:admin-web`. |
| Deployment | `deployment/export-windows-update.ps1`, `deployment/apply-windows-update.ps1`, `deployment/rollback.ps1`, `deployment/wsl/apply-release.sh` | Änderung über das bestehende Windows-11-/WSL2-Offline-Update ausliefern; keine separate Docker-Anwendung. |

Die Anwendung ist laut Repository-Regel auf dem Entwicklungsrechner nicht live ausführbar.
Live-, Kamera- und End-to-End-Abnahmen erfolgen auf dem Zielserver. Lokal sind statische
Prüfungen, Unit-Tests, Schemaexport und Frontend-Build vorgesehen.

### 3.2 Vorhandener Runtime-Datenfluss

Die produktionsnahe Vorlage wird in
`backend/packages/cv-db/src/cv_db/pipeline_store.py` durch
`_stem_curvature_minimum_template_definition` erzeugt. Ihr relevanter Datenfluss ist:

```text
source.channel_frame
  -> preprocessing.camera_undistort
  -> preprocessing.process_coordinate_rectify
  -> inference.segmentation
  -> analysis.active_stem_tracker
       -> analysis.stem_measurement_eligibility
       -> analysis.stem_horizontal_curvature -----------+
       -> analysis.stem_mask_geometry                   |
             -> analysis.stem_surface_phase ------------+
                    -> analysis.stem_curvature_phase_solver
                         -> output.result / output.overlay / output.display_output
```

Die Registry liegt in `backend/packages/cv-runtime/src/cv_runtime/registry.py`. Ein Node
folgt `RuntimeNodeHandler.execute(context, node, inputs) -> NodeExecutionResult`. Fachlich
größere Handler werden bereits über Modulreferenzen geladen, zum Beispiel
`cv_runtime.nodes.stem_horizontal_curvature` und
`cv_runtime.nodes.stem_curvature_phase_solver`.

Der Realtime-Executor hält `pipeline_state` in
`backend/services/realtime-executor/src/realtime_executor/live.py`. Nur Node-Typen in
`_STATEFUL_PIPELINE_NODE_TYPES` erhalten garantiert serialisierte Verarbeitung mit
`pipeline_max_inflight=1`. Der State liegt unter `context.state[node.node_key]` und ist
nicht über einen Runtime-Neustart persistent. Draft Preview ist pro Request stateless und
kann deshalb keine vollständige Mehrframe-Messung simulieren.

### 3.3 Kameraaufnahme und Frame-/Messzyklus

- `backend/services/camera-media` besitzt die Kamera-/Media-Prozesse und die geteilte
  Latest-Frame-Übertragung.
- `backend/services/realtime-executor/src/realtime_executor/sources.py` enthält
  `SharedMemoryFrameSource`, `OpenCvRtspFrameSource`, `AravisGigeRtspFrameSource`,
  `HarvesterBlackflyFrameSource`, `BaslerPylonFrameSource` und `FileTestFrameSource`.
- `FramePayload`, `RuntimeImageContext` und Transform-Provenienz liegen in
  `cv_runtime.payloads`.
- `analysis.active_stem_tracker` hält genau eine aktive Stamminstanz, eine stabile
  `track_id`, Zonenstatus, Framealter, normalisierte Bewegung sowie kalibrierte
  `delta_x_mm`, `delta_y_mm`, Geschwindigkeiten und die Klassifikation
  `transfer_y`/`longitudinal_x`/`stationary`/`ambiguous` in
  `active_stem_tracking.movement`.
- `analysis.stem_measurement_eligibility` sperrt ungeeignete, unscharfe, instabile oder
  bereits hinter dem Freeze-Gate liegende Frames fail closed.
- Prozesskalibrierung, `session_start_gate`, `curvature_freeze_gate`, `decision_gate` und
  Förderrichtung sind im versionierten Prozessprofil gebunden.
- `analysis.stem_curvature_phase_solver` implementiert bereits einen Track-/Session-Zyklus,
  friert die Vor-Einzug-Entscheidung ein und lässt danach nur eine begrenzte
  Phasen-Nachverfolgung zu.

Diese Lebenszyklusbausteine werden wiederverwendet. Es wird kein zweiter Kamera- oder
Messzyklusmanager eingeführt.

### 3.4 Segmentierung, Masken und Konturen

- `inference.segmentation` liefert `SegmentPayload` mit Masken, Bildkontext,
  Objektidentität und Modellroute.
- Die aktive Maske wird in
  `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py` durch
  `ActiveStemTrackerHandler` ausgewählt.
- `backend/packages/cv-runtime/src/cv_runtime/stem_geometry.py` bereinigt die Maske,
  wählt eine zusammenhängende Komponente, extrahiert Konturen mit OpenCV, bestimmt per
  PCA die projizierte Stammachse und bildet Centerline-/Breitenprofile. Diese Logik ist
  ausdrücklich Pixel-2D und kein metrisches 3D-Modell.
- `backend/packages/cv-runtime/src/cv_runtime/stem_horizontal_curvature.py` arbeitet auf
  der rektifizierten, metrischen Maske, entfernt kleine Komponenten, bildet standardmäßig
  96 X-Stationen und liefert je Station `x_mm`, `y_center_mm`, `width_mm`, Gültigkeit und
  Gewicht.
- Der zugehörige Handler in
  `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_horizontal_curvature.py` bindet
  Track, Framefolge, Prozesskalibrierung, Eligibility und Freeze-Gate.

Direkt wiederverwendet wird das metrische `StemHorizontalCurvaturePayload`, nicht die
Pixelkoordinate aus `StemGeometryPayload`. Es wird keine zweite Segmentierungs-,
Kontur- oder Morphologieimplementierung für die Optimierung gebaut.

### 3.5 Kalibrierung und Koordinaten

Die relevanten Verträge liegen in
`backend/packages/cv-contracts/src/cv_contracts/runtime.py`:

- `RuntimeImageViewProfile` beschreibt die kanonische Bildansicht.
- `RuntimeStemCurvatureProcessCalibrationProfile` bindet Kamera, Intrinsic-Hash,
  Bildauflösung, Homographie in die Anlagen-`X/Y`-Referenzebene, Prozessgrenzen, ROIs,
  Gates, Förderrichtung, Blickrichtung und positive Drehrichtung.
- `RuntimeStemSurfacePhaseCalibrationProfile` bindet Phasenschätzer, Gradskalierung,
  Prozesskalibrierung, Blickrichtung, positive Drehrichtung und Gültigkeitsbereich.
- `backend/packages/cv-runtime/src/cv_runtime/stem_process_coordinates.py` berechnet und
  validiert Homographien und transformiert Bild-/Prozesspunkte.
- `backend/packages/cv-runtime/src/cv_runtime/stem_surface_phase.py` validiert die
  Phasenkalibrierung. Gradwerte werden nur bei gültigem Profil ausgegeben.

Die bestehende Prozesshomographie ist zweidimensional: physisches `x` längs und
physisches `y` quer/horizontal. `z` ist im Referenzprofil als Ebenennormale und Richtung
dokumentiert, wird aber nicht direkt metrisch gemessen. Ein vollständiges
`(cy, cz, a, b, phi)` entsteht deshalb erst durch die zeitliche, phasenkalibrierte
Rekonstruktion mehrerer Projektionen und nicht aus einem Einzelbild.

### 3.6 Krümmung und Drehwinkeltracking

- `StemHorizontalCurvaturePayload` (`stem.horizontal-curvature.v1`) enthält absolute
  `y_center_mm`-Werte, eine robuste Referenzlinie, Residuen, Breiten, gemeinsamen
  X-Bereich, Qualitätswerte und Kalibrierungsprovenienz.
- `StemSurfacePhasePayload` (`stem.surface-phase.v1`) enthält rohe und kalibrierte,
  kumulative Phase, Trace-Generation, Lock-/Relock-Zustand, Qualität,
  Kalibrierungs-Hashes, `rotation_reference_view` und `positive_rotation`.
- `StemRotationPayload` ist ein älterer relativer Rotationsvertrag. Für die neue
  Messkette ist `StemSurfacePhasePayload` vorzuziehen, weil der aktuelle produktionsnahe
  Pfad dort Kalibrierung, High-Rate-Tracking und Relock zusammenführt.
- `backend/packages/cv-runtime/src/cv_runtime/stem_curvature_phase_solver.py` und der
  zugehörige Node fitten derzeit skalare horizontale Krümmung gegen Phase. Das Ergebnis
  `stem.orientation.v3` trägt explizit den Algorithmus `CURVATURE_PHASE_MINIMUM`.

Der bestehende Solver ist fachlich nicht die neue planunabhängige Ellipsenoptimierung.
Er wird nicht still umgedeutet. Beide Algorithmen brauchen eigene Schema- und
Algorithmuskennungen.

### 3.7 Konfiguration, Logging und Persistenz

- Node-Konfigurationen werden über Registry-`property_schema` und unveränderliche
  Pipelineversionen verwaltet.
- Channel-Konfigurationen und `result_history_policy` liegen in `RuntimeChannel` in
  PostgreSQL und werden über `runtime-admin-api`/`channel_store.py` gepflegt.
- Ein Pipeline-Property ist für die Bedienerauswahl ungeeignet: Eine Änderung erfordert
  Draft/Publish beziehungsweise kann eine Runtime-Neubindung auslösen und erfüllt nicht
  „gleiches eingefrorenes Modell neu optimieren“.
- Runtime-Resultate werden als `ResultRecord` gespeichert. `output.result` projiziert
  deklarierte Felder in Response, History und Live View.
- `_stem_orientation_history_event_key` in `realtime_executor.live` persistiert eine
  eingefrorene `stem.orientation.v2/v3`-Entscheidung einmalig als semantisches Ereignis.
- `_compact_result_record_payload` entfernt Frames und große doppelte Abschnitte, behält
  aber projektierte History-Werte.
- `cv_observability` stellt strukturiertes JSON-Logging und Korrelationskontext bereit;
  `runtime-admin-api` schreibt bereits Audit-Events für Channel-Änderungen.

### 3.8 UI und Visualisierung

- Die Liveansicht liegt in
  `frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx`.
- Der Pipeline Designer liegt in
  `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`.
- Die aktuelle Krümmung-über-Phase-Diagnose liegt in
  `frontend/apps/admin-web/src/features/stem-orientation/curvature-phase-diagnostic-graph.tsx`.
- Historische Stammresultate und technische Reviews werden in
  `frontend/apps/admin-web/src/features/results/result-history-page.tsx` dargestellt.
- API-Zugriffe laufen über `frontend/packages/api-client/src/index.ts`; gemeinsame Typen
  liegen in `frontend/packages/schemas/src/index.ts` und folgen dem OpenAPI-Export.

Die neue Visualisierung wird als weitere Komponente im vorhandenen Featureordner
`stem-orientation` implementiert. Geometrie oder Optimierung werden nicht in TypeScript
nachgerechnet; das Frontend rendert ausschließlich ein versioniertes View Model aus der
Runtime.

### 3.9 SPS-/Steuerungsschnittstellen

Im geprüften Repository existiert ein Siemens-S7-Schreibpfad über
`output.siemens_sps_write`, `cv_runtime.siemens_sps` und `python-snap7`. Der Node ist
standardmäßig deaktiviert beziehungsweise Dry-Run und wird zusätzlich durch
`plc_writes_allowed` und Shadow Mode gesperrt.

Eine implementierte zyklische SPS-Lesequelle für den Prioritätsmodus wurde nicht
gefunden. Ebenso wurde kein verwendeter OPC-UA-, ADS- oder Modbus-Lesepfad gefunden.
`RuntimeControlCommandRecord` ist für Start/Stop/Restart/Reconcile zuständig und kein
dauerhafter fachlicher Sollwertspeicher.

Folgerung: In dieser Ausbaustufe wird nur der Frontend-Adapter implementiert. Ein späterer
PLC-Adapter muss denselben typisierten `StemRotationPriorityPayload` liefern. Er ist eine
eigene freizugebende Phase und darf nicht über den heutigen Schreib-Node simuliert werden.

### 3.10 Vorhandene Test- und Replaydaten

Im Git-verwalteten Repository befinden sich keine echten Stamm-Messsequenzen, Videos oder
Testbilder für diese Funktion. `git ls-files` zeigt als Medien nur zwei allgemeine
Frontend-Assets. `tests/fixtures/synthetic` enthält kleine synthetische Payload-/Bilddaten,
aber keine Rotationssequenz.

Unter `.local` liegen nicht versionierte DVC-Snapshots und ein einzelner Runtime-Live-
Overlayzustand mit eingebettetem Frame. Die geprüften Segmentation-Snapshots tragen die
Labels `Ast`, `Hand` beziehungsweise `Hand/image`; es gibt dort keinen Temporal-Snapshot,
kein `sequence-manifest.json` und kein Videoartefakt. Diese lokalen Daten sind daher kein
portabler Regressionstest für die Stammrotation.

Der vorhandene Recorder in `cv_runtime.temporal_samples` kann bereits rektifizierte und
diagnostische Videos, `observations.jsonl`, `phase_trace.json`, Fit, Ergebnis,
Kalibrierungen und ein Manifest materialisieren. Er ist der richtige Anschluss für
zukünftige echte Replaysequenzen.

## 4. Verbindliche Koordinaten- und Winkelabbildung

### 4.1 Fachlicher Raum

Für alle neuen Verträge und reinen Funktionen gilt ausschließlich:

| Größe | Bedeutung |
|---|---|
| `x` | Stamm-Längsachse/Förderrichtung, Millimeter |
| `y` | horizontal/quer, Millimeter |
| `z` | vertikal, Millimeter |
| Scheibenvektor | `q = (y, z)^T` |
| Rotation | um `+x`, kanonisch nach Rechtsschraubenregel |
| Horizontalachse | `u_H = (1, 0)^T` |
| Vertikalachse | `u_V = (0, 1)^T` |
| aktueller Optimierungszustand | `delta_angle_deg = 0` |

Die virtuelle Rotation ist

```text
R(alpha) = [[cos(alpha), -sin(alpha)],
            [sin(alpha),  cos(alpha)]]
c'       = R(alpha) c
phi'     = phi + alpha
```

### 4.2 Adapter aus dem Altbestand

Es gibt drei unterschiedliche Koordinatensprachen, die nicht vermischt werden dürfen:

1. `StemGeometryPayload.x/y` sind Bildkoordinaten. Die positive Normale wird relativ zur
   orientierten Bildachse definiert. Diese Namen werden nicht als Maschinen-`x/y`
   interpretiert.
2. `StemHorizontalCurvaturePayload.x_mm/y_center_mm` sind kalibrierte Anlagenkoordinaten
   in der X/Y-Referenzebene. Diese entsprechen fachlich Maschinen-`x/y`, enthalten aber
   noch kein direkt gemessenes `z`.
3. `StemSurfacePhasePayload.cumulative_phase_deg` ist relativ zur versionierten
   `rotation_reference_view` und `positive_rotation` kalibriert. Es ist nicht ohne
   Prüfung automatisch die positive Rechtsschraubenrotation um `+x`.

Das geplante `canonical_phase_deg(...)` bildet Blickrichtung und konfigurierte physische
Positivrichtung explizit auf die Rechtsschraubenkonvention ab. Die Abbildung muss mit
vier tabellarischen Tests abgedeckt werden:

- Blick in Förderrichtung + Uhrzeigersinn,
- Blick in Förderrichtung + Gegenuhrzeigersinn,
- Blick gegen Förderrichtung + Uhrzeigersinn,
- Blick gegen Förderrichtung + Gegenuhrzeigersinn.

Kann die Kombination nicht eindeutig aufgelöst oder stimmen Prozess- und
Phasenkalibrierung nicht überein, bleibt das Modell ungültig und es wird keine Gradzahl
ausgegeben.

## 5. Ziel-Datenfluss

```text
bestehende Kamera-/Masken-/Kalibrierkette
            |
            +--> StemHorizontalCurvaturePayload --+
            +--> StemSurfacePhasePayload ----------+--> analysis.stem_slice_model
            +--> aktiver Track + Bewegung ----------+         |
                                                          eingefrorenes
                                                           Scheibenmodell
RuntimeChannel.stem_rotation_priority_control                    |
            |                                                    |
            +--> analysis.stem_rotation_priority_adapter --------+-->
                                      analysis.stem_rotation_optimizer
                                                  |
                         +------------------------+----------------------+
                         |                        |                      |
                  typisiertes Result        Overlay/View Model     Diagnose/Replay
                         |                        |                      |
                  output.result       output.overlay/display      Result History +
                                                                Temporal Recorder
```

Der Slice-Model-Node friert Messbeobachtungen am bestehenden Curvature-Freeze-Gate ein.
Bis zum Decision-Gate darf die gültige Phase weiterlaufen, damit das Modell in die
tatsächlich aktuelle Entscheidungslage gedreht werden kann. Danach bleibt das Modell
unveränderlich. Ein Wechsel des Frontendmodus verändert nur die Optimierung dieses
Modells; er nimmt keine neuen Geometriebeobachtungen auf. In einer laufenden Kamera-
Pipeline erscheint das neu optimierte Ergebnis spätestens mit dem nächsten Frame nach
dem Control-Refresh.

## 6. Geplante Domänenverträge

Die neuen Pydantic-Verträge kommen in
`backend/packages/cv-runtime/src/cv_runtime/payloads.py` und werden in
`AnyRuntimePayload` aufgenommen.

### 6.1 `StemSliceModelPayload`

Vorgesehene Schema-ID: `stem.slice-model.v1`.

Pflichtinhalt:

- `session_id`, `track_id`, Mess-/Entscheidungsframe und Zustand,
- `coordinate_system = MACHINE_X_Y_Z_RIGHT_HANDED_V1`, Einheit `mm`,
- `current_pose_phase_deg` und explizite Phasen-/Vorzeichenkonvention,
- `raw_slices` und `processed_slices`, jeweils begrenzt auf die konfigurierte
  Stationszahl,
- je Scheibe mindestens `x`, `cy`, `cz`, `a`, `b`, `phi`, `quality`, `valid`,
- je Scheibe Fit-Support, Residuen, Condition Number und Reason Codes,
- gemeinsamer materialfester Evaluationsbereich,
- verwendete Framefolgen/Phasen und verworfene Beobachtungen,
- Prozess-, Intrinsic-, View- und Phasenkalibrierungs-IDs/-Hashes,
- `model_hash`, Algorithmus-/Preprocessingversion und globale Qualität,
- `frozen`, Freeze-/Decision-Zeitpunkt und Reason-Code-Historie.

Invarianten:

- `a >= b > 0`, `quality in [0,1]`, endliche Zahlen,
- `phi` kanonisch auf einen dokumentierten 180°-Bereich normiert,
- keine gültige Scheibe ohne ausreichenden Phasensupport und stabile Fits,
- kein `frozen=true` ohne gültige Kalibrierungsbindung und Entscheidungslage,
- ungültige Modelle tragen keine erfundenen `cz`-/Winkelwerte.

### 6.2 `StemRotationPriorityPayload`

Vorgesehene Schema-ID: `stem.rotation-priority.v1`.

Felder:

- `source`: `FRONTEND` oder später `PLC`,
- `mode`: `NONE`, `VORSCHNITT_KRITISCH`, `NACHSCHNITT_KRITISCH`,
- `control_revision`, `updated_at`, `updated_by`,
- `valid`, `reason_codes`.

`NONE`, fehlende Control-Daten oder ein noch nicht implementierter PLC-Adapter liefern
ein ungültiges Payload und damit keine Gradzahl.

### 6.3 `StemRotationOptimizationPayload` und eingefrorenes Ergebnis

Vorgesehene Schema-ID des fachlichen Ergebnisses:
`stem.rotation-optimization.v1`.

Mindestens enthalten:

- `algorithm = PLAN_INDEPENDENT_SYMMETRIC_CHORD`, Algorithmusversion,
- `session_id`, `track_id`, `model_hash`, `config_hash`,
- `priority_source`, `priority_mode`, `control_revision`,
- `valid`, `decision = ROTATE | NO_ROTATION | UNCERTAIN`,
- `delta_angle_deg`, `gain_mm`, `should_rotate`,
- `score_current_mm`, `score_optimal_mm`,
- `limiting_x_current_mm`, `limiting_x_optimal_mm`,
- Modell-/Fit-/Ergebnisqualität und Reason Codes,
- Blick-/Positivrichtungs- sowie kanonische `+x`-Konvention,
- Entscheidungszeitpunkt, Frame, Deadline- und Freeze-Provenienz,
- begrenzte lokale Scorekurve und ein versioniertes View Model,
- `shadow_mode=true` und `plc_writes_suppressed=true` in der ersten Phase.

Bei ungültiger Priorität, ungültigem Modell, nicht eindeutiger Winkelabbildung oder zu
geringer Qualität sind `delta_angle_deg` und `gain_mm` `null`. Bei gültigem Modell, aber
unterschrittener Mindestverbesserung lautet die Entscheidung `NO_ROTATION` mit
`delta_angle_deg=0` und `should_rotate=false`. `gain_mm` bleibt gemäß Spezifikation der
nichtnegative potenzielle geometrische Gewinn des besten Kandidaten gegenüber der
aktuellen Lage; `optimal_candidate_angle_deg` hält dessen Winkel nur als Diagnose fest.
Das Frontend darf diesen Wert nicht als tatsächlich realisierten Gewinn darstellen.

Das Schema bleibt getrennt von `stem.orientation.v3`; dessen Semantik
`CURVATURE_PHASE_MINIMUM` darf nicht verändert werden.

## 7. Mess- und Scheibenmodellbildung

### 7.1 Kanonische Projektionsbeobachtung

Das neue reine Zwischenmodell `StemProjectionObservation` wird nicht als zweiter
Kameravertrag publiziert. Der Runtime-Adapter erzeugt es aus drei synchronen Inputs:

- gültiges `StemHorizontalCurvaturePayload`,
- gültiges, gelocktes `StemSurfacePhasePayload` desselben Tracks und Frames,
- `active_stem_tracking` desselben Tracks für Bewegungs-/Positionsgates.

Es enthält kanonische Phase, Prozess-X/Y-Profil, Breite, Gewichte,
Kalibrierungsbindungen, Framefolge sowie eine Pose-/Registrierungsdiagnose.

Akzeptanzbedingungen:

- gleiche `track_id` und kompatible Framefolge,
- identischer `process_calibration_hash`,
- unveränderte `trace_generation`, gültige und eindeutige Phasenkonvention,
- Eligibility angenommen, Curvature-Profil gültig und vergleichbar,
- ausreichende Geometrie-/Phasen-/Trackingqualität,
- kein Trackwechsel, Reset, unzulässiger Framegap oder Messung hinter dem Freeze-Gate,
- materialfeste Stationszuordnung nach Abschnitt 7.2.

### 7.2 Materialfeste X-Registrierung

`StemHorizontalCurvaturePayload.x_mm` ist Anlagen-X. Bei einer Längsbewegung bezeichnet
derselbe Zahlenwert in zwei Frames nicht dieselbe Holzscheibe. Die neue Implementierung
darf daher niemals Profile nur nach gleichem `x_mm` gruppieren.

Die implementierte Strategie `TRACKED_SIGNED_MATERIAL_X_V1` entspricht dem realen Ablauf:

1. Projektionen werden nur während des natürlichen Rollens in der vorhandenen
   `process_zone=transfer` angenommen.
2. Der bestehende Mantelphasen-Tracker akkumuliert neben der Rollphase seine signierte
   Bildtranslation. Die vorhandene Prozesshomographie projiziert diese am Stammzentrum nach
   Maschinen-X/Y; es entsteht kein zweiter Tracker.
3. Für eine Materialstation gilt
   `x_machine = material_origin_machine + x_material + signed_x_shift`.
4. Der gemeinsame sichtbare Schnitt wird in Material-X geführt. Schritt und gesamte
   Auslenkung sind konfiguriert und werden bei unplausiblen Werten fail-closed abgelehnt.
5. Bereits die Bestätigungsframes einer beginnenden Längsbeschleunigung werden nicht
   gesammelt. Mit bestätigt erreichtem `longitudinal_conveyor` friert das Modell ein.

Die Maskencentroid-Geschwindigkeit bleibt Diagnose- und Phasenzoneninput. Sie wird nicht als
Materialposition verwendet, weil Rollen, Silhouettenänderung und Perspektive ihren Wert
beeinflussen können. Im Längstransport wird das Scheibenmodell nicht weitergeführt.

### 7.3 Translation und Mittelpunktidentifizierbarkeit

Eine transversale Translation des gesamten Stamms verändert `y_center_mm` ebenso wie die
Rotation eines Mittelpunktversatzes. Der aktuelle Tracker meldet zwar `delta_y_mm`, aber
dieser Wert stammt aus der Maskencentroid und enthält potenziell selbst Form-/
Rotationsanteile. Ein ungeprüftes Subtrahieren würde `cy/cz` verfälschen.

Die Längskomponente ist mit `TRACKED_SIGNED_MATERIAL_X_V1` implementiert. Eine globale
transversale Verschiebung in der gemessenen Querrichtung bleibt dagegen mit dem
Mittelpunktversatz gekoppelt und wird nicht ungeprüft aus der Maskencentroid subtrahiert.

Die translationinvarianten Centerline-Residuen können bereits Bogenrichtung und
-änderung stützen, ersetzen aber nicht den absoluten Mittelpunktbezug zur Maschinenachse.
Insbesondere darf ein globales `cz` nicht auf null gesetzt oder aus einem Bild geraten
werden.

### 7.4 Fit je Scheibe

Für jede materialfest registrierte Station wird robust und gewichtet gefittet.

Für den Mittelpunkt in der aktuellen Entscheidungskoordinate gilt bei relativer
Beobachtungsphase `theta_j`:

```text
theta_j = canonical_phase_j - canonical_phase_at_decision
```

Damit beschreibt das gefittete `c=(cy,cz)` bereits die eingefrorene aktuelle Lage, für
die der Optimizer später `delta_angle_deg=0` bewertet. Äquivalent darf zuerst in einer
anderen Referenzlage gefittet und das gesamte Rohmodell anschließend einmal mit der
nachgewiesenen Phasendifferenz in die Entscheidungslage gedreht werden.

```text
y_observed_j = offset0_j + cy * cos(theta_j) - cz * sin(theta_j)
```

Der stationspezifische konstante Projektionsversatz `offset0_j` ist nur ein
Fit-Nuisance-Parameter. Er wird nicht als rotierender Anteil in `cy/cz` übernommen.

Für die projizierte Halbausdehnung `h_j = width_j / 2` wird die symmetrische
Formmatrix `S` gefittet:

```text
h_j^2 = [cos(theta_j), -sin(theta_j)]
        * S
        * [cos(theta_j), -sin(theta_j)]^T
```

Das ist eine lineare gewichtete Schätzung der drei unabhängigen Matrixkomponenten.
Eigenwerte/-vektoren liefern `a^2`, `b^2` und `phi`. Vorzeichen und Winkel werden nicht
per Formelannahme allein freigegeben, sondern durch Vorwärtsprojektion synthetischer
Ellipsen getestet.

Robuste Iteration, Mindestphasenspanne, Mindestanzahl voneinander getrennter
Phasenstützstellen, Matrixkondition, positive Definitheit, Residuen, Breitenplausibilität
und `a >= b` sind harte Qualitätsgates. Nahezu kreisförmige Scheiben dürfen gültig sein,
aber `phi` wird dann als nicht informativ markiert und darf die Gesamtqualität nicht
künstlich erhöhen.

### 7.5 Preprocessing des Rohmodells

Das Rohmodell und das verarbeitete Modell bleiben getrennt protokolliert. Die
Preprocessing-Reihenfolge ist fest und versioniert:

1. Rohfit auf dem registrierten X-Raster,
2. Scheiben mit unzureichendem Support/instabilem Fit ungültig markieren,
3. kurze Lücken bis `maximum_interpolation_gap_slices` interpolieren,
4. `cy`, `cz`, `a`, `b` robust räumlich glätten,
5. `phi` mit 180°-Periodizität entrollen, glätten und wieder kanonisieren,
6. `a >= b > 0`, Sprung-, Durchmesser-, Achsverhältnis- und Glättungsresiduen prüfen,
7. zusammenhängenden Evaluationsbereich nach `x_eval_start/end` bestimmen,
8. Modellqualität aus Mess-, Registrierungs-, Fit- und Abdeckungsqualität bilden.

Das Preprocessing darf ungültige lange Bereiche nicht überbrücken und keine verdeckten
Stammenden rekonstruieren.

## 8. Reiner Geometriekern

`stem_rotation_types.py` definiert dafür die unveränderlichen, Pydantic-unabhängigen
Dataclasses `StemSliceGeometry`, `StemSliceModel`, `PriorityAxis`,
`StemRotationScore` und die zugehörigen Diagnosewerte. Die Runtime-Payloads sind nur
serialisierbare Adapter auf diese Coretypen.

`stem_rotation_geometry.py` enthält keine Runtime-, OpenCV-, Datenbank- oder
Frontend-Abhängigkeit. Seine API arbeitet nur mit endlichen Zahlen und den Core-
Dataclasses.

Vorgesehene Funktionen:

- `rotation_matrix(angle_rad)`
- `rotate_slice(slice, delta_angle_rad)`
- `ellipse_quadratic_matrix(a, b, phi_rad)`
- `symmetric_usable_span(slice, axis)`
- `score_stem(slices, axis, x_range)`

`symmetric_usable_span` schneidet die Gerade `p(t)=t*u` durch die Maschinenachse exakt
mit der Ellipse. Nur wenn reelle Lösungen `t_neg <= 0 <= t_pos` existieren, ist
`G_i = 2 * min(t_pos, -t_neg)` gültig. Andernfalls ist der Span null und trägt einen
Reason Code für Achse außerhalb/keinen symmetrischen Bereich.

`score_stem` verwendet gemäß Spezifikation das Minimum aller gültigen, geglätteten
Scheiben im Evaluationsbereich. Ein konfigurierbares Quantil darf nur zusätzliches
Diagnosefeld sein und nicht unbemerkt das Minimum ersetzen.

## 9. Rotation Optimizer

`stem_rotation_optimizer.py` bleibt ebenfalls rein und deterministisch.

Vorgehen:

1. Kandidatenbereich über die 180°-Symmetrie bilden, auf die konfigurierten positiven
   und negativen mechanischen Drehgrenzen schneiden und `0°` immer aufnehmen.
2. Grobsuche mit konfigurierbarer Schrittweite, Startwert 1°.
3. Feinsuche um die besten Grobkandidaten.
4. Für jeden Kandidaten alle Scheiben gemeinsam drehen und den gewählten Achsenscore
   berechnen.
5. Zuerst den höchsten Score bestimmen. Kandidaten innerhalb `score_tolerance_mm` dieses
   Scores gelten als praktisch gleichwertig; daraus kleinsten absoluten Drehweg, dann
   stabilstes Plateau und optional höhere Qualität der limitierenden Scheiben wählen.
   Ein verbleibender exakter Gleichstand folgt einem versionierten Vorzeichen-Tie-break.
6. Plateauweite und lokale Scorekurve bestimmen.
7. `gain_mm = score_optimal_mm - score_current_mm` berechnen.
8. Unter `minimum_gain_mm` oder `minimum_rotation_deg` gültig `NO_ROTATION` ausgeben.
9. Bei unzureichendem Modell, breitem/mehrdeutigem Plateau oder Konventionsfehler
   `UNCERTAIN` ohne Winkel ausgeben.

Der Optimierer kennt nur die kanonische positive Rotation um `+x`. Die Darstellung
`CLOCKWISE/COUNTER_CLOCKWISE` für den konfigurierten Blickpunkt ist eine explizite
Ausgabeprojektion und verändert nicht das interne Vorzeichen.

## 10. Runtime- und Prioritätsintegration

### 10.1 Channel-Control statt Pipeline-Property

Geplante persistente Eigenschaft:

```text
RuntimeChannel.stem_rotation_priority_control JSON
  schema_version: runtime.stem-rotation-priority-control.v1
  source: FRONTEND
  mode: NONE | VORSCHNITT_KRITISCH | NACHSCHNITT_KRITISCH
  revision: integer
  updated_at: timestamp
  updated_by: user
```

Die Änderung erfolgt über den vorhandenen
`PUT /api/v1/runtime/channels/{channel_id}` und `api.channels.update`, ohne neuen Service
und ohne Runtime-Neustart. Dazu erhält der Create-/Update-Vertrag ein einfaches
`stem_rotation_priority_mode`; die Response liefert den vollständigen Control-Block.
`channel_store.update_runtime_channel` erhöht die Revision und der vorhandene Audit-Event
protokolliert Quelle, Modus und Revision. Default ist `NONE`, also fail closed.

`RealtimeRuntimeWorker._runtime_should_continue` liest den Channel bereits mindestens
sekündlich. Dort wird zusätzlich ein validierter Control-Snapshot aktualisiert und beim
Erzeugen des nächsten `RuntimeExecutionContext` als unveränderliche Metadata-Kopie
eingefügt. Damit ist die Auswahl dynamisch, ohne Pipelineversion oder Node-State zu
ändern.

### 10.2 Neue Nodes

`analysis.stem_rotation_priority_adapter`:

- Input: Frame als Ausführungstakt; später optional ein typisierter PLC-Read-Payload.
- Output: `StemRotationPriorityPayload`.
- Abhängigkeit: nur Context-Metadata und Node-Konfiguration `priority_source`.
- Zustand: stateless.
- Verhalten: FRONTEND liest den Channel-Control-Snapshot; PLC bleibt bis zur späteren
  Implementierung fail closed.

`analysis.stem_slice_model`:

- Inputs: `stem_horizontal_curvature`, `stem_surface_phase`, `active_stem`, optional Events.
- Output: `StemSliceModelPayload` plus Diagnoseoverlay.
- Zustand: Track-/Session-lokale, begrenzte Beobachtungsliste in
  `context.state[node.node_key]`.
- Lifecycle: vorhandene Start-/Freeze-/Decision-Gates übernehmen; Track-, Trace- oder
  Kalibrierwechsel setzen zurück; am Decision-Gate wird genau ein Modell eingefroren.
- Realtime: zwingend in `_STATEFUL_PIPELINE_NODE_TYPES` eintragen.

`analysis.stem_rotation_optimizer`:

- Inputs: eingefrorenes `StemSliceModelPayload` und `StemRotationPriorityPayload`.
- Outputs: `StemRotationOptimizationPayload` und Overlay/View-Model.
- Zustand: stateless; gleicher Modellhash + andere Control-Revision erzeugt ein neues,
  deterministisches Optimierungsergebnis.
- Verhalten: nur eingefrorene Modelle optimieren; keine Masken oder Bilder lesen.

### 10.3 Result-History-Semantik

`output.result` erhält für die neue Vorlage explizite Projektionen auf alle fachlich
erforderlichen Ergebnisfelder. `builtin._result_from_payload` wird um den neuen
Optimization-Payload ergänzt.

Die einmalige Event-Erkennung in `realtime_executor.live` wird generalisiert, zum Beispiel
zu `_stem_decision_history_event_key`, und erkennt beide Schemfamilien. Für die neue
Funktion umfasst der Schlüssel mindestens:

```text
schema_version : session_id : model_hash : priority_mode : control_revision
```

Dadurch wird eine Entscheidung einmalig gespeichert, ein späterer Frontend-Moduswechsel
auf demselben Modell aber als eigene, auditierbare Neuoptimierung erfasst. Periodische
Frame-History darf das Ergebnis nicht duplizieren.

`runtime_execution_store._contains_decision_ready_stem_orientation` und die Result-
Typisierung werden auf einen neutraleren Stem-Decision-Begriff erweitert oder erhalten
einen getrennten `stem_rotation_optimization`-Typ. Bevorzugt wird der getrennte Typ,
damit Filter, Reviewsemantik und Shadow-Auswertung des Krümmungsminimums nicht vermischt
werden.

### 10.4 Strukturierte Laufzeit-Logs

Die neue Funktion verwendet den vorhandenen `cv_observability`-Kontext. Sie schreibt
keine vollständigen Scheibenmodelle oder Scorekurven in den normalen Logstrom; diese
gehören in Result History beziehungsweise Temporal Recorder. Vorgesehen sind zwei
begrenzte, versionierte Fachereignisse:

- `stem.slice-model.frozen.v1`: Session-/Track-/Frame-ID, Modell- und Konfigurationshash,
  Anzahl roher/verarbeiteter/gültiger Scheiben, Modellqualität, Kalibrierungs- und
  Rotationsreferenz sowie Reason Codes,
- `stem.rotation-optimization.selected.v1`: Quelle, Modus und Control-Revision,
  Modell-/Konfigurationshash, Entscheidungsphase, `delta_angle_deg`, `gain_mm`, aktuelle
  und optimale Scores, limitierende X-Stationen, Qualität, Entscheidung und Reason Codes.

Reset-, Invalidierungs- und Deadline-Ereignisse werden nur bei einem Zustandswechsel
geschrieben, nicht pro Frame. Die bereits vorhandenen Korrelationsfelder für Runtime,
Ausführung und Channel bleiben erhalten; Modusänderungen bleiben zusätzlich über das
bestehende Channel-Audit nachvollziehbar.

## 11. Visualisierung und View Model

`stem_rotation_view_model.py` erzeugt aus Modell und Ergebnis ein kompaktes,
versioniertes `stem.rotation-optimization-view.v1`. Es enthält bereits alle
Darstellungskoordinaten in fachlichen Millimetern beziehungsweise normalisierten
Diagrammwerten:

- Entscheidung, Modus, Quelle, Winkel, Gewinn, Qualität, Gründe,
- Längsansicht mit aktueller und optimierter Mittellinie,
- Markierung der limitierenden X-Position aktuell/optimal,
- aktueller und optimierter Grenzquerschnitt als Ellipsenparameter,
- Maschinenachsen und aktive Prioritätsachse,
- lokale `delta_angle_deg -> score_mm`-Kurve mit aktuellem/optimalem Marker,
- Raw-/Processed-Slice-Gültigkeit und Lücken als Diagnosestreifen.

Neue Frontenddateien:

- `frontend/apps/admin-web/src/features/stem-orientation/rotation-optimization-panel.tsx`
  für Live-Entscheidung und Modusauswahl,
- `frontend/apps/admin-web/src/features/stem-orientation/rotation-optimization-diagrams.tsx`
  für SVG-Längsansicht, Querschnitt und Scorekurve,
- zugehörige Vitest-Dateien im selben Ordner.

Integration:

- `channel-detail-page.tsx` zeigt die Auswahl nur bei einer Pipeline mit
  `analysis.stem_rotation_optimizer`.
- Bei `priority_source=FRONTEND` sind die beiden fachlichen Modi auswählbar; `NONE` wird
  als „keine Priorität gewählt“ angezeigt und erzeugt keine Empfehlung.
- Bei späterem `priority_source=PLC` wird die Auswahl read-only und Quelle/effektiver
  Wert werden sichtbar.
- `result-history-page.tsx` erhält einen getrennten Renderer für
  `stem.rotation-optimization.v1`; die bestehende V2/V3-Reviewmaske wird nicht ohne
  angepassten Fachvertrag wiederverwendet.
- Der Pipeline Designer kann dasselbe Panel für Draft Preview verwenden, zeigt bei
  fehlender Mehrframehistorie aber explizit „Replay/Live-Sequenz erforderlich“.

## 12. Diagnostik und Replay

### 12.1 Result- und Artefaktumfang

Kompakt inline in Result History:

- Entscheidung und Control-Provenienz,
- aktuelle/optimale Scores, Winkel und Gewinn,
- limitierende Stationen,
- Modell-/Config-/Kalibrierungshashes,
- Modellqualität und Reason Codes,
- begrenzte Scorekurve und kompaktes View Model.

Als referenzierte Recorderartefakte:

- `projection-observations.jsonl`,
- `slice-model-raw.json`,
- `slice-model-processed.json`,
- `optimization-result.json`,
- `optimization-score-curve.json`,
- `calibrations.json`,
- optional rektifiziertes und Overlay-Video,
- Manifest mit Hashes, Versionen, Track, Framegrenzen und Ausschlussgrund.

### 12.2 Erweiterung des vorhandenen Recorders

`TemporalSampleRecorderConfig.profile` erhält eine weitere Variante
`rotation_optimization_diagnostics`. Extraktion und Materialisierung werden in
`backend/packages/cv-runtime/src/cv_runtime/temporal_samples.py` ergänzt;
`backend/services/realtime-executor/src/realtime_executor/temporal_samples.py` erkennt
den neuen Slice-Model-Node. Capture-Service, Speichergrenzen, Retention und
Artefaktreferenz-Update bleiben unverändert.

Es wird kein automatischer Label-Studio-Import für diese technischen Replayartefakte
aktiviert.

### 12.3 Replay-Werkzeug

`scripts/dev/replay_stem_rotation_optimization.py` lädt entweder ein gespeichertes
Scheibenmodell oder das neue Diagnosemanifest und ruft dieselben reinen
Preprocessing-/Geometrie-/Optimizerfunktionen auf. Es gibt JSON mit Ergebnis,
Modellhash, Config-Hash und Scorekurve aus. Es startet keinen Server und implementiert
keine zweite Runtime.

Der erste Regressionstestbestand wird synthetisch erzeugt. Echte Sequenzen müssen bei
der Zielserver-Inbetriebnahme mit freigegebenem Recorder aufgenommen, pseudonymisiert,
über den vorhandenen DVC-/Datasetpfad versioniert und mit einer unabhängigen Referenz
versehen werden.

## 13. Konkreter Dateiänderungsplan

| Reihenfolge | Datei/Komponente | Aufgabe | Inputs | Outputs/Abhängigkeiten |
|---|---|---|---|---|
| 1 | `backend/packages/cv-runtime/src/cv_runtime/stem_rotation_types.py` (neu) | unveränderliche Core-Dataclasses und Achsen-/Diagnosetypen | endliche Fachwerte | gemeinsame, Pydantic-unabhängige Typen für Messung, Geometrie und Optimizer |
| 2 | `backend/packages/cv-runtime/src/cv_runtime/stem_rotation_geometry.py` (neu) | Ellipsenmatrix, virtuelle Drehung, exakte symmetrische Sehne, Stammminimum | Core-Slices, Achse, Winkel | reine Zahlen/Diagnosen; nur stdlib + NumPy |
| 3 | `backend/packages/cv-runtime/src/cv_runtime/stem_rotation_optimizer.py` (neu) | Grob-/Feinsuche, Tie-break, Plateau, Gain/Deadband | verarbeitetes Modell, Priorität, Config | reines Optimierungsergebnis + Scorekurve |
| 4 | `backend/packages/cv-runtime/src/cv_runtime/stem_slice_measurement.py` (neu) | synchrone Projektionsbeobachtung, Phasenkonvertierung, Stationsregistrierung, robuste Slice-Fits | Curvature-/Phasewerte, Trackbewegung, Config | Rohscheiben + Fitdiagnose |
| 5 | `backend/packages/cv-runtime/src/cv_runtime/stem_slice_preprocessing.py` (neu) | Raster, kurze Lücken, robuste Glättung, Phi-Unwrap, Plausibilität | Rohscheiben | verarbeitete Scheiben + Qualitätsbericht |
| 6 | `backend/packages/cv-runtime/src/cv_runtime/stem_rotation_view_model.py` (neu) | einziges Backend-View-Model für Live/History/Replay | Modell + Ergebnis | `stem.rotation-optimization-view.v1` |
| 7 | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` | neue Slice-, Prioritäts-, Ergebnis- und View-Model-Verträge; `AnyRuntimePayload`; Adapter zu/von Coretypen | reine Domainwerte | validierte Runtime-Payloads |
| 8 | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_slice_model.py` (neu) | zustandsbehaftete Session-/Messintegration, Freeze | Curvature, Phase, Active Stem, Events | `StemSliceModelPayload`, Overlay |
| 9 | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_rotation_priority.py` (neu) | FRONTEND-Control in typisiertes Payload adaptieren | Context-Metadata, Taktframe | `StemRotationPriorityPayload` |
| 10 | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_rotation_optimizer.py` (neu) | reinen Optimierer auf Runtime-Payloads anwenden | Slice Model + Priority | Optimization-Payload + Overlay |
| 11 | `backend/packages/cv-runtime/src/cv_runtime/registry.py` | drei Node-Typen, Ports, Properties, Constraints registrieren | Handlerreferenzen | Node-Katalog/Designer |
| 12 | `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` | neues Optimization-Payload nach `ResultPayload` projizieren | Optimization-Payload | vorhandener Outputpfad |
| 13 | `backend/services/realtime-executor/src/realtime_executor/live.py` | Slice-Node stateful; Control-Snapshot; semantischen History-Key erweitern | Channelrow, Pipelineergebnis | geordneter State, Context-Metadata, einmalige History |
| 14 | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` | Channel-Control Request/Summary und Prioritätsmode | API-JSON | OpenAPI-/Frontend-Vertrag |
| 15 | `backend/packages/cv-db/src/cv_db/models.py` und `backend/migrations/versions/20260820_0036_stem_rotation_priority_control.py` (neu) | JSON-Controlfeld mit fail-closed Default | Migration | persistente, update-/rollbackfähige Auswahl |
| 16 | `backend/packages/cv-db/src/cv_db/channel_store.py` | Control erstellen, validieren, Revision erhöhen, Summary ausgeben; keine Runtime-Neustartanforderung | Create/Update Request | RuntimeChannelDetail |
| 17 | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` | vorhandenen Channel-Auditpayload um Priorität ergänzen | Updateergebnis | Audit-Event |
| 18 | `backend/packages/cv-db/src/cv_db/pipeline_store.py` | neue Templateprojektionen/-vorlage; gemeinsamen Messkettenbuilder extrahieren | Registrytypen | versionierbare Pipelinevorlage |
| 19 | `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` | neuen Resulttyp sicher erkennen | Historyprojektion | `stem_rotation_optimization` |
| 20 | `backend/packages/cv-runtime/src/cv_runtime/temporal_samples.py` | Diagnoseprofil und Artefakte | Frame + RuntimeResult | Spool/Manifest |
| 21 | `backend/services/realtime-executor/src/realtime_executor/temporal_samples.py` | Recorder aus neuer Nodeconfig aktivieren | Pipelinegraph | vorhandener asynchroner Recorder |
| 22 | `scripts/dev/replay_stem_rotation_optimization.py` (neu) | Offline-Replay über dieselbe Core-API | Modell-/Manifestpfad, Config | JSON-Report |
| 23 | `frontend/packages/schemas/src/index.ts` | generierte/abgeleitete neue Runtime-Verträge | OpenAPI | TypeScript-Typen |
| 24 | `frontend/packages/api-client/src/index.ts` | vorhandenes Channel-Update nutzt den erweiterten Vertrag | Mode-Update | RuntimeChannelDetail |
| 25 | neue Frontendkomponenten aus Abschnitt 11 | Bedienung und reine Darstellung | Channel/Live-/Historydaten | vorhandene Admin-Web-App |
| 26 | `frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx` | Modusauswahl und Livepanel einbinden | Channel + Overlay | Bedienansicht |
| 27 | `frontend/apps/admin-web/src/features/results/result-history-page.tsx` | getrennte Historydarstellung | ResultRecord | auditierbare Ergebnisansicht |
| 28 | `deployment/export-windows-update.ps1` und Installer-Prüfungen nur falls nötig | neue Runbook-/Schemaartefakte in bestehendem Bundle validieren | Repoartefakte | vorhandenes Offline-Update |

Die Dateien 1 bis 3 sind der direkte Einstieg für den nächsten Codex-Lauf. Sie benötigen
keine offene Runtime-/Kameraentscheidung und können vollständig synthetisch getestet
werden. Danach folgen Verträge und Slice-Fit.

## 14. Konfiguration

Die Registry-Konfiguration des Slice-Model-Nodes soll mindestens enthalten:

- `slice_spacing_mm` (Startwert 50, Bereich 25..100),
- `minimum_observations_per_slice`,
- `minimum_phase_span_deg`,
- `minimum_phase_separation_deg`,
- `maximum_observations`,
- Geometrie-/Phase-/Tracking-Mindestqualitäten,
- `maximum_material_x_step_mm` (Startwert 75 mm),
- `maximum_material_x_excursion_mm` (Startwert 150 mm),
- robuste Fitparameter und maximale Condition Number,
- maximale Center-/Width-/Axis-Ratio-Residuen,
- `max_interpolation_gap_mm` (intern deterministisch auf Raster-Slices abgebildet),
- räumliche Glättungsfenster/-stärken getrennt nach Center, Achsen und Phi,
- `x_eval_start_mm`, `x_eval_end_mm` beziehungsweise relative Innenfraktion,
- `material_registration_mode=TRACKED_SIGNED_MATERIAL_X_V1`,
- `debug_overlay`, `record_session_artifacts` und Recordergrenzen.

Der Optimizer-Node soll mindestens enthalten:

- `coarse_angle_step_deg`, `fine_angle_step_deg`, `fine_search_half_width_deg`,
- kanonischer Kandidatenbereich über 180°,
- `rotation_positive_limit_deg`, `rotation_negative_limit_deg`,
- `score_tolerance_mm`,
- `min_gain_mm`, `min_rotation_deg`,
- Plateau-/Eindeutigkeitsgrenzen,
- `min_model_quality`, `min_valid_slice_ratio` und eine Mindestzahl gültiger Scheiben,
- deterministische Tie-break-Regel,
- `priority_source=FRONTEND` für die erste Phase,
- `shadow_mode_enabled=true` und keine SPS-Freigabe.

Alle Defaults müssen fail closed sein und über Registrytests abgesichert werden.

## 15. Teststrategie

### 15.1 Reine Geometrie

Neue Datei `tests/unit/backend/test_stem_rotation_geometry.py`:

- zentrierter Kreis: gleicher Span in jeder Richtung und jedem Winkel,
- zentrierte Ellipse: analytische Haupt-/Nebenachsenspans,
- verschobener Kreis und verschobene Ellipse,
- Maschinenachse auf Rand, innerhalb und außerhalb,
- tangentiale/negative Diskriminante und numerische Epsilonfälle,
- Rotation von Mittelpunkt und Ellipsenachse gemeinsam,
- `y`-/`z`-Priorität exakt vertauscht,
- Scoreminimum und limitierende X-Station,
- keine Eingabemutation und deterministische Ergebnisse.

Neue Datei `tests/unit/backend/test_stem_rotation_optimizer.py`:

- Optimum bei 0°, positivem und negativem Winkel,
- 180°-Periodizität und Wrap,
- Grob-/Feinsuche gegen dichte Referenzsuche,
- deterministischer Tie-break und Plateau,
- Gain und Mindestverbesserung; unterhalb der Schwelle bleibt der potenzielle Gain als
  Diagnose erhalten, während `delta_angle_deg=0` und `should_rotate=false` gelten,
- `NONE`, ungültige Scheiben, geringe Qualität und mehrdeutiges Optimum ohne Gradzahl,
- VORSCHNITT bewertet nur `z`, NACHSCHNITT nur `y`.

### 15.2 Slice-Fit und Preprocessing

Neue Dateien
`tests/unit/backend/test_stem_slice_measurement.py` und
`tests/unit/backend/test_stem_slice_preprocessing.py`:

- synthetische bekannte `(cy, cz, a, b, phi)` vorwärts in viele Phasen projizieren und
  innerhalb Toleranz rekonstruieren,
- alle vier Blick-/Drehrichtungskombinationen,
- unregelmäßige Phasenabstände, Ausreißer und gewichtete Beobachtungen,
- zu geringe Phasenspanne/Condition Number fail closed,
- Kreisfall mit nicht informativer `phi`,
- materialfeste X-Registrierung und Ablehnung längsbewegter/mehrdeutiger Profile,
- Translation oberhalb Grenzwert fail closed,
- kurze versus lange Lücken,
- Phi-Unwrap an der 180°-Grenze,
- kein Erfinden verdeckter Enden oder absoluten `cz`.

### 15.3 Runtime und Persistenz

Bestehende Testkonventionen erweitern:

- `test_runtime_stem_slice_model.py`: Track-/Trace-/Kalibrierreset, Frame-Synchronität,
  Freeze, Decision-Gate, bounded State, Ergebniswiederholung,
- `test_runtime_stem_rotation_optimizer.py`: Payload-/Reason-Code- und Overlaymapping,
- `test_runtime_pipeline_definition_system.py`: Registryports, Statefulness,
  Vorlagenkanten und Resultprojektionen,
- Realtime-Test: neuer Node steht in `_STATEFUL_PIPELINE_NODE_TYPES`, Control-Refresh
  ändert nur den Optimizer und nimmt keine neue Messung auf,
- Channel-Store/API-Test: `NONE`-Default, Revisionsinkrement, Audit und kein Restart,
- Migrationstest: Upgrade/Downgrade sowie bestehende Channels erhalten validen Default,
- Result-Service-Test: neuer Resulttyp, einmalige Speicherung je Revision, keine
  Vermischung mit V3-Reviews/Shadowauswertung,
- Temporal-Recorder-Test: Manifest/Hashes/Artefaktreferenzen und Ausschlussfälle,
- SPS-Test: Optimization-Payload wird vom bestehenden Schreibgate abgelehnt.

### 15.4 Frontend

- Modusauswahl nur für passende Pipeline und FRONTEND-Quelle,
- `NONE` und API-Fehler sichtbar/fail closed,
- PLC-Quelle read-only,
- gleicher Modellhash, neue Revision und neu gerendertes Ergebnis,
- korrekte Achsenbeschriftung `y` horizontal/`z` vertikal,
- Winkel/Gain nur bei gültigem Resultat,
- Längsansicht, Grenzquerschnitt, limitierende X-Stelle und Scorekurve aus View Model,
- Historyrenderer erkennt das neue Schema getrennt von V2/V3.

### 15.5 Vorgesehene lokale Prüfkommandos

Nach den jeweiligen Implementierungsphasen:

```text
uv run pytest tests/unit/backend/test_stem_rotation_geometry.py \
  tests/unit/backend/test_stem_rotation_optimizer.py \
  tests/unit/backend/test_stem_slice_measurement.py \
  tests/unit/backend/test_stem_slice_preprocessing.py

uv run pytest tests/unit/backend/test_runtime_stem_slice_model.py \
  tests/unit/backend/test_runtime_stem_rotation_optimizer.py \
  tests/unit/backend/test_runtime_pipeline_definition_system.py \
  tests/unit/backend/test_temporal_sample_recorder.py

uv run ruff check backend/packages/cv-runtime backend/packages/cv-contracts \
  backend/packages/cv-db backend/services/realtime-executor \
  backend/services/runtime-admin-api tests

uv run python scripts/dev/export_openapi.py runtime-admin-api.v1.yaml
pnpm lint:frontend
pnpm test:frontend
pnpm build:admin-web
```

Auf dem Zielserver folgen Replay gegen aufgenommene Sequenzen, Kamerakalibrierungsprüfung,
Vorzeichen-/Blickrichtungsabnahme, Deadline-/Latenzmessung und Shadowvergleich. Eine
produktive SPS-Schreibung ist kein Abnahmeschritt dieser Phase.

## 16. Repository-belegte Risiken und offene Punkte

| Punkt | Befund | Konsequenz/Gate |
|---|---|---|
| Kein direktes Z-Messsignal | Prozesshomographie misst X/Y; Z ist nur Referenzrichtung. | `cz`, `b` und `phi` nur aus ausreichend vielen kalibrierten Projektionen; sonst kein gültiges Modell. |
| Material-X nicht vorhanden | Profil-`x_mm` ist Anlagen-X. | V1 nur bei geringer Längsbewegung plus geprüfter Registrierung; sonst eigener Pose-Adapter vor Freigabe. |
| Translation/Rotation nicht sicher getrennt | Trackerbewegung basiert auf Maskencentroid. | Initial nur nachgewiesene feste Rotationsachse; kein ungeprüftes Translation-Abziehen. |
| Referenzebenenhomographie | Ein Stamm liegt oberhalb der kalibrierten X/Y-Ebene; Perspektive kann Breiten in mm verzerren. | Synthetische Tests reichen nicht; Referenzkörper über Durchmesser/Höhenbereich auf Zielkamera vermessen und Korrekturkalibrierung nur bei belegtem Bedarf ergänzen. |
| Winkelkonventionen unterscheiden sich | Phase ist relativ zu Blickpunkt und CLOCKWISE/COUNTER_CLOCKWISE; Spezifikation fordert Rechtsschraube um `+x`. | Expliziter, versionierter Adapter und Vier-Kombinationen-Test vor jeder Gradfreigabe. |
| State ist flüchtig | `RuntimeExecutionContext.state` übersteht keinen Neustart; es gibt keinen allgemeinen Finalize-Hook. | Neustart verwirft Session fail closed; nur eingefrorene Result-/Recorderartefakte sind persistent. |
| Kein SPS-Read-Adapter | Nur Siemens-Schreiben und HTTP-Trigger wurden gefunden. | Frontend ist einzige implementierte Prioritätsquelle; PLC bleibt eigener Auftrag. |
| Immutable Pipelineconfig | Nodeproperty kann nicht als schneller Bedienwert dienen. | DB-backed Channel-Control und Context-Snapshot verwenden. |
| Bestehender V3-Algorithmus ist anders | `CURVATURE_PHASE_MINIMUM` fitttet skalare Krümmung, kein Slice-/Sehnenmodell. | Eigene Schema-ID, Resulttyp, UI und Shadowauswertung; keine stille Migration. |
| History/Review ist V2/V3-spezifisch | Extraktion und technische Reviews erkennen nur `stem.orientation.v2/v3`. | Neue Resulterkennung getrennt ergänzen; V3-Reviewsemantik nicht automatisch übernehmen. |
| Keine portablen Real-Replays | Keine eingecheckte Stammsequenz/kein Temporalmanifest. | Synthetische Tests zuerst; echte Zielserversequenzen vor fachlicher Freigabe aufnehmen und versionieren. |
| Serialisierte Pipeline | Stateful Nodes erzwingen inflight=1; Segmentierung und High-Rate-Phase teilen Budget. | Beobachtungslisten begrenzen, reine Fits vektorisieren, Latenz auf Zielhardware messen. |
| Entscheidungsfrist | Ergebnis muss vor erstem Vorzentrierungs-Walzenpaar bereitstehen. | Bestehende Gates/Deadlinefelder wiederverwenden; keine auf dem Entwicklungs-PC erfundene Latenzzahl. |

## 17. Abweichungen beziehungsweise Präzisierungen gegenüber der Fachspezifikation

1. Das Repository enthält kein fertiges `StemSlice`-Messpayload. Der neue Builder nutzt
   nicht direkt Pixelkonturen, sondern die vorhandenen metrischen horizontalen Profile
   plus Phase und Trackdiagnose. Das vermeidet doppelte Masken-/Kalibrierlogik.
2. Die vorhandene X/Y-Prozesskalibrierung liefert kein direktes Z. Das vollständige
   Ellipsenmodell ist daher eine zeitliche Rekonstruktion mit Qualitätsgates, keine
   Einzelbildmessung.
3. Anlagen-X muss vor einem Mehrframe-Slicefit in materialfestes X überführt werden.
   Diese im Altbestand fehlende Zuordnung wird als eigener Messschritt aufgenommen.
4. Transversale Stammtranslation und rotationsbedingter Mittelpunktversatz sind mit der
   heutigen Maskencentroid nicht automatisch trennbar. Die erste Freigabe setzt deshalb
   eine nachgewiesene feste Rotationsachse voraus; sonst bleibt das Modell ungültig.
5. Der manuelle Frontendmodus wird nicht als Pipelineproperty gespeichert, sondern als
   revisionsbehafteter Channel-Control-Wert. Nur so kann dasselbe eingefrorene Modell
   ohne neue Messung und ohne Pipeline-Neupublikation neu optimiert werden.
6. Die bestehende `stem.orientation.v3`-Entscheidung bleibt ein anderer Algorithmus. Die
   neue Funktion erhält ein eigenes Resultschema statt die Bedeutung von V3 zu ändern.
7. Eine spätere PLC-Prioritätsquelle ist mangels Read-Adapter noch nicht implementierbar.
   Die Schnittstelle wird vorbereitet, die Quelle aber fail closed gelassen.
8. Reale Replaydaten sind derzeit nicht als Repositoryfixture vorhanden. Der vorhandene
   Temporal-Recorder wird erweitert, bevor Zielserverdaten als Gold-/Regressionbestand
   verwendet werden.

## 18. Umsetzungsreihenfolge und Abnahmekriterien

### Phase A: Reiner Geometriekern

Implementiere `stem_rotation_geometry.py`, `stem_rotation_optimizer.py` und deren Tests.
Abnahme: alle analytischen/synthetischen Geometriefälle deterministisch grün; keinerlei
Runtime-, OpenCV- oder DB-Abhängigkeit.

### Phase B: Verträge und synthetischer Slice-Fit

Ergänze Payloads, `stem_slice_measurement.py` und `stem_slice_preprocessing.py`.
Abnahme: vorwärts projizierte synthetische Scheiben werden innerhalb festgelegter
Toleranzen rekonstruiert; unzureichende Phase, Bewegung und Konventionsfehler liefern
keine vollständigen Werte.

### Phase C: Runtime-Nodes und Pipelinevorlage

Implementiere Priority-Adapter, Slice-Model-Handler und Optimizer-Handler, Registry,
Stateful-Allowlist und eine neue Pipelinevorlage im bestehenden Store.
Abnahme: geordnete Session, Freeze am richtigen Gate, Decision vor dem ersten Walzenpaar,
Track-/Kalibrierwechsel fail closed, kein SPS-Node in der Vorlage.

### Phase D: Persistente Frontend-Priorität

Ergänze Contract, Alembic-Migration, Channel Store, Context-Refresh, Audit, OpenAPI und
Frontendauswahl.
Abnahme: laufender Channel übernimmt den Modus ohne Restart; dasselbe Modell wird bei
neuer Control-Revision neu optimiert; `NONE` erzeugt keinen Winkel.

### Phase E: Result History, View Model und UI

Ergänze Projektionen, semantischen History-Key, getrennten Resulttyp sowie Live-/History-
Diagramme.
Abnahme: Entscheidung und alle drei geforderten Visualisierungen stammen aus demselben
Backend-View-Model; keine Geometriemath im Browser; V3 bleibt unverändert.

### Phase F: Recorder und Replay

Erweitere den bestehenden Temporal-Recorder und das CLI-Replay.
Abnahme: ein synthetisches beziehungsweise Zielserver-Diagnosemanifest reproduziert
denselben Modellhash, Config-Hash, Winkel, Gain und Scorecurve.

### Phase G: Zielserver-Shadow-Inbetriebnahme

Kalibriere Blick-/Vorzeichenkonvention, prüfe feste Rotationsachse, Perspektivfehler,
Stationsregistrierung, Phasensupport, Latenz und Wiederholbarkeit mit echten Stämmen.
Versioniere freigegebene Sequenzen über den bestehenden DVC-/Datasetpfad.
Abnahme: definierte Goldfälle und Fehlerszenarien, Ergebnis vor Decision-Deadline,
SPS-Schreiben weiterhin deaktiviert.

### Spätere, ausdrücklich getrennte Phase: PLC-Quelle und Maschinenübergabe

Erst nach fachlicher Shadow-Freigabe einen typisierten PLC-Read-Adapter und gegebenenfalls
die Ausgabezuordnung implementieren. Richtung, Sequenz, Watchdog, Verfallszeit,
Interlocks und SPS-Grenzen benötigen eine eigene Sicherheits-/Abnahmefreigabe.

## 19. Definition of Done für die erste geometrische Shadow-Version

- Die Scheibenparameter entsprechen nachweislich Maschinen-`x/y/z` und derselben
  Zentrierachse.
- Ohne gültige Phasenspur, eindeutige Blick-/Vorzeichenabbildung, materialfeste
  Stationszuordnung und ausreichende Qualität wird keine Gradzahl ausgegeben.
- Beide Prioritätsmodi optimieren ausschließlich ihre jeweilige Achse.
- `delta_angle_deg=0` ist die eingefrorene aktuelle Entscheidungslage.
- `gain_mm` ist nur die Differenz der symmetrisch nutzbaren Geometriescores und wird nicht
  als Produktreserve bezeichnet.
- Das Ergebnis liegt vor dem ersten Vorzentrierungs-Walzenpaar; spätere Beobachtung ist
  getrennte Nachkontrolle und überschreibt die Entscheidung nicht.
- Ein Moduswechsel verwendet dasselbe Modell und ist über Revision/Audit nachvollziehbar.
- Bestehende Pipelines, V2/V3-Verträge, Training, Modellverwaltung und Deployments bleiben
  rückwärtskompatibel.
- Keine SPS-Schreiboperation wird durch die neue Funktion ermöglicht.
- Unit-, Registry-, Persistenz-, Recorder- und Frontendtests sowie statische Prüfungen
  sind grün; Live-/Kamera-/E2E-Nachweise sind auf dem Zielserver dokumentiert.
