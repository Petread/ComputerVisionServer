# Migrationsaudit: Stammausrichtung auf das Krümmungsminimum-Prinzip

Stand: 2026-08-04

Umfang: Bestandsanalyse und konkrete Migrationskarte; keine Produktivcodeänderung.

## 1. Ergebnis in Kurzform

Der aktuelle Stammausrichtungspfad ist ein kausaler, zustandsbehafteter Vor-Einzugspfad, aber noch **kein Krümmungsminimum-Solver**. Er verfolgt einen Stamm, misst pro Frame eine pixelbasierte Stammgeometrie, integriert kalibrierte relative Mantelflächenbewegung und wählt aus der beobachteten Folge einen Zielkandidaten. `evaluate_projected_pre_entry_baseline()` sucht dazu Nullstellen der signierten Biegung oder lokale Minima von `abs(signed_bow_normalized)` und rechnet deren beobachtete Rotation gegen den Decision-Frame. Das Resultat ist `stem.orientation.v2` mit `estimate_method="projected_baseline"`.

Für die verbindliche Zielarchitektur fehlen im Produktivcode:

- die projektive Anlagenkoordinaten-Kalibrierung mit Referenzebene und metrischen Prozessgates,
- `preprocessing.process_coordinate_rectify`,
- eine ausschließlich horizontale, projektive Y-Krümmung mit physikalischer Skala,
- eine Mantelflächenphase mit Keyframes und Re-Lock,
- die periodische Kurve „Krümmung über Rollphase“ samt Minimum, Maximum und Fitgüte,
- eine vom Ziel-Frame unabhängige `target_phase`,
- ein separates `curvature_freeze_gate` und ein kalibriertes `decision_gate`,
- ein Resultvertrag der nächsten Version mit `algorithm="CURVATURE_PHASE_MINIMUM"`,
- die entsprechende Kurven-/Phasendiagnostik in Preview und Result History.

Die Migration kann vollständig in der bestehenden Runtime-, Pipeline-, Result-, Recorder- und Admin-Web-Architektur erfolgen. Eine zweite API, Anwendung, Datenbank oder ML-Plattform ist weder nötig noch zulässig. TCN/GRU bleibt ein vorhandener optionaler Legacy-/Forschungspfad und ist keine V1-Abhängigkeit.

## 2. Gelesene Grundlagen und Vorrang

Gelesen wurden `AGENTS.md` und alle Repository-Dokumente, die Stammausrichtung, Stammbogen oder Stem Orientation behandeln:

- `docs/Stammausrichtung_Inferenzserver/Spezifikation_Produktivsystem_Stammbogen_Minimumprinzip.md`
- `docs/Stammausrichtung_Inferenzserver/Codex_Prompts_Produktivsystem_Stammbogen_Minimumprinzip.md`
- `docs/Stammausrichtung_Inferenzserver/Anleitung_Stammkruemmung_Inbetriebnahme.md`
- `docs/Stammausrichtung_Inferenzserver/Spezifikation.md`
- `docs/Stammausrichtung_Inferenzserver/Codex_Prompts_v2_Integration.md`
- `docs/stem-orientation/camera-calibration-persistence.md`
- `docs/stem-orientation/implementation-map.md`
- `docs/stem-orientation/prompt-05-change-report.md`
- `docs/stem-orientation/prompt-06-change-report.md`
- `docs/stem-orientation/temporal-angle-model-adr.md`

Bei Widersprüchen gilt die Minimumprinzip-Spezifikation. Insbesondere sind ein manuell gewähltes Zielintervall, „best vertical“ und `good / acceptable / bad` als Grundlage des Winkelziels für V1 überholt. Bestehende Verträge und Daten bleiben trotzdem lesbar.

## 3. Ist-Inventar

### 3.1 Runtime-Grundverträge

| Dateipfad | Symbol/Node | Ein-/Ausgang | State/Persistenz | UI | Tests und Befund |
|---|---|---|---|---|---|
| `backend/packages/cv-runtime/src/cv_runtime/context.py` | `RuntimeExecutionContext`, `RuntimeModelBinding` | Frame(s), Inference-/Modellauflösung und Metadaten in den Handler; kein eigener fachlicher Ausgang | `state: dict[str, Any]` ist der vorhandene, pro Pipeline-Lauf geteilte Zustandspfad | indirekt über Runtime Preview und Live Runtime | In vielen Runtime-Tests benutzt; für neue stateful Nodes unverändert geeignet |
| `backend/packages/cv-runtime/src/cv_runtime/handlers.py` | `RuntimeNodeHandler`, `NodeExecutionResult`, `assert_handler_compatible()` | `execute(context, node, inputs)` → typisierte `outputs`, Overlay-Primitiven, persistierte Resultate und Capture-Intents | kein eigener State; Handler benutzen `context.state` | keine direkte UI | Handlerkonvention ist vollständig wiederverwendbar |
| `backend/packages/cv-runtime/src/cv_runtime/loader.py` | `load_runtime_handler()`, `validate_runtime_handler_ref()` | Registry-Referenz `module:symbol` → kompatibler Handler | stateless | keine | dynamisches Laden ist ausreichend; keine feste Handlerliste zu migrieren |
| `backend/packages/cv-runtime/src/cv_runtime/engine.py` | `RuntimePipelineEngine.execute()` | validierter Graph und Port-Payloads → `RuntimeExecutionResult` | reicht denselben `RuntimeExecutionContext` an alle Nodes weiter | Preview und Live Runtime benutzen denselben Engine-Pfad | bestehende Graph-/Porttests decken die Ausführung ab |
| `backend/packages/cv-runtime/src/cv_runtime/payloads.py` | `StemGeometryPayload`, Schema `stem.geometry.v1` | pixelbasierte Referenzachse, Centerline, Residual-/Breitenprofile, signierte Krümmungs- und Bogenkennzahlen, Sichtbarkeit, Qualität, Fragmente, Provenienz | Payload selbst immutable; keine Session | Resultprojektion/Overlay indirekt | Serialisierung in `test_runtime_stem_geometry.py`; kein Anlagenkoordinaten- oder metrischer Krümmungsvertrag |
| derselbe Pfad | `StemRotationPayload`, Schema `stem.rotation.v1` | Delta- und kumulative unwrapped Grad, Konfidenz, Features/Inlier, Translation, Trace-Generation, Kalibrierprovenienz | Zustand liegt im Handler, nicht im Payload | aktuelle numerische Node-Felder; Result History zeigt kompakten Rotationstrace | `test_stem_rotation.py`, `test_runtime_stem_temporal.py`; keine Keyframes, Re-Lock- oder explizite `current_phase` |
| derselbe Pfad | `TemporalSequencePayload`, `TemporalPredictionPayload` | kausale Featurefolge beziehungsweise TCN/GRU-Vorhersage | Sequenz im Orientierungs-Handler begrenzt gepuffert | Pipeline Designer kann optionale Modellnodes konfigurieren | Temporal-Inference- und Trainingstests vorhanden; für V1 nicht erforderlich |
| derselbe Pfad | `RuntimeImageTransform` | Transformationen wie Identity, Resize, Crop, Mask, Normalize und Undistort; Koordinatenprojektion Identity/Scale/Crop/Affine/`not_projectable` | im Frame-Payload transportiert | Preview kann Transformationsketten anzeigen | Undistort-Tests belegen ausdrücklich: intrinsische Entzerrung ist keine projektive Referenzebenen-Abbildung |
| `backend/packages/cv-runtime/src/cv_runtime/registry.py` | `BUILTIN_NODE_TYPES`, `BUILTIN_NODE_TYPE_BY_KEY` | Node-, Port-, Property-, Output-Katalog- und Handlervertrag | `runtime_constraints.stateful` markiert zustandsbehaftete Nodes | bildet Node-Palette und Formfelder des Designers | `test_runtime_pipeline_definition_system.py` und nodespezifische Registrytests; keine der fünf neuen Minimumprinzip-Nodes außer dem schon vorhandenen Tracker ist registriert |

