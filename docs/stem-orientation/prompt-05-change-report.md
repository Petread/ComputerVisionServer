# Change Report – Prompt 05: relative Stammrotation und Vor-Einzug-Baseline

Stand: 2026-08-03

## Ergebnis und Abgrenzung

Die deterministische Phase-F-Baseline ist innerhalb der vorhandenen Runtime umgesetzt. `analysis.stem_surface_rotation` erzeugt eine kalibrierte relative Rotationsspur; `analysis.stem_temporal_orientation` friert spätestens beim Eintritt in die konfigurierte Entscheidungszone ein Shadow-Result `stem.orientation.v2` ein. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemSurfaceRotationHandler.execute`, `StemTemporalOrientationHandler.execute`, `_orientation_result`.

Es wurden keine API, Frontend-Komponente, Datenbanktabelle, Migration, Trainingsfunktion oder SPS-Schreiboperation ergänzt. Der Temporal-Result setzt `operating_mode="shadow"`, `temporal_model_version_id=None` und enthält keine Aktoranbindung. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result`, `_temporal_result`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Compatibility-Constraints von `analysis.stem_temporal_orientation`.

## Architekturentscheidung: eigener Surface-Rotation-Node

Die Rotationsspur wurde gemäß Spezifikation 2.2 als eigener Node `analysis.stem_surface_rotation` umgesetzt und nicht in einen Temporal-Super-Node eingebaut. Sie besitzt mit `StemRotationPayload` einen eigenen Vertrag, eine eigenständig validierbare Offline-Kalibrierung, einen eigenen begrenzten Frame-State und kann unabhängig von der Zielkandidatenlogik geprüft werden. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `StemRotationPayload`; `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` — `calibrate_stem_rotation`, `estimate_surface_rotation_from_matches`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemSurfaceRotationHandler`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node `analysis.stem_surface_rotation`.

## Rotationskern

`calibrate_stem_rotation` passt eine robuste Ursprungsgerade an bekannte Winkelstellungen oder temporäre Umfangsmarkierungen an, verwirft Residualausreißer über MAD und speichert Methode, Intrinsik-Hash, Blickrichtung, positive Richtung, validierten Bild-/Breitenbereich, Fehlergrenzen, Version und SHA-256-Profilhash. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` — `StemRotationCalibrationObservation`, `StemRotationCalibrationProfile`, `StemRotationCalibrationMethod`, `calibrate_stem_rotation`, `stem_rotation_calibration_hash`.

`estimate_surface_rotation_from_matches` transformiert jedes Merkmal in das lokale Achs-/Normalensystem des vorherigen und aktuellen Stamms. Dadurch werden Zentroidtranslation, Achsrotation und Längen-/Breitenskalierung vor der robusten transversalen Zusammenfassung entfernt. Trackingfehler, Längsschlupf, Featureanzahl, Inlieranteil, Kalibrierungsbereich, Einzelschritt und Confidence sind Fail-closed-Gates. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` — `StemAxisObservation`, `SurfaceFeatureMatch`, `SurfaceRotationEstimationConfig`, `estimate_surface_rotation_from_matches`.

Der Runtime-Handler sucht Shi-Tomasi-Merkmale und führt Lucas-Kanade-Flow ausschließlich innerhalb der robust erodierten aktiven Stammmaske aus. Sein State unter `context.state[node.node_key]` enthält nur den letzten Grauframe, die erodierte Maske, die Referenzachse, die Frameidentität, die unwrapped Summe und eine Trace-Generation. Trackwechsel erzeugt einen neuen kalibrierten Nullanker; Zeit-/Framelücken, Blur oder Featureverlust erzeugen keine Gradzahl und markieren eine Diskontinuität. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_track_surface_features`, `_eroded_stem_mask`, `_store_rotation_baseline`, `_reset_rotation_state`, `_rotation_trace_gap`, `_rotation_anchor`, `StemSurfaceRotationHandler.execute`.

## Vor-Einzug-Baseline und Prozesszonen

Die Konfiguration führt `observation_entry_roi`, `throw_in_roi`, `decision_roi`, `first_roller_roi`, Prozesszonen-Kalibrierungs-ID/-Version/-Intrinsik-Hash sowie explizite Blick- und Positivrichtung. Alle Felder verwenden bestehende Designer-Typen `roi`, `text`, `select`, `number`, `integer` oder `boolean`. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node `analysis.stem_temporal_orientation`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_temporal_node_config`, `_process_configuration_hash`.

`evaluate_projected_pre_entry_baseline` ordnet projizierten Bogen und kumulative Rotation, erkennt Verbesserung, Verschlechterung, Vorzeichenwechsel, eingeklammerte Minima, S-/Mehrdeutigkeit und unzureichende Beobachtung. Die Korrektur wird mit `wrap_to_half_turn` strikt nach `[-90°, +90°)` abgebildet. Eine Gradzahl entsteht nur bei gültiger Kalibrierung, zusammenhängender Spur, ausreichender Rotation, ausreichender Beobachtungsqualität und eindeutigem Zielkandidaten; andernfalls ist der Status `manual_required`. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/stem_temporal.py` — `StemTemporalReasonCode`, `evaluate_projected_pre_entry_baseline`, `_target_candidates`, `wrap_to_half_turn`.