### 3.2 Fachliche Runtime-Nodes

| Dateipfad | Symbol/Node | Ein-/Ausgang | State | UI | Tests und Befund |
|---|---|---|---|---|---|
| `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` | `preprocessing_camera_undistort`; Node `preprocessing.camera_undistort` | `FramePayload` → intrinsisch entzerrter `FramePayload` plus Transformationsprovenienz | Remap-Cache in `context.state` | `CameraIntrinsicCalibrationEditor` | `test_runtime_camera_undistort.py`, `test_runtime_camera_undistort_validation.py`; **Keep**, aber nicht mit Prozess-Rektifizierung verwechseln |
| `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py` | `ActiveStemTrackerHandler`; Node `analysis.active_stem_tracker` | Segmente, optional Frame/Event/Trigger → genau ein aktives `SegmentPayload` plus Overlay | pro `node_key`: aktive Track-ID, BBox, Schwerpunkt, Achse, Geschwindigkeit, Prozessstufe, Lost-/Completed-Track-Zustand | generische ROI- und Zahlenfelder | `test_runtime_active_stem_tracker.py`; überbrückt kurzzeitige Okklusion nur für Identität, rekonstruiert keine Stammgeometrie |
| derselbe Pfad und `backend/packages/cv-runtime/src/cv_runtime/stem_geometry.py` | `StemMaskGeometryHandler`, `analyze_stem_geometry()`; Node `analysis.stem_mask_geometry` | aktives Segment, optional Frame → `StemGeometryPayload` und Overlay | stateless | generische Geometrie-/Qualitätsparameter | `test_stem_geometry.py`, `test_runtime_stem_geometry.py`; PCA-/Querschnittsgeometrie in Bildpixeln, ausdrücklich ohne 3D oder metrische Aussage |
| `backend/packages/cv-runtime/src/cv_runtime/stem_geometry.py` | `_largest_meaningful_component()` und Qualitätsgründe | binäre Maske → größte sinnvolle Komponente, Lochfüllung oder fail-closed | stateless | Gründe im Overlay/Result | `test_too_small_or_strongly_fragmented_masks_fail_closed`; kleinere Fragmente werden entfernt (`FRAGMENTS_REMOVED`), starke Fragmentierung wird `MASK_TOO_FRAGMENTED`; es gibt **kein** Hidden-Fragment-Merging |
| `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` | `StemRotationCalibrationProfile`, `calibrate_stem_rotation()`, `estimate_surface_rotation_from_matches()` | bekannte Winkelpositionen beziehungsweise Featurematches + Stammachse → gehashte Gradkalibrierung beziehungsweise relatives Winkeldelta | pure Kernfunktionen | Kalibrierprofil derzeit als viele flache Node-Felder, kein geführter Assistent | `test_stem_rotation.py`; robuste Translation-/Achsen-/Skalenkompensation und Ausreißerbegrenzung sind wertvolle Basis |
| `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` | `StemSurfaceRotationHandler`; Node `analysis.stem_surface_rotation` | entzerrter Frame + aktiver Stamm + `StemGeometryPayload` → `StemRotationPayload` und Overlay | vorheriges Grau-/Maskenbild, Achse, Frame/Timestamp, kumulative Rotation, Trace-Generation | Zahlen-/Textfelder für Kalibrierung und Qualitätsgates | `test_runtime_stem_temporal.py`; Shi-Tomasi/LK inkrementell, bei Trackwechsel/Lücke/Blur/Featureverlust Reset und neue Generation, aber keine Keyframes und kein Re-Lock |
| `backend/packages/cv-runtime/src/cv_runtime/stem_temporal.py` | `StemTemporalObservation`, `evaluate_projected_pre_entry_baseline()` | Folge aus kumulativer Rotation, signiertem normiertem Bogen, RMS und Qualität → Winkelvorschlag | pure Auswertung einer übergebenen Folge | keine direkte UI | `test_stem_temporal.py`; sucht beobachtete Bogen-Nullstellen oder lokales Minimum von `abs(signed_bow_normalized)`, nicht das periodische Krümmungsminimum |
| `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` | `StemTemporalOrientationHandler`; Node `analysis.stem_temporal_orientation` | Geometrie + Rotation + optional aktiver Stamm/Event → Result, optional `TemporalSequencePayload`, Overlay | Session, bounded observations/features, Trace-Flags, `target_candidates`, bester Zielkandidat, Decision-Frame, eingefrorenes Resultat, Post-Check | vier normierte Box-ROIs und viele Qualitäts-/Recorderfelder | `test_runtime_stem_temporal.py`; friert vor dem ersten Walzenpaar korrekt ein, aber die Prozessstufe entsteht nur durch Schwerpunkt-in-ROI und die Entscheidung bleibt Ziel-Frame-basiert |
| `backend/packages/cv-runtime/src/cv_runtime/nodes/temporal_inference.py` | `inference.temporal_sequence`, `analysis.stem_orientation_model_decision` | kausale Sequenz + optional Baseline → Shadow-Modellresultat/Entscheidung | stateful Sequenzentscheidung | Modellpicker im Designer | Temporal Runtime-/Deploymenttests; bestehender optionaler TCN/GRU-Pfad, nicht Bestandteil des Minimumprinzip-V1-Pfades |

### 3.3 Session-, Gate- und Resultverhalten

Der aktuelle `StemTemporalOrientationHandler` verwendet die konfigurierten normierten Boxen `observation_entry_roi`, `throw_in_roi`, `decision_roi` und `first_roller_roi`. `_process_stage()` prüft den Mittelpunkt des Trackers gegen diese Boxen und nimmt die höchste erreichte Stufe. Die Namen `process_zone_calibration_id` und `process_zone_calibration_version` liefern Provenienz, aber keine projektive Anlagenkoordinate und keine metrische Gate-Geometrie.

Es gibt weder `candidate_roi`, `curvature_observation_roi`, `surface_tracking_roi`, `session_start_gate`, `curvature_freeze_gate`, `decision_gate`, `decision_margin_px`, `process_reference_plane`, `target_phase` noch `surface_phase` im Produktivcode. Die gleichnamigen Treffer liegen ausschließlich in der neuen Spezifikation und den neuen Prompts.

Das aktuelle Resultat `stem.orientation.v2` enthält unter anderem Session-/Track-ID, `decision_ready`, Empfehlung, Konfidenz, `estimate_method`, `target_frame_or_interval_candidate`, Decision-Frame, kompakte Bogen-/Rotationstraces, Richtungs- und Versionsprovenienz sowie Reason Codes. Es enthält keine periodische Kurve, kein Minimum/Maximum, keine aktuelle/Zielphase und keinen Fitresidual.

### 3.4 Realtime Executor, Preview und Pipeline Designer

| Dateipfad | Symbol/Node | Ein-/Ausgang | State/Persistenz | UI | Tests und Befund |
|---|---|---|---|---|---|
| `backend/services/realtime-executor/src/realtime_executor/live.py` | `_STATEFUL_PIPELINE_NODE_TYPES`, `_pipeline_max_inflight()` | Pipelineframes → serialisierte Engine-Ausführung | ein `pipeline_state` pro Worker; stateful Graph begrenzt Inflight auf 1 | Live View/Diagnostik | `test_realtime_live_path.py`; Tracker, Rotation und temporale Orientierung sind korrekt stateful markiert; neue Phase und Solver müssen ergänzt werden |
| derselbe Pfad | `_history_result_persist_due()`, `_stem_orientation_history_event_key()` | eingefrorenes Resultat → einmaliges Result-History-Ereignis | Eventschlüssel je Session/Frame/Phase | Result History | prüft ausschließlich `stem.orientation.v2`; `stem.orientation.v3` würde aktuell nicht als Stem-Decision persistiert |
| `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` | `preview_channel_pipeline_graph()` | ein gespeicherter/Test-Frame + Autorengraph → Preview-Ergebnis | pro Request neuer `RuntimeExecutionContext`; danach State-Ende; `plc_writes_allowed=False` | Runtime Preview im Designer | Preview ist gut für Rektifizierung und Einzelbildkrümmung, aber nicht für mehrframeige Phase/Solver-Session |
| derselbe Pfad | `capture_channel_calibration_frame()`, `calculate_channel_camera_calibration()` | exklusiver Kameraframe beziehungsweise ChArUco-Beobachtungen → Intrinsikprofil | Aufnahme transient, angewendetes Profil im Node-Config/`PipelineVersion.graph` | Intrinsik-Assistent | Designer- und Undistorttests; kein Prozessreferenzebenen- oder Mantelphasen-Assistent |
| `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` | `NodeField`, `VisualBoxEditor`, `CameraIntrinsicCalibrationEditor`, `BoardSurfaceCalibrationEditor` | Registry-Properties + Previewbild → Node-Konfiguration | Draft im Frontend, versioniert beim Pipeline-Save/Publish | bestehender Pipeline Designer und Runtime Preview | `runtime-pipeline-designer-page.test.ts`; generische ROI-Bearbeitung und zwei strukturierte Kalibriereditoren sind wiederverwendbare Muster |
| `backend/packages/cv-db/src/cv_db/pipeline_store.py` | `_validate_node_properties()`, `_valid_camera_intrinsic_calibration()`, `_valid_board_surface_calibration()` | Pipelinegraph → normalisierter Graph und Publish-Validierung | Konfiguration in versioniertem `PipelineVersion.graph` | Validierungsfehler im Designer | Pipeline-Definitionstests; noch kein Validator für Prozess- oder Phasenkalibrierung |

### 3.5 Result History und Review

| Dateipfad | Symbol | Ein-/Ausgang | State/Persistenz | UI | Tests und Befund |
|---|---|---|---|---|---|
| `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` | `_result_type()`, `_contains_decision_ready_stem_orientation()` | Runtime-Ausgabe → `ResultRecord` und `result_type="stem_orientation"` | JSON-Felder im bestehenden PostgreSQL-Resultpfad | Result-History-Filter | erkennt nur `stem.orientation.v2` |
| `backend/packages/cv-db/src/cv_db/result_store.py` | `stem_orientation_result_from_record()`, Review-Funktionen | `ResultRecord` → Stemresultat; Review-Request → append-only Reviewrevision | vorhandene Tabellen `result_records`, `stem_orientation_reviews` | Result Detail/Review | aktuell nur `stem.orientation.v2`; Review wird für andere Versionen abgewiesen |
| `backend/packages/cv-contracts/src/cv_contracts/runtime.py` | `RuntimeStemOrientationReviewCreateRequest`, `RuntimeStemOrientationReview` | Plausibilität, Richtung, Winkel, Bogenrelevanz, verpflichtendes `final_position`, optionale Ziel-Frames | API-Vertrag, kein eigener Runtime-State | Reviewformular | Vertragstests indirekt; `StemReviewFinalPosition = Literal["good", "acceptable", "bad"]` |
| `frontend/apps/admin-web/src/features/results/result-history-page.tsx` | `ResultDetailPanel`, `StemOrientationResultSummary`, `StemOrientationReviewPanel` | Resultdetail + Reviews → kompakte Anzeige und neue Reviewrevision | React Query/API | zeigt Empfehlung, Decision-Frame, Zielkandidat, normierten Bogen und Rotation; Review fragt Ziel-Frame-Intervall ab | `result-history-page.test.tsx`; erkennt nur `stem.orientation.v2`, keine Krümmungs-über-Phase-Kurve |

### 3.6 Recorder, Label Studio, Dataset und Training

| Dateipfad | Symbol | Ein-/Ausgang | State/Persistenz | UI | Tests und Befund |
|---|---|---|---|---|---|
| `backend/packages/cv-runtime/src/cv_runtime/temporal_samples.py` | `TemporalSampleRecorderConfig`, `extract_temporal_sample_event()`, `TemporalSampleSpoolWriter`, `materialize_temporal_sample_spool()` | v2-Result + `stem_geometry` + `stem_rotation` + Frame → Spool, Videos, `features.jsonl`, `rotation-trace.jsonl`, Manifest | Filespool und materialisierte Capture-Artefakte | Result Asset-Links | `test_temporal_sample_recorder.py`; `_orientation_result()` akzeptiert nur v2, Defaultschema ist `stem_orientation_angle.v1`, Baseline-Prediction referenziert Ziel-Frames |
| `backend/services/realtime-executor/src/realtime_executor/temporal_samples.py` | `RealtimeTemporalSampleRecorder`, `temporal_recorder_config_from_graph()`, `attach_temporal_capture_plans()` | Runtimeframes/Resultate → bounded Queue, Finalisierung, Capture Intent und Asset-Referenzen | aktiver Sessionbuffer, Finalized-Session-IDs, Writer-Thread | keine separate Anwendung | Recorder-Konfiguration wird nur aus `analysis.stem_temporal_orientation` gelesen; muss für den neuen Solver kompatibel erweitert werden |
| `backend/packages/cv-datasets/src/cv_datasets/annotation_schemas.py` | `STEM_ORIENTATION_ANGLE_V1` | Videosequenz → manuelles `target_orientation`-Timelineintervall, Sequenzlabels und `final_orientation_quality` | versioniertes Schema in Registry/Label-Projekt | automatisch erzeugte Label-Studio-Konfiguration | `test_temporal_sequence_datasets.py`; Labeltext bezeichnet das Intervall als „best vertical“ und verlangt es für relevante/sichtbare Sequenzen |
| `backend/packages/cv-datasets/src/cv_datasets/target_builders/stem_orientation.py` | `build_stem_orientation_targets()` | manuelles Zielintervall + gespeicherte Rotationsspur → signierter Winkel, `sin(2e)`, `cos(2e)` und Qualitätsziel | Snapshot-Materialisierung | keine direkte UI | umfangreiche Target-Builder-Tests; Winkel wird nicht geraten, bleibt aber von einer subjektiven Zielintervallauswahl abhängig |
| `backend/packages/cv-datasets/src/cv_datasets/temporal_import.py` | Temporal-Task-Builder/Import-Key | Recorder-Manifest → idempotente Label-Studio-Videoaufgabe und Runtime-Prediction | Importledger im bestehenden Capture-/Datasetpfad | Project Mapping/Label Studio | `test_temporal_label_studio_import.py`; Prediction enthält `target_orientation`, ausdrücklich nicht Ground Truth |
| `frontend/apps/admin-web/src/features/datasets/project-mapping-page.tsx` und `datasets-page.tsx` | schema-getriebene Projektprovisionierung und Snapshotworkflow | Dataset/Schemaauswahl → bestehendes Label-Studio-Projekt, Sync und Snapshot | bestehende DB-/Workerpfade | aktuelle Label-Studio-/Dataset-UI | Temporal Dataset ist integriert; keine eigene Krümmungsminimum-UI erforderlich |
| `backend/packages/cv-training/src/cv_training/backends/temporal_sequence.py`, `temporal_data.py`, `temporal_metrics.py` | TCN/GRU-Backend und Multi-Head-Targets | materialisierte Temporal-Snapshots → Winkel-, Bogenrelevanz- und Endqualitätsmodell | bestehende Training-/Modellkatalogpfade | bestehende Training-UI | Training-, Export-, Runtime- und Deploymenttests vorhanden; darf bestehen bleiben, ist aber kein V1-Blocker und nicht Teil dieser Migration |