Beim ersten Frame in `decision_roi` wertet `StemTemporalOrientationHandler.execute` ausschließlich den bis dahin gespeicherten Beobachtungspuffer aus; der aktuelle Frame hinter der Deadline wird nicht mehr zur Zielsuche hinzugefügt. Das Result wird anschließend unter `frozen_pre_entry_estimate` unverändert wieder ausgegeben. Der Eintritt in `first_roller_roi` ohne frühere Entscheidung erhält den Reason Code `decision_line_missed_first_roller_reached`. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemTemporalOrientationHandler.execute`, `_process_stage`.

Das Result enthält die geforderten direkten Felder, Ziel-/Entscheidungsframe, Diagnosewerte, `estimate_method="projected_baseline"`, vollständige Kalibrierungs-/Schema-/Algorithmusversionen sowie Blick- und Vorzeichenprovenienz. Overlay-Primitives zeigen alle vier Prozesszonen, Merkmalsvektoren, Rotationsstatus, Empfehlung, Blickrichtung, Positivrichtung und Reason Codes. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result`, `_rotation_overlay`, `_temporal_overlay`.

## Runtime-Integration

`StemRotationPayload` ist Teil von `AnyRuntimePayload` und wird aus `cv_runtime` exportiert. Beide Handler werden über vorhandene `module:attribute`-Referenzen geladen. Beide Node-Typen stehen in `BUILTIN_NODE_TYPES`; der Realtime-Executor erkennt sie über `_STATEFUL_PIPELINE_NODE_TYPES` und begrenzt den Graphen über `_pipeline_max_inflight` auf ein geordnetes Work-Item. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `StemRotationPayload`, `AnyRuntimePayload`; `backend/packages/cv-runtime/src/cv_runtime/__init__.py` — Exporte; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — beide Node-Typen; `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`, `_pipeline_max_inflight`.

## Tests und lokale Verifikation

Die synthetischen Kerntests decken bekannte Winkelkalibrierung und Profilhash, bekannte Winkelschritte, reine Translation, Achsrotation, lokalen Schlupf, Featureverlust, ungültige Kalibrierung, F1–F4-Zielinterpolation, Modulo-180, S-/Mehrdeutigkeit und Fail-closed-Qualitätsgates ab. Codebeleg: `tests/unit/backend/test_stem_rotation.py`; `tests/unit/backend/test_stem_temporal.py`.

Die Runtime-Tests decken Registry, Handler-Loading, Standard-Designerfelder, Payload-Roundtrip, Graphaufbau, Nullanker, Featureverlust/Wiederauftauchen, Trackwechsel, eingefrorenes Result an der Decision-Zone, Runtime-Neustart und Stateful-Serialisierung ab. Codebeleg: `tests/unit/backend/test_runtime_stem_temporal.py`.

Ausgeführt wurden `python -m ruff check` für die neuen Kerne, Handler und Tests, `python -m py_compile` für die geänderten Python-Module sowie isolierte Pytest-Läufe mit `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` und `--confcutdir=tests/unit/backend`. Es wurden keine Services, Anwendungen oder Container gestartet.

## Bewusst offene Punkte

- Die Runtime kann ein validiertes Rotationsprofil konsumieren, aber dieser Auftrag ergänzt keinen Upload-, Datenbank- oder Admin-Workflow für Rotationskalibrierungsdatensätze. Die offline testbare Erzeugungsgrenze ist `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` — `calibrate_stem_rotation`; Persistenz und Bedienworkflow bleiben offen.
- Der bestehende Result-History-Sampler garantiert keine ereignispriorisierte Persistenz des eingefrorenen Decision-Results. Codebeleg: `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker._history_result_persist_due`, `RealtimeRuntimeWorker._publish_pipeline_work_result`. Dies bleibt für Prompt 06 offen.
- Ein nicht beendeter ephemerer Temporal-State kann beim Runtime-Shutdown mangels Handler-Finalisierung nicht nachträglich als `aborted_runtime_restart` persistiert werden. Ein neuer Runtime-Kontext beginnt sicher ohne alte Empfehlung. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/handlers.py` — `RuntimeNodeHandler`; `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemTemporalOrientationHandler.execute`.
- Eine schräge Einzelkamera liefert weiterhin keine 3D-Bogenrichtung. Das Result bleibt deshalb ausdrücklich `projected_baseline`; ein validiertes Temporalmodell existiert im aktuellen Trainer-/Katalog-/Gateway-Pfad nicht. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result`; `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType`; `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`.