### 3.7 Relevante vorhandene Tests

| Bereich | Vorhandene Tests | Was sie bereits absichern | Fehlende Minimumprinzip-Abdeckung |
|---|---|---|---|
| Stammmaske/Geometrie | `tests/unit/backend/test_stem_geometry.py`, `test_runtime_stem_geometry.py` | C-/S-Bogen, Taper, Aststörung, Clipping, Rotation/Translation, Fragment-fail-closed, Payload/Registry/Pipeline | metrische horizontale Y-Krümmung nach Projektivrektifizierung, Segmentkonstanz über Rollphase |
| Tracking/Okklusion | `test_runtime_active_stem_tracker.py` | stabile Track-ID, kurze Okklusion, Trackwechsel, Ambiguität, stateful Livepfad | kalibrierte Candidate-ROI/Gatelines und Sessionstart in Anlagenkoordinaten |
| Intrinsik | `test_runtime_camera_undistort.py`, `test_runtime_camera_undistort_validation.py` | Profil/Hash, Auflösung, Cache, Pipeline, keine falsche Projektierbarkeit | Referenzebene/Homographie, X/Y/Z-Konvention, metrischer Skalenfehler |
| Rotation | `test_stem_rotation.py`, `test_runtime_stem_temporal.py` | bekannte Winkelkalibrierung, Translation, Outlier, Featureverlust, Trace-Generation | Keyframes, Re-Lock nach Impact/Verdeckung, `current_phase`, Phasenunsicherheit |
| Alte Entscheidung | `test_stem_temporal.py`, `test_runtime_stem_temporal.py` | Zielkandidat, modulo 180°, Ambiguität, freeze vor erster Walze, Restart/Trackwechsel | periodischer Fit, Minimum/Maximum, Target-Observed/Extrapolation, Gate-Reihenfolge, neuer State-Automat und v3-Resultat |
| Realtime/History | `test_realtime_live_path.py`, `test_runtime_result_service.py`, `test_stem_orientation_review_migration.py` | stateful Serialisierung, einmalige v2-History, Reviewpersistenz | einmalige v3-History, v2/v3-Lesen, kein Winkel bei abgelaufener/unsicherer Entscheidung |
| UI | `runtime-pipeline-designer-page.test.ts`, `result-history-page.test.tsx` | Intrinsikeditor, generische Boxen, v2-Result/Review | Prozess-/Phasenkalibrierung, Gateordnung, statische Curvature-Preview, Periodenkurve und phasenbasierter Reviewmodus |
| Temporal/Label Studio | `test_temporal_sample_recorder.py`, `test_temporal_label_studio_import.py`, `test_temporal_sequence_datasets.py`, `test_temporal_snapshot_materialization.py` | deterministische Artefakte, Import, manuelles Zielintervall, abgeleitetes Legacy-Winkelziel | v3-Sessionartefakte ohne Ziel-Frame-Prediction und ohne manuelles Winkel-Ground-Truth |

### 3.8 Relevante Migrationen

| Migration | Ist-Zweck | Konsequenz für das Minimumprinzip |
|---|---|---|
| `backend/migrations/versions/20260501_0011_runtime_plane_architecture.py` | `pipeline_versions` und `result_records` einschließlich JSON-Verträgen | neue Node-Konfiguration und `stem.orientation.v3` passen grundsätzlich in die vorhandenen Tabellen |
| `backend/migrations/versions/20260501_0012_runtime_pipeline_designer.py` | Designer-/Versionsmetadaten | versionierte Kalibrierprofile können wie die Intrinsik im `PipelineVersion.graph` liegen |
| `backend/migrations/versions/20260720_0026_opt_in_result_history.py` | opt-in `history_retained` | neue Resultate können denselben Retentionpfad benutzen |
| `backend/migrations/versions/20260803_0035_stem_orientation_reviews.py` | append-only v1-Reviews einschließlich `good/acceptable/bad` und optionaler Ziel-Frames | vorhandene Reviews bleiben unverändert; die nullable Ziel-Frame-Spalten erzwingen keine neue Migration für v3 |

Es gibt keine dedizierte Temporal-Sequence-Migration und keine Prozess-/Phasenkalibrierungstabelle. Für 07A bis 07F ist nach heutigem Codebefund **keine neue Alembic-Migration erforderlich**, solange die neuen Kalibrierprofile wie die bestehende Intrinsik innerhalb des versionierten Pipelinegraphen gespeichert und neue Runtimeergebnisse im vorhandenen JSON-Resultpfad abgelegt werden. Eine neue Tabelle nur für diese Funktion wäre eine unnötige Parallelpersistenz.

## 4. Soll-Ist-Abgleich der verbindlichen Architektur

| Zielvorgabe | Ist | Migrationsurteil |
|---|---|---|
| Keine 3D-Rekonstruktion | erfüllt; `stem_geometry.py` schließt metrische/3D-Aussagen ausdrücklich aus | **Keep**; neue Kalibrierung bleibt ebene Projektivabbildung |
| X/Y/Z-Konvention kalibrieren, nur horizontale projektive Y-Krümmung messen | nur Intrinsik und normierte Bild-ROIs vorhanden | **Change** in 07A/07B |
| Mehrere Rollstellungen während Einwurf | Frames und kumulative Rotation werden bereits gesammelt | **Keep/Change**: Sammlung behalten, Geometriewert und Gates ersetzen |
| Horizontale Krümmung pro Frame | nur pixel-/normbasierte Gesamtgeometrie | **Change** in 07B |
| Mantelflächenmerkmale liefern kumulative Rollphase | inkrementelle LK-Rotation vorhanden | **Keep/Change** in 07C: Gradkalibrierkern behalten, Keyframe/Re-Lock und Phasenvertrag ergänzen |
| Periodische Kurve liefert Minimum/Maximum | nicht vorhanden | **Neu** in 07D |
| Aktuelle Phase nach Aufliegen | Trace wird bei Featureverlust neu geankert und ist dann nicht global relockt | **Neu** in 07C/07E |
| Signierter Winkel am Decision Gate zurück zum Minimum | v2 rechnet zu beobachtetem Ziel-Frame; Gate ist normierte Box | **Ersetzen** in 07E, v2 kompatibel weiterlesbar |
| Keine subjektive Auswahl eines richtigen Frames | Runtime und Label Studio führen Ziel-Frames/Intervalle | **Deprecate** für den neuen Pfad in 07E/07F |
| Keine Rekonstruktion aus stark verdeckten Fragmenten | aktuelle Geometrie merged keine Fragmente und scheitert bei starker Fragmentierung | **Keep** und im neuen Curvature-Node erneut erzwingen |
| TCN/GRU nicht Voraussetzung für V1 | Modelle sind optional, Baseline kann ohne sie laufen | **Keep außerhalb des V1-Kritischen Pfads** |

## 5. Keep / Change / Deprecate / Remove Later

| Thema | Einstufung | Konkrete Entscheidung |
|---|---|---|
| Direkte Vertikalpfeil-Logik | **Change**, danach alter Algorithmus **Deprecate** | Im Runtimecode existiert kein eigener `vertical`-Node und kein explizites Welt-Vertikalziel. Die faktische Direktlogik liegt in `evaluate_projected_pre_entry_baseline()` und der Overlay-Empfehlung: beobachtete signierte Biegungsnullstelle/lokales Einzelbildminimum → Winkel. Diese Entscheidungsquelle wird für neue Pipelines durch Periodenfit + `target_phase` ersetzt. Eine Pfeil-/Drehrichtungsdarstellung darf als reine Visualisierung des v3-Ergebnisses bleiben. |
| Manuelles Zielintervall/`target_frame` | für v3 **Deprecate**; Legacy **Keep**; später **Remove Later** nur aus aktiver Erfassung | `target_orientation`, `target_frame_start/end` und `target_frame_or_interval_candidate` dürfen kein v3-Winkelziel und kein Gate speisen. Alte Snapshots, v2-Resultate und Reviews bleiben lesbar. Erst nach einer festgelegten Aufbewahrungs-/Exportfrist darf die UI das Erfassen neuer Zielintervalle für Legacyprojekte ganz entfernen; Daten und Decoder werden nicht rückwirkend gelöscht. |
| `good / acceptable / bad` | **Keep** als Zusatzlabel; als Winkelziel **Deprecate** | `final_orientation_quality` bleibt eine optionale Endlagen-/Betriebsbewertung. Es erzeugt weder `target_phase` noch Winkel, Konfidenz oder Freigabe. Der bestehende TCN/GRU-Neben-Head kann alte Datensätze weiterverwenden. |
| Bisheriges Rotationstracking | **Keep/Change** | Behalten: `StemRotationCalibrationProfile`, Hash/Version, bekannte Winkelpositionen, Translation-/Achsen-/Skalenkompensation, robuste Inlier. Ändern: neuer `analysis.stem_surface_phase`-Vertrag, Keyframebank, Re-Lock nach Verdeckung/Impact, explizite kumulative und aktuelle Phase, Phasenunsicherheit. `analysis.stem_surface_rotation` bleibt für v2-Pipelines verfügbar und wird erst nach erfolgreicher v3-Inbetriebnahme deprecated. |
| Fragment-Merging | **Keep: nicht vorhanden und nicht einführen** | `_largest_meaningful_component()` entfernt Nebenfragmente oder scheitert fail-closed. Das ist mit dem Ziel vereinbar. Das Überbrücken einer Track-ID bei kurzer Okklusion ist keine Krümmungsrekonstruktion. Der neue Horizontal-Curvature-Node darf stark verdeckte/getrennte Fragmente weder verbinden noch extrapolieren. Es gibt hier aktuell nichts zu entfernen. |
| Bestehende Resultverträge | **Keep/Extend** | `ResultPayload`, `ResultRecord`, Result Service, Retention und Connectorpfad bleiben. Neues Schema `stem.orientation.v3` mit `algorithm="CURVATURE_PHASE_MINIMUM"`; alle v2-only-Erkenner werden additiv auf v2/v3 erweitert. Kein In-place-Uminterpretieren von v2-Feldern. |
| Bestehende Gates | Konzept teilweise **Keep**, Geometrie **Change** | Das kausale Freeze-vor-Walze-Prinzip bleibt. Normierte Schwerpunkt-in-Box-Stufen werden für v3 durch kalibrierte `candidate_roi`, `curvature_observation_roi`, `surface_tracking_roi`, `session_start_gate`, `curvature_freeze_gate`, `decision_gate` und Margin ersetzt. Gate-Reihenfolge und Förderrichtung werden validiert. Alte vier ROI-Felder bleiben nur im v2-Node. |
| Intrinsische Kamerakalibrierung | **Keep** | `preprocessing.camera_undistort` bleibt zwingend vor der Prozess-Rektifizierung. Intrinsik darf nicht als Anlagenkoordinatenkalibrierung ausgegeben werden. |
| Prozess-/Referenzebenen-Kalibrierung | **Neu/Change** | Versioniertes Profil im Pipelinegraph, mit X/Y/Z-Konvention, `process_reference_plane`, Projektionsmatrix/Skala, Förderrichtung, Betrachtungsstandpunkt, positiver Rotation und Gates. Nur die ebene horizontale Y-Komponente fließt in Krümmung ein. |
| Rotation-/Phasenkalibrierung im Designer | **Change** | Die heute flachen Felder bekommen einen geführten Assistenten nach dem vorhandenen Intrinsikeditor-Muster; Profilversion, Fehler, Gültigkeitsbereich, Intrinsik-/Prozessprofilhash und Blick-/Vorzeichenkonvention werden gespeichert. |
| Runtime Preview | **Keep/Extend** | Einzelframe-Preview bleibt für Rektifizierung, Maskenqualität und Horizontal-Curvature. Sie darf wegen des frischen Contexts keine stabile Mehrframephase vortäuschen. Vollständige Sessiondiagnose nutzt den bestehenden Live-/Testfile-/Recorderpfad; keine zweite Preview-Anwendung. |
| Temporal Recorder | **Keep/Extend** | Bestehende bounded Queue, Spool-, Capture- und Retentionpfade wiederverwenden. Additive v3-Artefakte statt eines zweiten Recorders. |
| Label Studio / TCN / GRU | **Keep außerhalb des Kernpfads** | Standardsegmentierung bleibt die V1-Labelquelle. `stem_orientation_angle.v1` bleibt als Legacy-Schema, wird aber nicht automatisch für neue Krümmungsminimum-Sessions gewählt. Kein neues Modelltraining ist Bestandteil 07A–07F. |
| SPS-Schreiben | **Keep deaktiviert** | v3 liefert Messwert, Empfehlung und Shadow-Ergebnis. Kein Prompt 07A–07F aktiviert Anlagen-Schreibzugriffe. |

## 6. Zielbild innerhalb der bestehenden Anwendung

Der Zielgraph bleibt ein normaler Runtime-Pipelinegraph:

```text
Kamera
  -> preprocessing.camera_undistort
  -> preprocessing.process_coordinate_rectify
  -> bestehende Segmentierung
  -> analysis.active_stem_tracker
  -> analysis.stem_horizontal_curvature
  -> analysis.stem_surface_phase
  -> analysis.stem_curvature_phase_solver
  -> bestehender Result-/Overlay-Ausgang
```

Der Phasen-Node benötigt zusätzlich den rektifizierten Frame und die aktive Stammmaske; die Darstellung oben beschreibt die fachliche Reihenfolge, nicht den einzigen Port-Edge. `analysis.stem_surface_phase` und `analysis.stem_curvature_phase_solver` sind stateful und müssen den vorhandenen `RuntimeExecutionContext.state`-Mechanismus und die Live-Serialisierung verwenden. Rektifizierung und Einzelbildkrümmung bleiben stateless.

Der Solverzustand gehört eindeutig zu `(pipeline_version_id, node_key, track_id/session_id)` und bildet die Spezifikationszustände `WAITING`, `CANDIDATE`, `OBSERVING_CURVATURE`, `TRACKING_PHASE`, `READY_TO_DECIDE`, `RESULT_READY` sowie optional `TRACKING_TO_TARGET`/`DONE` ab. Ein Trackwechsel, Kalibrierhashwechsel, Rücksprung über Gate-Reihenfolge oder eine Trace-Generation ohne erfolgreichen Re-Lock invalidiert den Winkel; es wird keine Gradzahl geraten.

`stem.orientation.v3` muss mindestens tragen:

- `algorithm="CURVATURE_PHASE_MINIMUM"`, Schema-/Komponentenversionen und Session-/Track-ID,
- `signed_correction_deg` nur bei gültigem Decision Gate und ausreichender Konfidenz,
- `current_phase`, `target_phase`, Phasendistanz und definierte positive Drehrichtung,
- beobachtetes/extrapoliertes Ziel, Minimum/Maximum und Kurvenparameter,
- Fitresiduum, Phasenabdeckung, Rotations-/Re-Lock- und Curvature-Qualität,
- verwendete Frames/Phasenpunkte und Kalibrier-Hashes,
- `decision_frame`, Gate-/Deadline-/Ablaufinformation und Reason Codes,
- Asset-Referenzen auf Diagnose-/Sessionartefakte.

## 7. Konkrete Ziel-Dateikarte für Prompts 07A bis 07G

Die folgenden neuen Dateien sind Zielpfade für die späteren Implementierungsprompts; sie existieren beim Audit noch nicht. Die Karte erweitert ausschließlich bestehende Pakete und Anwendungen.

### Prompt 07A – Anlagenkoordinaten, Referenzebene und Prozessgates

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`: projektive, versionierte Prozess-Transformationsprovenienz in `RuntimeImageTransform` beziehungsweise einem eng typisierten Prozessprofil; keine 3D-Punktwolke.
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`: Node `preprocessing.process_coordinate_rectify`; kalibrierte ROI-/Gate-/Richtungsfelder; Publish-Blocker bei ungültigem Profil.
- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py`: `ActiveStemTrackerHandler` liest den rektifizierten Prozessraum und kalibrierte Candidate-/Session-Gates additiv; v2-Konfiguration bleibt gültig.
- `backend/packages/cv-runtime/src/cv_runtime/__init__.py`: neue öffentliche Profil-/Payloadtypen exportieren.
- `backend/packages/cv-contracts/src/cv_contracts/runtime.py` und `__init__.py`: strikter Prozesskalibrier-Vertrag und Calculate-/Validate-Request/Response innerhalb der Runtime-Admin-API.
- `backend/packages/cv-db/src/cv_db/pipeline_store.py`: Validierung von Profilhash, Intrinsikbindung, Referenzebene, X/Y/Z-Konvention, Skala, Förderrichtung, Betrachtungsstandpunkt, positiver Rotation und Gate-Reihenfolge.
- `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py`: bestehenden `capture_channel_calibration_frame()`-Pfad wiederverwenden; Prozessprofil serverseitig berechnen/validieren und weiter über den Pipelineentwurf speichern.
- `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`: geführter Prozess-/Gate-Kalibriereditor nach dem bestehenden Intrinsikmuster, Vorschau der rektifizierten Ebene und der Gate-Reihenfolge.
- Falls sich der HTTP-Vertrag ändert: `contracts/openapi/runtime-admin-api.v1.yaml`, `frontend/packages/schemas/src/index.ts`, `frontend/packages/api-client/src/index.ts` nach bestehender Schema-Generierung aktualisieren.

**Neue Dateien**

- `backend/packages/cv-runtime/src/cv_runtime/stem_process_coordinates.py`: reine Profil-, Homographie-, Skalen- und Gate-Geometriefunktionen; vorhandene Homographiemuster aus `cv_runtime.nodes.board_cut` nur mathematisch wiederverwenden, keine Board-Cut-Fachkopplung.
- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_process_coordinates.py`: `RuntimeNodeHandler` für `preprocessing.process_coordinate_rectify`.

**Migrationen**

- Keine neue Alembic-Migration. Das gültige Profil wird wie die Intrinsik im unveränderlichen `PipelineVersion.graph` versioniert. Keine zweite Kalibrierungsdatenbank.

**Tests**

- Neu: `tests/unit/backend/test_stem_process_coordinates.py` für Homographie, metrische Skala, X/Y/Z-/Vorzeichenkonvention, Profilhash, falsche Auflösung und Fail-closed-Verhalten.
- Neu: `tests/unit/backend/test_runtime_stem_process_coordinates.py` für Registry, Handler, Transformationsprovenienz und stateless Engineausführung.
- Erweitern: `test_runtime_pipeline_definition_system.py`, `test_runtime_camera_undistort_validation.py`, `runtime-pipeline-designer-page.test.ts` und bestehende Previewtests um Publish-Blocker, Gate-Reihenfolge und UI-Roundtrip.

**Rückwärtskompatibilität**

- `preprocessing.camera_undistort`, bestehende v2-Nodes und Graphen bleiben unverändert ladbar. Neue v3-Graphen verlangen das neue Profil. Keine automatische Umdeutung normierter v2-ROIs in Anlagenkoordinaten.

### Prompt 07B – Robuste horizontale Krümmung pro Frame

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`: neuer typisierter Horizontal-Curvature-Payload mit rektifizierter Centerline, Referenzlinie, signierter horizontaler Abweichung/Krümmung, gemeinsamer Stammsegmentdefinition, Sichtbarkeit, Fragment-/Occlusionstatus, physikalischer Einheit und Kalibrierprovenienz.
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`: Node `analysis.stem_horizontal_curvature`, Ports, Qualitätsparameter und Outputkatalog.
- `backend/packages/cv-runtime/src/cv_runtime/__init__.py`: Payload/Kernsymbole exportieren.
- `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`: vorhandene Nodeform und Einzelframe-Preview für Referenzlinie, Centerline, horizontale Abweichung und ausgeschlossene Fragmente nutzen.

**Neue Dateien**

- `backend/packages/cv-runtime/src/cv_runtime/stem_horizontal_curvature.py`: pure robuste Messung im rektifizierten X/Y-Referenzraum.
- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_horizontal_curvature.py`: Handler `analysis_stem_horizontal_curvature`.

**Migrationen**

- Keine; Nodekonfiguration im Pipelinegraph, Messwerte in Runtimepayloads/Result-JSON.

**Tests**

- Neu: `tests/unit/backend/test_stem_horizontal_curvature.py` mit geraden, C-/S-förmigen, rotierten, unterschiedlich skalierten und teilverdeckten synthetischen Referenzen; Fehler in physikalischer Einheit prüfen.
- Neu: `tests/unit/backend/test_runtime_stem_horizontal_curvature.py` für Payload, Registry, Engine und Overlay.
- Erweitern: `test_runtime_pipeline_definition_system.py` und Designer-Test um Portkompatibilität und Propertynormalisierung.
- Explizite Negativtests: starke Fragmentierung, zu geringer sichtbarer gemeinsamer Stammabschnitt, ungültige Prozesskalibrierung und stark verdeckte Einzelreste ergeben keine rekonstruierte Krümmung.

**Rückwärtskompatibilität**

- `StemGeometryPayload`/`analysis.stem_mask_geometry` bleiben für v2-Graphen und allgemeine Diagnostik erhalten. Der neue Payload erhält ein eigenes Schema; Pixelwerte werden nicht stillschweigend als metrische Krümmung interpretiert.

### Prompt 07C – Mantelflächenphase, Re-Lock und Gradkalibrierung

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py`: `StemRotationCalibrationProfile`, `calibrate_stem_rotation()` und robuste Matchauswertung als Basis behalten; Keyframe-/Re-Lock-Kern und explizite Phasenunsicherheit additiv ergänzen.
- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`: neuer `StemSurfacePhasePayload` mit inkrementeller, kumulativer und aktueller Phase, Keyframe-/Re-Lock-Provenienz, Trace-Generation, Konfidenz und Kalibrierhash.
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`: Node `analysis.stem_surface_phase`, strukturierte Kalibrierung, Feature-/Keyframe-/Re-Lock-Gates und `stateful=True`.
- `backend/services/realtime-executor/src/realtime_executor/live.py`: neuen Phasen-Node in `_STATEFUL_PIPELINE_NODE_TYPES` aufnehmen.
- `backend/packages/cv-contracts/src/cv_contracts/runtime.py`, `backend/packages/cv-db/src/cv_db/pipeline_store.py` und die jeweiligen `__init__.py`: strukturiertes, validiertes Grad-/Phasenkalibrierprofil, weiterhin an Intrinsik, Prozessprofil, Betrachtungsstandpunkt und Vorzeichen gebunden.
- `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` und `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`: Kalibrierassistent für bekannte Winkelpositionen, Fehler-/Gültigkeitsanzeige und reiner Profil-Save in den Pipelinegraph.
- Bei API-Änderung dieselben OpenAPI-/Frontend-Schemadateien wie in 07A regenerieren.

**Neue Dateien**

- `backend/packages/cv-runtime/src/cv_runtime/stem_surface_phase.py`: Keyframebank, inkrementelle Integration, Relativphasenvergleich, Re-Lock- und Vertrauenslogik.
- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_surface_phase.py`: stateful Handler `analysis_stem_surface_phase`.

**Migrationen**

- Keine; Profil im `PipelineVersion.graph`, Keyframes nur begrenzter Runtime-Sessionstate beziehungsweise Recorderartefakt. Keine dauerhafte Schatten-Datenbank.

**Tests**

- `test_stem_rotation.py` um Kompatibilität des bestehenden Profils erweitern.
- Neu: `tests/unit/backend/test_stem_surface_phase.py` für kumulative Phase, Translationskompensation, Keyframewahl, Verlust/Re-Lock, falsches Re-Lock, Impact und Konfidenzabbau.
- Neu: `tests/unit/backend/test_runtime_stem_surface_phase.py` für State-Isolation, Trackwechsel, Profilhashwechsel, Payload/Overlay und Handler-Reset.
- `test_realtime_live_path.py`, `test_runtime_pipeline_definition_system.py` und Designer-Test um stateful Serialisierung und Kalibrier-Roundtrip ergänzen.

**Rückwärtskompatibilität**

- `analysis.stem_surface_rotation` und `stem.rotation.v1` bleiben für v2-Pipelines. Der neue Node darf bestehende Trace-Generationen nicht als erfolgreich relockte absolute Phase ausgeben. Eine spätere Deprecation erfolgt erst nach Feldvalidierung.

### Prompt 07D – Krümmung-über-Phase-Solver

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`: neuer Fit-/Solverpayload mit verwendeten `(phase, curvature)`-Punkten, Parametern, Minimum/Maximum, `target_phase`, Abdeckung, Residuum, Mehrdeutigkeit und Reason Codes.
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`: `analysis.stem_curvature_phase_solver`, Curvature-/Phase-Ports, Mindestabdeckung, Mindestpunktzahl, Ausreißer-/Fitgates und `stateful=True`.
- `backend/services/realtime-executor/src/realtime_executor/live.py`: Solver in `_STATEFUL_PIPELINE_NODE_TYPES`.
- `backend/packages/cv-runtime/src/cv_runtime/__init__.py`: Solververtrag/Kern exportieren.

**Neue Dateien**

- `backend/packages/cv-runtime/src/cv_runtime/stem_curvature_phase.py`: reiner periodischer Solver für signiertes Modell `B(phi)` und/oder Energie `K²(phi)`, robuste Gewichtung, Minimum/Maximum, Unsicherheit und zulässige Extrapolation.
- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_curvature_phase.py`: stateful Beobachtungssammlung und Handler `analysis_stem_curvature_phase_solver`.

**Migrationen**

- Keine; Fitdaten sind Sessionstate und Result-/Recorder-JSON.

**Tests**

- Neu: `tests/unit/backend/test_stem_curvature_phase.py` für vollständige/partielle Phasenabdeckung, Ausreißer, C-/S-Bogen, 180°-Periodizität, beobachtetes versus extrapoliertes Ziel, Minimum/Maximum und degenerierte Fits.
- Neu: `tests/unit/backend/test_runtime_stem_curvature_phase.py` für bounded State, Track-/Kalibrierwechsel, Payload, Registry und Overlay.
- Erweitern: `test_realtime_live_path.py` und Pipeline-Definitionstests um Serialisierung und Portvalidierung.

**Rückwärtskompatibilität**

- Der Solver konsumiert nur neue kalibrierte Curvature-/Phasepayloads. Er liest keine v1-Pixelgeometrie als Ersatz und verändert den alten Baseline-Solver nicht.

### Prompt 07E – Sessionablauf, Decision Gate und Winkelresultat

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_curvature_phase.py`: spezifizierten Sessionautomaten, Gateübergänge, Freeze, aktuelle Phase nach Aufliegen und genau einmaliges Result-Freeze integrieren; kein zusätzlicher Parallel-Orchestrator.
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`: Gate-/Deadline-/Konfidenzfelder, Resultoutputkatalog und Reason Codes des Solver-Nodes vervollständigen.
- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`: nur falls ein eigener typisierter Sessionstatuspayload neben dem generischen `ResultPayload` benötigt wird; Resultinhalt selbst ist `stem.orientation.v3`.
- `backend/services/realtime-executor/src/realtime_executor/live.py`: `_history_result_persist_due()` und `_stem_orientation_history_event_key()` additiv für v3; einmalige Persistenz des eingefrorenen Decision-Resultats.
- `backend/packages/cv-db/src/cv_db/runtime_execution_store.py`: `_contains_decision_ready_stem_orientation()` akzeptiert v2 und v3 und behält `result_type="stem_orientation"`.
- `backend/packages/cv-db/src/cv_db/result_store.py`: `stem_orientation_result_from_record()` und Reviewzulassung additiv für v3, ohne v2 umzuschreiben.
- `backend/packages/cv-contracts/src/cv_contracts/runtime.py`: Reviewvertrag nur soweit nötig versionssensitiv machen; Ziel-Frames bei v3 nicht verlangen oder als Winkelreferenz verwenden.
- `backend/services/result-service/src/result_service/api/routes.py`: bestehende Endpunkte und Stores wiederverwenden; keine zweite Result-API.

**Neue Dateien**

- Keine zusätzliche Anwendung und kein zweiter Sessionservice. Der fachliche Zustand bleibt im in 07D eingeführten Handler/Kernmodul.

**Migrationen**

- Keine für das Resultat: `ResultRecord` speichert bereits JSON und den stabilen Typ `stem_orientation`.
- Keine Änderung oder Löschung der Migration `20260803_0035_stem_orientation_reviews.py`; Ziel-Frame-Spalten sind nullable. Nur wenn später neue normalisierte Reviewspalten zwingend werden, wäre dafür ein eigener, vorwärtskompatibler Folgeprompt nötig.

**Tests**

- Neu beziehungsweise in `test_runtime_stem_curvature_phase.py`: vollständige Zustandsübergänge, falsche Gate-Reihenfolge, Trackwechsel, Freeze-Gate, Decision-Margin, aktuelle Phase, genau ein eingefrorenes v3-Resultat und keine Gradzahl bei jedem spezifizierten Fail-closed-Grund.
- `test_realtime_live_path.py`: einmalige v3-History und stabiler Eventschlüssel.
- `test_runtime_result_service.py`: v2/v3-Lesen, Filtertyp, Retention und Reviewkompatibilität.
- `test_stem_orientation_review_migration.py`: bestehende Reviews bleiben nach Upgrade/Rollback erhalten; keine destruktive Schemaänderung.

**Rückwärtskompatibilität**

- `stem.orientation.v2` bleibt vollständig lesbar, filterbar und reviewbar. Neue Pipelines publizieren v3; alte Graphen werden nicht automatisch migriert. Connectoren erhalten weiter den generischen Resultpfad und können per Schema-/Algorithmusfeld unterscheiden.

### Prompt 07F – Diagnoseoverlay, Sessionaufzeichnung und Label-Studio-Abgrenzung

**Geänderte Dateien/Symbole**

- `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_process_coordinates.py`, `stem_horizontal_curvature.py`, `stem_surface_phase.py`, `stem_curvature_phase.py`: Overlay-Primitiven für Gates, aktive Messstrecke, Centerline/Referenzlinie, Feature-/Keyframe-/Re-Lock-Status, aktuelle/Zielphase und Decision-Zustand.
- `backend/packages/cv-runtime/src/cv_runtime/temporal_samples.py`: `_orientation_result()` additiv für v3; Event-/Artefaktmaterialisierung für Curvature-Beobachtungen, Surface-Phase-Trace, Fitparameter und finales v3-Resultat. v3 erzeugt keine `target_frame_or_interval_candidate`-Baseline-Prediction.
- `backend/services/realtime-executor/src/realtime_executor/temporal_samples.py`: `temporal_recorder_config_from_graph()` erkennt auch `analysis.stem_curvature_phase_solver`; bestehende Queue, Finalisierung, Capture Intents und Asset-Referenzen bleiben.
- `backend/services/capture-service/src/capture_service/executor.py`: nur die bestehende Importentscheidung schema-/resultversionssensitiv machen; Krümmungsminimum-Aufzeichnungen nicht ungefragt an `stem_orientation_angle.v1` senden.
- `backend/packages/cv-datasets/src/cv_datasets/temporal_import.py`: Legacy-v2-Prediction behalten; für v3 kein `target_orientation`-Timeline-Predictionobjekt erzeugen.
- `frontend/apps/admin-web/src/features/results/result-history-page.tsx`: `StemOrientationResultSummary` um Periodenkurve, Messpunkte, Minimum/Maximum, aktuelle/Zielphase, Fit-/Re-Lock-Qualität und Gate-/Ablaufstatus erweitern; `StemOrientationReviewPanel` blendet Ziel-Frame-Eingaben bei v3 aus.
- `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`: statische Previewdiagnostik der neuen Nodes; keine vorgetäuschte Mehrframephase.
- Bei API-Feldänderungen bestehende OpenAPI-/Schema-/Clientgenerierung ausführen.

**Neue Dateien**

- Keine neue Recorder-, Labeling- oder Resultanwendung. Optional darf ein kleines wiederverwendbares Frontend-Unterelement für den Phasenplot im bestehenden Result-Feature entstehen, zum Beispiel `frontend/apps/admin-web/src/features/results/stem-curvature-phase-chart.tsx`; dies ist eine Komponente, keine Anwendung.
- Kein neues Label-Studio-Winkel-Ground-Truth-Schema in V1. Standardsegmentierung benutzt die bereits vorhandenen Projekt-/Datasetpfade. Ein rein technisches Sequenzreview wäre ein späteres optionales Schema und darf keine manuelle Ziel-Frame-Pflicht einführen.

**Migrationen**

- Keine. Recorderartefakte sind versionierte Dateien; alte Manifest-/Artefaktversionen bleiben lesbar. Bestehende Dataset-/Label-Projekt-Tabellen werden wiederverwendet.

**Tests**

- `test_temporal_sample_recorder.py`: v2-Artefakte unverändert; v3-Artefakte deterministisch, bounded, vollständig referenziert und ohne Ziel-Frame-Prediction.
- `test_temporal_label_studio_import.py`: v3 wird nicht als `stem_orientation_angle.v1` importiert; Legacyimport bleibt idempotent.
- `test_temporal_sequence_datasets.py` und `test_temporal_snapshot_materialization.py`: bestehende Legacy-Snapshots bleiben reproduzierbar; kein neues subjektives Winkelziel.
- `result-history-page.test.tsx`: v3-Kurve/Phasen/Reason Codes, Ziel-Frame-Felder nur für v2.
- `runtime-pipeline-designer-page.test.ts`: neue Overlay-/Editorpfade und statische Preview.

**Rückwärtskompatibilität**

- Bestehende `stem_orientation_angle.v1`-Projekte, Exporte, Snapshots, TCN/GRU-Rezepte und v2-Recorderartefakte bleiben unverändert verfügbar. Nur neue Minimumprinzip-Sessions werden aus dem manuellen Zielintervallpfad herausgenommen.

### Prompt 07G – nicht definiert

Im gelesenen `docs/Stammausrichtung_Inferenzserver/Codex_Prompts_Produktivsystem_Stammbogen_Minimumprinzip.md` sind Prompt 07A bis 07F definiert; danach folgt Prompt 08. Der Auditauftrag und die Einleitung des Promptdokuments verlangen zwar eine Karte „07A bis 07G“, enthalten aber keinen Titel, Umfang oder Akzeptanzvertrag für 07G.

- **Geänderte Dateien:** nicht belastbar bestimmbar.
- **Neue Dateien:** keine erfinden.
- **Migrationen:** nicht bestimmbar.
- **Tests:** nicht bestimmbar.
- **Rückwärtskompatibilität:** nicht bewertbar, solange 07G nicht definiert ist.

Prompt 08 wird nicht eigenmächtig in 07G umbenannt. Das ist eine echte Dokumentationslücke.

## 8. Offene Punkte, die der Code nicht auflöst

1. **Definition von Prompt 07G:** Im maßgeblichen Promptdokument fehlt der Abschnitt vollständig.
2. **Anlagenspezifische Koordinaten und Vorzeichen:** Reale Referenzpunkte, X/Y/Z-Richtung, Referenzebene, Förderrichtung, Betrachtungsstandpunkt, positive Drehrichtung sowie Position/Reihenfolge aller ROIs und Gates sind nicht aus Repositorycode ableitbar und müssen bei Inbetriebnahme gemessen/versioniert werden.
3. **Gemeinsamer physischer Stammabschnitt:** Der aktuelle Code benutzt sichtbare normierte Maskenbereiche. Er legt nicht fest, welcher kalibrierte Längsabschnitt über alle Rollphasen als identische Messstrecke gilt, wenn Stammenden, Äste oder Verdeckungen wandern.
4. **Referenzlinie der horizontalen Krümmung:** Die Spezifikation lässt robuste Gerade, Sehne oder eine gleichwertige definierte Referenz zu. Der Code entscheidet nicht, welche Variante die Referenzkörper und die erwarteten C-/S-Bögen am zuverlässigsten trennt.
5. **Keyframe-/Re-Lock-Grenzen:** Die vorhandene Rotation setzt nach Featureverlust zurück. Anzahl/Abstand der Keyframes, Descriptor-/Matchgates und maximal zulässige Phasenunsicherheit nach Impact müssen mit realen Mantelflächen- und Verdeckungsdaten bestimmt werden.
6. **„Aufliegen“ und aktuelle Phase:** Es gibt keine belastbare numerische Definition für Impact-Ende beziehungsweise ausreichend ruhige aktuelle Phase. Zeit-, Bewegungs- und Re-Lock-Schwellen müssen aus der Anlage kommen.
7. **Zulässige Extrapolation:** Weder Code noch Spezifikation beziffern die minimale Phasenabdeckung, maximale Extrapolationsdistanz und Unsicherheitsgrenze, ab der `target_observed=false` noch einen Winkel erlauben darf.
8. **Deadline/Expiry:** Der aktuelle Code kennt Decision-/Roller-ROIs, aber keine kalibrierte verbleibende Zeit oder sichere Ablaufgrenze. Decision-Margin, ggf. verfügbare Fördergeschwindigkeit und maximale Resultgültigkeit sind anlagenspezifisch festzulegen.
9. **Abnahmeschwellen:** Mindestbogenrelevanz, metrischer Curvature-Fehler, Fitresiduum, Min/Max-Trennschärfe, erforderliche Phasenabdeckung und Gesamtkonfidenz sind ohne vermessene gerade/C-/S-Referenzkörper und aufgezeichnete reale Sessions nicht seriös festlegbar. Alte Pixel-/Baseline-Defaults dürfen nicht übernommen werden.

Alle übrigen Architekturfragen sind durch vorhandene Plattformmuster oder die Minimumprinzip-Spezifikation ausreichend aufgelöst: Persistenz im versionierten Pipelinegraph/Result-JSON, Runtime-State in `RuntimeExecutionContext.state`, bestehende Admin-Web-/Result-/Recorderpfade, keine 3D-Rekonstruktion, keine SPS-Schreibfreigabe und keine V1-Abhängigkeit von TCN/GRU.
