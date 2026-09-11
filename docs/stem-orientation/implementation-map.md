# Implementierungslandkarte Stammausrichtung

> Fortschreibung kanonischer Messkorridor (2026-08-05): Der bestehende Node
> `preprocessing.process_coordinate_rectify` verwendet mit
> `RuntimeImageViewProfile` den gemeinsamen Transformationskern in
> `cv_runtime.image_view`. Runtime, Preview und `capture.full_frame_labelstudio`
> transportieren dadurch dieselben kanonischen Pixel; Eligibility, Dataset-
> Provenienz und Modellbindung sind in
> [canonical-measurement-view.md](canonical-measurement-view.md) beschrieben.

> Fortschreibung Phase G (2026-08-03): Das eingefrorene
> `stem.orientation.v2`-Vor-Einzug-Result enthält jetzt eine begrenzte
> Krümmungs-/Rotationsspur und explizite Artefaktprovenienz in
> `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` —
> `_orientation_result`. Der bestehende Realtime-Resultpfad persistiert den
> ersten entscheidungsbereiten Stand als semantisches Ereignis über
> `RealtimeRuntimeWorker._history_result_persist_due`; der gespeicherte Datensatz
> erhält über `cv_db.runtime_execution_store._result_type` den Typ
> `stem_orientation`. Versionierte Schnellreviews werden append-only über
> `cv_db.models.StemOrientationReview`, die Result-Service-Routen
> `get_stem_orientation_reviews`/`append_stem_orientation_review` und den
> bestehenden `ResultDetailPanel` geführt. Die Migration
> `20260803_0035_stem_orientation_reviews.py` behält diese Bedienerdaten auch bei
> einem Anwendungs-Rollback.
>
> Fortschreibung Phase F (2026-08-03): `StemRotationPayload` in
> `backend/packages/cv-runtime/src/cv_runtime/payloads.py`, die reinen Kerne
> `calibrate_stem_rotation`/`estimate_surface_rotation_from_matches` in
> `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` und
> `evaluate_projected_pre_entry_baseline` in
> `backend/packages/cv-runtime/src/cv_runtime/stem_temporal.py` sowie die Handler
> `StemSurfaceRotationHandler` und `StemTemporalOrientationHandler` in
> `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` bilden jetzt
> die deterministische, ausschließlich projizierte Shadow-Baseline. Die Node-Typen
> `analysis.stem_surface_rotation` und `analysis.stem_temporal_orientation` sind in
> `backend/packages/cv-runtime/src/cv_runtime/registry.py` registriert und in
> `backend/services/realtime-executor/src/realtime_executor/live.py` über
> `_STATEFUL_PIPELINE_NODE_TYPES` serialisiert.
>
> Fortschreibung Phase D (2026-08-03): Die in Abschnitt 10 als ursprüngliche
> Kollisionen 3 bis 5 dokumentierten Erweiterungslücken sind mit
> `RuntimeCameraCalibrationCalculateRequest`/
> `RuntimeCameraCalibrationCalculateResponse` in
> `backend/packages/cv-contracts/src/cv_contracts/runtime.py`,
> `calculate_channel_camera_calibration` in
> `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py`,
> `CameraIntrinsicCalibrationEditor` in
> `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`
> sowie `CameraUndistortHandler` in
> `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` geschlossen.
> Die Persistenzentscheidung und der weiterhin offene dockerfreie
> Installer-Release-Rollback sind in
> `docs/stem-orientation/camera-calibration-persistence.md` dokumentiert.

Stand: 2026-08-03. Dieses Dokument beschreibt ausschließlich den im Repository vorhandenen Erweiterungs- und Ausführungspfad einschließlich der inzwischen implementierten Stammkomponenten. Grundlage sind `AGENTS.md` (`Stammausrichtung – dauerhafte Arbeitsregeln`) und `docs/Stammausrichtung_Inferenzserver/Spezifikation.md` (`Spezifikation Stammausrichtung`).

## Kurzantworten auf die vier Prüffragen

| Prüffrage | Verifizierte Antwort | Codebeleg |
|---|---|---|
| Werden Standard-Property-Typen automatisch im Frontend gerendert? | Ja, wenn ein Node einen bereits im Vertrag vorhandenen Typ verwendet. `boolean`, `select`, `expression`, `label_project_list`, `zone_list`/`roi`, `slider`, `integer`/`number` und die vorhandenen strukturierten Typen haben explizite Zweige; `text` landet im Text-Input-Fallback. Ein neuer `field_type` ist dagegen nicht automatisch möglich. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`; `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type` |
| Erscheint ein neuer Standard-Node ohne individuelle Frontendänderung im Designer? | Ja, sofern er in `BUILTIN_NODE_TYPES` registriert und aktiv ist, den aktuellen Modus unterstützt, nicht `hidden_from_palette` ist und `canAddNodeToGraph` seine Graphbedingungen erfüllt. Ein neuer Analysis-Node ohne Sondergruppe landet in der allgemeinen Analysis-Gruppe. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `BUILTIN_NODE_TYPES`; `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `ensure_builtin_node_types`, `list_runtime_node_types`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `nodeTypesQuery`, `eligibleNodeTypesForGraph`, `canAddNodeToGraph`, `paletteGroupsForCategory` |
| Wird `RuntimeExecutionContext.state` über aufeinanderfolgende Liveframes geteilt? | Ja, aber nur zuverlässig, wenn mindestens ein Node des Graphen von `_stateful_pipeline_nodes` als stateful erkannt wird. Dann erzwingt `_pipeline_max_inflight` den Wert `1`, und jeder Frame-Kontext erhält dasselbe lokale `pipeline_state`-Dictionary. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`, `_stateful_pipeline_nodes`, `_pipeline_max_inflight`, `RealtimeRuntimeWorker._execute_pipeline_work` |
| Unterstützt der Modell-/Trainerpfad bereits Sequenzregression oder Multi-Task-Zeitreihenmodelle? | Nein. Die vollständige Capability-Matrix umfasst nur Classification, Detection, Oriented Detection, Segmentation und Anomaly; Training-Export und öffentliche Inferenz verwenden bildbezogene Tensorverträge. | `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType`; `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`, `supported_model_architectures`; `backend/workers/training-worker/src/training_worker/runtime.py` — `_build_io_schema`; `backend/services/inference-gateway/src/inference_gateway/runtime/public_service.py` — `PublicGatewayMixin._ensure_task` |

## 1. Exakte Erweiterungspunkte für Payloads, Handler und Node-Typen

### 1.1 Payloads

| Schritt | Bestehender Erweiterungspunkt | Codebeleg |
|---|---|---|
| Payload definieren | Ein neuer Runtime-Payload folgt `RuntimePayload`, verwendet ein eindeutiges `payload_type` und Pydantic-Felder mit `extra="forbid"`. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `RuntimePayload` |
| Payload für Handler-Ports zulassen | Der konkrete Typ muss in `AnyRuntimePayload` aufgenommen werden, weil `NodeExecutionResult.outputs` genau auf diese Union typisiert ist. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `AnyRuntimePayload`; `backend/packages/cv-runtime/src/cv_runtime/handlers.py` — `NodeExecutionResult.outputs` |
| Existierende Segment-/Trackingdaten wiederverwenden | `SegmentPayload.objects` kann `RuntimeObjectReference`-Einträge tragen; `RuntimeObjectReference.track_id` ist bereits vorhanden. Ein separater Tracking-Payload ist nur erforderlich, wenn die fachlichen Daten nicht in diesen Vertrag passen. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `SegmentPayload`, `RuntimeObjectReference.track_id` |
| Persistierbares fachliches Ergebnis ausgeben | Kompakte fachliche Ergebnisse verwenden `ResultPayload.result`; Persistenz entsteht erst über einen `output.result`-Node oder dessen Projektionslogik. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `ResultPayload`; `backend/packages/cv-runtime/src/cv_runtime/engine.py` — `_persisted_results` |
| Relative Stammrotation transportieren | Der typisierte Vertrag führt Track, kalibriertes Delta, unwrapped Summe, Richtung, Konfidenz, Feature-/Inlierqualität, Translation, Diskontinuität sowie vollständige Kalibrierungs- und Richtungskonvention. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `StemRotationPayload`, `StemRotationFeatureQuality`, `StemRotationTranslationComponent`, `AnyRuntimePayload` |
| Eigenen Top-Level-Ausführungsabschnitt ergänzen | Nur wenn ein neuer Payload zusätzlich als eigener Abschnitt in der Ausführungsantwort erscheinen soll, sind `RuntimeExecutionResult` und die Aggregation in `ExecutablePipelineGraph.execute` zu erweitern. Für den bloßen Transport zwischen Node-Ports ist dieser Schritt nicht erforderlich. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `RuntimeExecutionResult`; `backend/packages/cv-runtime/src/cv_runtime/engine.py` — `ExecutablePipelineGraph.execute` |

### 1.2 Handler

| Schritt | Bestehender Erweiterungspunkt | Codebeleg |
|---|---|---|
| Handlervertrag | Ein Handler besitzt `node_type` und implementiert `execute(context, node, inputs) -> NodeExecutionResult`. | `backend/packages/cv-runtime/src/cv_runtime/handlers.py` — `RuntimeNodeHandler`, `NodeExecutionResult` |
| Kontext verwenden | Laufzeitdaten, Inferenzclient, Modellauflösung, Eingangsframe, Metadaten und ephemerer State kommen über `RuntimeExecutionContext`. | `backend/packages/cv-runtime/src/cv_runtime/context.py` — `RuntimeExecutionContext` |
| Komplexen Node auslagern | Der vorhandene Präzedenzfall sind feste Handlerklassen und exportierte Handlerinstanzen in einem eigenen Modul. | `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py` — `BoardViewCutConstraintsHandler`, `analysis_board_view_cut_constraints` |
| Handler laden | `runtime_handler_ref` hat das Format `module:attribute`; `load_runtime_handler` importiert das Attribut, instanziiert Klassen und prüft über `assert_handler_compatible` den erwarteten `node_type`. | `backend/packages/cv-runtime/src/cv_runtime/loader.py` — `load_runtime_handler`, `validate_runtime_handler_ref`; `backend/packages/cv-runtime/src/cv_runtime/handlers.py` — `assert_handler_compatible` |
| Builtin-Konvention verwenden | Ohne expliziten Handler erzeugt `_node` die Referenz `cv_runtime.nodes.builtin:<node_type mit Punkten als Unterstrichen>`; dafür muss in `builtin.py` ein gleichnamiges Modulattribut existieren. Ein eigenes Modul wird über das Argument `handler=` referenziert. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `_node`; `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `source_file_test`, `BUILTIN_HANDLERS` |
| Rotationsspur und Vor-Einzug-Baseline | Der Rotationshandler hält nur den letzten Grauframe, die erodierte Maske, die Achse und die unwrapped Summe; der Temporalhandler hält einen begrenzten Beobachtungspuffer und friert das Result beim ersten Entscheidungsframe ein. | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemSurfaceRotationHandler.execute`, `StemTemporalOrientationHandler.execute`, `_store_rotation_baseline`, `_orientation_result` |

### 1.3 Node-Typen und Registry

| Schritt | Bestehender Erweiterungspunkt | Codebeleg |
|---|---|---|
| Ports und Properties beschreiben | `port` erzeugt `RuntimeNodePort`, `prop` erzeugt `RuntimeNodePropertySpec`; `_node` bündelt Kategorie, Modi, Ports, Default-Konfiguration, Properties, Handlerreferenz und Authoring-Constraints. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `port`, `prop`, `_node` |
| Node in den Codekatalog aufnehmen | Die neue `_node(...)`-Definition muss in `BUILTIN_NODE_TYPES` stehen; `BUILTIN_NODE_TYPE_BY_KEY` und `builtin_node_types` werden daraus abgeleitet. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `BUILTIN_NODE_TYPES`, `BUILTIN_NODE_TYPE_BY_KEY`, `builtin_node_types` |
| Registry in PostgreSQL spiegeln | `ensure_builtin_node_types` schreibt beziehungsweise aktualisiert `RuntimeNodeTypeRegistry`; `list_runtime_node_types` gibt nur aktive Einträge zurück und kann nach Kategorie und Modus filtern. | `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `ensure_builtin_node_types`, `_runtime_node_type_active`, `list_runtime_node_types`; `backend/packages/cv-db/src/cv_db/models.py` — `RuntimeNodeTypeRegistry` |
| Graph validieren und ausführbar bauen | `validate_pipeline_graph` prüft Registrytyp, Modus, Ports, Properties, Modellbindung und Handlerreferenz; `ExecutablePipelineGraph.build` löst danach die Handler auf. | `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `validate_pipeline_graph`; `backend/packages/cv-runtime/src/cv_runtime/engine.py` — `ExecutablePipelineGraph.build` |

## 2. Lebenszyklus von `RuntimeExecutionContext.state` im Realtime-Betrieb

| Phase | Realer Lebenszyklus | Codebeleg |
|---|---|---|
| Workerstart | `RealtimeRuntimeWorker.run` legt genau einmal pro `run`-Aufruf ein lokales `pipeline_state: dict[str, Any] = {}` an; der State kommt nicht aus PostgreSQL. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`; `backend/packages/cv-runtime/src/cv_runtime/context.py` — `RuntimeExecutionContext.state` |
| Stateful-Erkennung | `_stateful_pipeline_nodes` vergleicht die `node_type`-Werte des Graphen mit `_STATEFUL_PIPELINE_NODE_TYPES`. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_stateful_pipeline_nodes`, `_STATEFUL_PIPELINE_NODE_TYPES` |
| Reihenfolge | Sobald die Erkennung mindestens einen stateful Node findet, liefert `_pipeline_max_inflight` immer `1`; Frames werden dadurch nicht parallel in der Pipeline ausgewertet. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_pipeline_max_inflight` |
| Übergabe an Frames | Bei `pipeline_max_inflight == 1` übergibt `RealtimeRuntimeWorker.run` dasselbe `pipeline_state` an jeden Work-Item; `_execute_pipeline_work` setzt dieses Objekt direkt als `RuntimeExecutionContext.state`. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`, `RealtimeRuntimeWorker._execute_pipeline_work` |
| Node-Isolation | Vorhandene Handler legen fachlichen State unter `context.state[node.node_key]` an; damit kollidieren mehrere Instanzen desselben Node-Typs nicht. | `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `FrameSamplerHandler.execute`, `ZoneEntryExitHandler.execute`, `OpticalFlowMotionHandler.execute`; `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py` — `_stabilize_realtime_guided_pose` |
| Gedroppte Frames | Frames, die Puffer-, FPS-, Deadline- oder Stale-Result-Logik verwirft, führen den Graphen nicht erfolgreich als geordnetes neues Ergebnis aus und aktualisieren daher nicht verlässlich den fachlichen Node-State. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`, `RealtimeRuntimeWorker._publish_pipeline_work_result`, `LatestFrameBuffer` |
| Workerende | Im `finally`-Block werden Quelle und Executor beendet sowie der SPS-Verbindungscache geschlossen; das lokale `pipeline_state` wird danach verworfen. Es gibt keine State-Persistenz oder Wiederherstellung. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`; `backend/packages/cv-runtime/src/cv_runtime/siemens_sps.py` — `close_siemens_sps_connection_cache` |
| Preview | Jeder Preview-Request erzeugt einen neuen `RuntimeExecutionContext` mit seinem Default-State und verwirft ihn nach genau einer Ausführung; mehrere Preview-Aufrufe teilen keinen State. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `preview_channel_pipeline_graph`; `backend/packages/cv-runtime/src/cv_runtime/context.py` — `RuntimeExecutionContext.state` |

Wichtig: Wenn kein erkannter stateful Node im Graphen steht, kann `pipeline_max_inflight` größer als `1` werden; dann übergibt `RealtimeRuntimeWorker.run` pro Work-Item ein neues `{}`. Ein Handler, der lediglich `context.state` benutzt, ist dadurch noch nicht frameübergreifend stateful. Codebeleg: `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`, `_pipeline_max_inflight`.

## 3. Eintragungsstelle für neue stateful Node-Typen

`analysis.active_stem_tracker`, `analysis.stem_surface_rotation` und `analysis.stem_temporal_orientation` sind in `backend/services/realtime-executor/src/realtime_executor/live.py` in `_STATEFUL_PIPELINE_NODE_TYPES` eingetragen; genau dieses Set steuert `_stateful_pipeline_nodes` und damit die Serialisierung durch `_pipeline_max_inflight`. Die drei Handler legen ihren kompakten, node-spezifischen Zustand jeweils unter `context.state[node.node_key]` ab. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py` — `ActiveStemTrackerHandler.execute`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemSurfaceRotationHandler.execute`, `StemTemporalOrientationHandler.execute`; `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`.

Der fachliche State muss je Nodeinstanz unter `context.state[node.node_key]` liegen; die vorhandenen Muster sind `FrameSamplerHandler.execute` in `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` und `_stabilize_realtime_guided_pose` in `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py`.

`preprocessing.camera_undistort` ist inzwischen ebenfalls in `_STATEFUL_PIPELINE_NODE_TYPES` eingetragen. `CameraUndistortHandler._remap_for_context` legt die OpenCV-Remap-Matrizen unter `context.state[node.node_key]` ab; dadurch teilt der Realtime-Executor diesen Cache geordnet über Folgeframes. Codebeleg: `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`; `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` — `CameraUndistortHandler._remap_for_context`.

Bestehende Auffälligkeit: `ObjectSpatialSignalHandler.execute` und `MovementDirectionHandler.execute` in `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` verwenden ebenfalls `context.state.setdefault(node.node_key, {})`, aber `analysis.object_spatial_signal` und `analysis.movement_direction` fehlen derzeit in `_STATEFUL_PIPELINE_NODE_TYPES` in `backend/services/realtime-executor/src/realtime_executor/live.py`. Diese Abweichung wird hier nur dokumentiert und nicht behoben.

## 4. Weg eines neuen Node-Typs bis in den Pipeline-Designer

| Reihenfolge | Übergang | Codebeleg |
|---|---|---|
| 1 | `_node(...)` beschreibt den Typ und wird in `BUILTIN_NODE_TYPES` aufgenommen. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `_node`, `BUILTIN_NODE_TYPES` |
| 2 | `ensure_builtin_node_types` synchronisiert den Codekatalog in `RuntimeNodeTypeRegistry`; `list_runtime_node_types` liest aktive Typen zurück. | `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `ensure_builtin_node_types`, `list_runtime_node_types`; `backend/packages/cv-db/src/cv_db/models.py` — `RuntimeNodeTypeRegistry` |
| 3 | `GET /runtime/node-types` liefert `RuntimeNodeTypeCatalogResponse`. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `get_node_types` |
| 4 | Der TypeScript-Client ruft `/api/v1/runtime/node-types` auf. | `frontend/packages/api-client/src/index.ts` — `api.runtimePipelines.nodeTypes` |
| 5 | Die Designer-Seite lädt die Einträge über `nodeTypesQuery`. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `nodeTypesQuery` |
| 6 | `NodePalette` filtert über `eligibleNodeTypesForGraph` und `canAddNodeToGraph`; `isHiddenFromPalette`, Modus-, Starter-, Singleton-, Terminal- und Inputbedingungen können einen Node ausblenden. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodePalette`, `eligibleNodeTypesForGraph`, `canAddNodeToGraph`, `isHiddenFromPalette` |
| 7 | `paletteGroupsForCategory` ordnet nicht hart codierte neue Analysis-Nodes der verbleibenden allgemeinen Analysis-Gruppe zu. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `paletteGroupsForCategory` |
| 8 | `buildNode` übernimmt Ports, Kategorie und `default_config` aus dem Katalog in `RuntimeNodeSpec`. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `buildNode`; `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodeSpec` |
| 9 | `NodePropertiesModal` gruppiert `property_schema`; `NodeField` rendert die Felder. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodePropertiesModal`, `groupPropertyFields`, `NodeField` |

Ein neuer Node benötigt somit keine individuelle Frontendänderung, wenn er ausschließlich vorhandene Property-Typen und vorhandene Port-Datentypen verwendet und keine eigene Interaktionskomponente benötigt. Codebeleg: `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type`, `RuntimeNodePort`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`.

Die beiden Stamm-Temporal-Nodes erfüllen genau diesen Pfad: Ihre `property_schema`-Einträge verwenden nur `text`, `number`, `integer`, `boolean`, `select` und `roi`; deshalb ist keine individuelle Designer-Komponente hinzugekommen. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node-Typen `analysis.stem_surface_rotation`, `analysis.stem_temporal_orientation`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`.

Ein neuer strukturierter Property-Typ benötigt dagegen mindestens eine Erweiterung von `RuntimeNodePropertySpec.field_type`, neu generierte Schemas und einen Rendererzweig in `NodeField`. Für `camera_intrinsic_calibration` sind diese Erweiterungen jetzt vorhanden; daraus folgt keine automatische Unterstützung weiterer strukturierter Typen. Codebeleg: `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type`, `RuntimeCameraIntrinsicCalibrationProfile`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`, `CameraIntrinsicCalibrationEditor`; `package.json` — Skript `generate:openapi`.

Für neue fachliche Resultfelder reicht die automatische Palettenanzeige nicht: `_known_result_fields_for_node` liefert für unbekannte Node-Typen nur `result` und `decision`. Soll der Result-Projection-Editor konkrete Stammfelder anbieten, muss der vorhandene Backend-Katalog an diesem Symbol erweitert werden. Codebeleg: `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `runtime_pipeline_output_catalog`, `_known_result_fields_for_node`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `ResultProjectionEditor`.

## 5. Vorhandener Preview-/File-Test-Pfad

### 5.1 Einzelbild-Preview

| Schritt | Bestehender Pfad | Codebeleg |
|---|---|---|
| Requestvertrag | Der Request nimmt `graph` oder `authoring_graph`, optional `preview_frame`, `selected_node_key` und `include_capture_outputs` auf. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimePipelinePreviewRequest` |
| Frontendaufruf | `previewMutation` sendet den aktuellen `authoring_graph`, den effektiven Modus, einen optional eingefrorenen Frame und den ausgewählten Node. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `previewMutation` |
| Client/API | `api.channels.pipelinePreview` ruft `POST /api/v1/runtime/channels/{channel_id}/pipeline-preview` auf. | `frontend/packages/api-client/src/index.ts` — `api.channels.pipelinePreview`; `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `preview_channel_pipeline_graph` |
| Framewahl | Die Route verwendet den expliziten `preview_frame`, andernfalls den letzten Live-View-Frame; ohne Frame liefert sie einen Recovery-Hinweis. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `preview_channel_pipeline_graph`, `_frame_payload_from_live_state` |
| Teilausführung | `_preview_graph_through_selected_node` reduziert den Graphen auf den ausgewählten Node und dessen Vorfahren; anschließend baut und führt `ExecutablePipelineGraph` den Graphen aus. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `_preview_graph_through_selected_node`, `preview_channel_pipeline_graph`; `backend/packages/cv-runtime/src/cv_runtime/engine.py` — `ExecutablePipelineGraph` |
| Sicherheitsgrenze | Preview setzt `authoring_preview=True`, `include_capture_outputs` kontrolliert Capture-Ausgaben, und `plc_writes_allowed=False`; der Zustand ist pro Request frisch. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `preview_channel_pipeline_graph`; `backend/packages/cv-runtime/src/cv_runtime/siemens_sps.py` — `_dry_run_reason` |

### 5.2 Datei- und Sequenztest

Der produktionsnahe bestehende Weg ist ein Channel mit `RuntimeSourceType.FILE_TEST`, dessen Capture-Prozess über `build_frame_source` eine `FileTestFrameSource` erzeugt; die Pipeline selbst verwendet `source.channel_frame`. Codebeleg: `backend/packages/cv-status/src/cv_status/enums.py` — `RuntimeSourceType.FILE_TEST`; `backend/services/camera-media/src/camera_media/worker.py` — `_capture_process_main`; `backend/services/realtime-executor/src/realtime_executor/sources.py` — `build_frame_source`, `FileTestFrameSource`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node `source.channel_frame`.

`FileTestFrameSource` lädt eingebettete Fotos, statische Bilddateien oder Videos, liest Video über OpenCV, unterstützt `loop` und taktet über `max_fps`, `fps` oder die native Video-FPS. Ein höher konfiguriertes `fps` kann die Dateiausgabe beschleunigen; es existiert aber kein eigener Playback-Speed- oder aufgezeichneter-Zeitachsenvertrag. Codebeleg: `backend/services/realtime-executor/src/realtime_executor/sources.py` — `FileTestFrameSource.__init__`, `FileTestFrameSource.read`, `FileTestFrameSource._read_video_frame`.

Der Registry-Node `source.file_test` ist ein anderer, älterer Pfad: Er ist `hidden_from_palette`, `development_only` und nennt `source.channel_frame` als Ersatz; `FileTestSourceHandler.execute` liest ohne bereitgestellten Kontextframe nur ein einzelnes Bild über `_frame_from_path`. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node `source.file_test`; `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `FileTestSourceHandler.execute`, `_frame_from_path`.

`validate_pipeline_graph` empfiehlt für neue Realtime-Pipelines `source.channel_frame` und erlaubt in Trigger-/Interval-Pipelines nur die dort explizit aufgezählten Source-Typen; ein neuer Operator-Graph sollte deshalb nicht mit dem versteckten `source.file_test` authoriert werden. Codebeleg: `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `validate_pipeline_graph`.

## 6. Result-History-/Persisted-Result-Pfad

| Schritt | Bestehender Pfad | Codebeleg |
|---|---|---|
| Fachausgabe | Ein fachlicher Handler liefert `ResultPayload`; `ResultOutputHandler` reicht den Result-Port des `output.result`-Nodes weiter. | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `ResultPayload`; `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `ResultOutputHandler`, `output_result` |
| Engine-Aggregation | `_persisted_results` übernimmt ausschließlich `ResultPayload` aus `output.result` beziehungsweise dem veralteten `output.persisted_result` oder die History-Zielprojektion des Result-Outputs. | `backend/packages/cv-runtime/src/cv_runtime/engine.py` — `_persisted_results` |
| Opt-in, Sampling und Entscheidungsereignis | History bleibt standardmäßig deaktiviert. Bei aktivierter Policy sampelt `_history_result_persist_due` normale Resultate nach `sample_interval_seconds`; den ersten entscheidungsbereiten `stem.orientation.v2`-Stand einer Session persistiert sie unabhängig vom Intervall sofort und unterdrückt danach Wiederholungen desselben eingefrorenen Ereignisses. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeResultHistoryPolicy`; `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker._history_result_persist_due`, `_stem_orientation_history_event_key` |
| Datenbankeintrag | `_publish_pipeline_work_result` ruft bei fälliger History, Capture-Intents, PLC-Fehlern oder nicht transienten Pipelinefehlern `record_runtime_execution_result` auf. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker._publish_pipeline_work_result`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `record_runtime_execution_result` |
| Kompakte Speicherung | `_orientation_result` begrenzt beide Zeitreihen deterministisch auf höchstens 32 Stützstellen; `record_runtime_execution_result` setzt `history_retained` nur bei aktivierter Policy und fälligen `persisted_results`, klassifiziert die Entscheidung als `stem_orientation`, und `_without_inline_image_data` entfernt Inline-Bilddaten. | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result`, `_compact_temporal_trace`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `record_runtime_execution_result`, `_result_type`, `_compact_result_record_payload`, `_without_inline_image_data` |
| Lesen und Filtern | `list_runtime_result_records` und `get_runtime_result_detail` geben nur `history_retained=True` zurück. Der Result-Service filtert über `result_type=stem_orientation`; die Textsuche umfasst außerdem `derived_outputs` und `trigger_response`, damit Track-/Session-IDs auffindbar sind. | `backend/packages/cv-db/src/cv_db/result_store.py` — `list_runtime_result_records`, `_apply_filters`; `backend/services/result-service/src/result_service/api/routes.py` — `list_runtime_results`, `get_runtime_result` |
| Versioniertes Schnellreview | `create_stem_orientation_review` sperrt den unveränderten Eltern-Resultdatensatz, akzeptiert nur entscheidungsbereite `stem.orientation.v2`-Resultate und fügt eine neue Revision mit `supersedes_review_id` an. GET/POST bleiben im bestehenden Result-Service. | `backend/packages/cv-db/src/cv_db/result_store.py` — `create_stem_orientation_review`, `list_stem_orientation_reviews`; `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeStemOrientationReviewCreateRequest`, `RuntimeStemOrientationReview`; `backend/services/result-service/src/result_service/api/routes.py` — `get_stem_orientation_reviews`, `append_stem_orientation_review` |
| Frontend | `ResultDetailPanel` erkennt `stem.orientation.v2`, zeigt die eingefrorene Vor-Einzug-Empfehlung einschließlich Blickkonvention, Confidence, Decision Frame, Reason Codes, kompakten Spuren und getrenntem Nachkontrollbereich und bettet `StemOrientationReviewPanel` in dieselbe Result-History-Seite ein. | `frontend/apps/admin-web/src/features/results/result-history-page.tsx` — `ResultDetailPanel`, `StemOrientationResultSummary`, `StemOrientationReviewPanel` |
| Aufbewahrung | Der normale Maintenance-Job begrenzt Result-History anhand `retention_hours` und `max_records`; `_result_has_no_active_dependents` schließt Resultate mit einer dauerhaften `StemOrientationReview` von dieser Löschung aus. | `backend/workers/maintenance-worker/src/maintenance_worker/jobs.py` — `runtime_result_retention`, `_result_has_no_active_dependents`; `backend/packages/cv-db/src/cv_db/models.py` — `StemOrientationReview` |

Persistenzentscheidung: `ResultRecord` reicht für das unveränderliche, kompakte Hauptresult einschließlich der getrennten optionalen Nachkontrolle aus. Es reicht nicht für eine append-only Revisionshistorie mit Zielintervallen, Revieweridentität und Supersession, ohne das ursprüngliche Result-JSON nachträglich zu verändern. Deshalb ist genau eine neue Tabelle zulässig und umgesetzt: `backend/packages/cv-db/src/cv_db/models.py` — `StemOrientationReview`; `backend/migrations/versions/20260803_0035_stem_orientation_reviews.py` — `upgrade`, `downgrade`. `downgrade` löscht die Bedienerdaten absichtlich nicht; ein Roll-forward verwendet die vorhandene Tabelle idempotent weiter.

Offen: Der derzeitige Code erzeugt noch keine Roh-/Diagnosevideo- oder Frame-Artefakte für den Stammfall. `_orientation_result` speichert deshalb ausschließlich logische ausgewählte Frameverweise mit `asset_ref=null`, `asset_references.status="not_recorded"` und dem Reason Code `temporal_asset_recorder_not_configured`; konkrete URIs werden nicht erfunden. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result`, `_selected_frame_references`.

## 7. Vorhandener Kalibrierungs-Frame-Workflow

| Schritt | Bestehender Workflow | Codebeleg |
|---|---|---|
| UI-Auslöser | `calibrationCaptureMutation` ruft für den ausgewählten Kalibrierungschannel `api.channels.captureCalibrationFrame` auf und hält die Antwort nur im React-State `capturedCalibrationLiveState`. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `calibrationCaptureMutation`, `capturedCalibrationLiveState` |
| API | `POST /runtime/channels/{channel_id}/calibration-frame` ist nur für einen gestoppten/inaktiven Channel zulässig und verlangt eine konfigurierte Source. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `capture_channel_calibration_frame` |
| Exklusivität | `_CALIBRATION_CAPTURE_LOCK` erlaubt nur eine gleichzeitige exklusive Einzelbildaufnahme. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `_CALIBRATION_CAPTURE_LOCK`, `capture_channel_calibration_frame` |
| Source-Wiederverwendung | `_capture_calibration_frame_from_source` verwendet den existierenden `build_frame_source`, liest bis zum Timeout einen Frame, begrenzt dessen Größe und schließt die Source im `finally`-Pfad. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `_capture_calibration_frame_from_source`; `backend/services/realtime-executor/src/realtime_executor/sources.py` — `build_frame_source` |
| Bestehende Kalibrierung | `board_surface_calibration` ist ein vorhandener Property-Typ; `BoardSurfaceCalibrationEditor` nimmt vier Bildpunkte und metrische Flächenmaße auf und schreibt sie über den normalen Node-Config-Pfad. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `BoardSurfaceCalibrationEditor`, `NodePropertiesModal` |
| Audit | Die API schreibt das Ereignis `runtime.channel.calibration_frame.captured.v1`, persistiert aber das Bild selbst nicht als Kalibrierungsbeobachtung. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `capture_channel_calibration_frame`, `write_runtime_audit_event` |
| Intrinsische Beobachtungsserie | `CameraIntrinsicCalibrationEditor` sammelt wiederholte Antworten des vorhandenen Capture-Endpunkts nur im React-State, erlaubt das Ein-/Ausschließen einzelner Bilder und sendet die ausgewählte Serie zur Berechnung. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `CameraIntrinsicCalibrationEditor`, `calibrationCaptureMutation` |
| Intrinsische Berechnung | `calculate_channel_camera_calibration` reicht Checkerboard- oder ChArUco-Beobachtungen an `calculate_camera_calibration` weiter; zurück kommen Profil, Einzelbildfehler sowie Roh-/Undistort-Vorschau. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `calculate_channel_camera_calibration`; `backend/packages/cv-runtime/src/cv_runtime/camera_calibration.py` — `calculate_camera_calibration` |
| Versionierung und Freigabe | Ein gültiges `RuntimeCameraIntrinsicCalibrationProfile` wird in die Node-Konfiguration übernommen. `_valid_camera_intrinsic_calibration` blockiert ungültige Profile; `create_pipeline_version` und `publish_pipeline_version` verwenden den bestehenden Pipelineversions- und Channel-Bindungspfad. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeCameraIntrinsicCalibrationProfile`; `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `_valid_camera_intrinsic_calibration`, `create_pipeline_version`, `publish_pipeline_version` |

Die intrinsischen Beobachtungsbilder bleiben bewusst transient; nur das Ergebnisprofil wird über die Node-Konfiguration persistiert. Die Entscheidung gegen eine zusätzliche Tabelle ist dokumentiert. Codebeleg: `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `CameraIntrinsicCalibrationEditor`; `docs/stem-orientation/camera-calibration-persistence.md` — Abschnitt `Entscheidung`.

`board_cut.py` besitzt zwar interne optionale Unterstützung für ein untypisiertes Config-Feld `camera_intrinsics` und die Hilfsfunktion `_undistort_normalized_points`; weder Registry-Property noch Designer stellen dieses Feld bereit. Codebeleg: `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py` — `BoardViewCutConstraintsHandler.execute`, `EndFaceCutConstraintsHandler.execute`, `_undistort_normalized_points`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Property-Schemas der Board-Cut-Nodes.

## 8. Trainings-, Modellkatalog-, Export- und Inferenzgrenzen

| Grenze | Verifizierter Ist-Zustand | Codebeleg |
|---|---|---|
| Task-Typen | Es existieren genau `classification`, `detection`, `oriented_detection`, `segmentation` und `anomaly`; Regression, Sequence und Multi-Task fehlen. | `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType` |
| Trainerwahl | `resolve_trainer_architecture` prüft die Capability-Matrix; `get_default_trainer` wählt nur die vorhandenen bildbezogenen Backends für diese Task-Typen. | `backend/packages/cv-training/src/cv_training/trainer.py` — `resolve_trainer_architecture`, `get_default_trainer` |
| Trainerkonfiguration | `TrainerConfig` enthält bildbezogene Größen wie `image_size`, aber keinen Sequence-Length-, Feature-Sequence- oder Multi-Head-Vertrag. | `backend/packages/cv-training/src/cv_training/trainer_types.py` — `TrainerConfig` |
| Metriken/Promotion | Der Trainingskatalog verzweigt nur über die vorhandenen `TaskType`-Werte und kennt keine Temporal-/Regression-Metriken. | `backend/packages/cv-training/src/cv_training/catalog.py` — `metric_definitions_for_task`, `default_promotion_criteria`, `trainer_framework_for_task` |
| Modellkatalog | `SUPPORTED_ARCHITECTURE_MATRIX` ist die geschlossene End-to-End-Matrix; `supported_model_architectures` markiert deren Einträge als Training-, Export-, Deployment- und Inferenz-fähig. | `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`, `supported_model_architectures`, `supported_model_capability` |
| Trainingsartefakt/Export | `_expected_recipe_path` kennt nur fünf Task-Verzeichnisse; `_build_io_schema` beschreibt ein CHW-Bildtensor-Input und taskabhängige Bildausgaben. | `backend/workers/training-worker/src/training_worker/runtime.py` — `_expected_recipe_path`, `_build_io_schema` |
| Deploymentpaket | `build_export_manifest_payload` erzeugt den vorhandenen ONNX-/TensorRT-Exportvertrag; `build_deployment_package` übernimmt diesen in das Serving-Paket. | `backend/packages/cv-deploy/src/cv_deploy/packaging.py` — `build_export_manifest_payload`, `build_deployment_package` |
| Inferenz | `PublicGatewayMixin` stellt nur Classification-, Detection-/OBB-, Segmentation- und Anomaly-Fähigkeiten bereit und prüft sie über `_ensure_task`. | `backend/services/inference-gateway/src/inference_gateway/runtime/public_service.py` — `PublicGatewayMixin._ensure_task`, `classify_public`, `detect_public`, `segment_public`, `anomaly_public` |

Folgerung: Die deterministische Temporal-Logik kann innerhalb eines stateful Runtime-Handlers implementiert werden, aber ein trainiertes TCN-/GRU-/Multi-Task-Modell kann nicht als bereits unterstützter Modelltyp ausgegeben werden. Ob ein spezialisierter Loader im Node oder ein neuer allgemeiner Task-Typ entsteht, bleibt die in `docs/Stammausrichtung_Inferenzserver/Spezifikation.md` unter `17.2 Verbindliche ADR` ausdrücklich offene Entscheidung; die bestehenden Grenzen sind `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX` und `backend/services/inference-gateway/src/inference_gateway/runtime/public_service.py` — `PublicGatewayMixin._ensure_task`.

Das Zieldeployment bleibt der vorhandene Docker-freie Windows-/WSL2-Pfad; Installer und Updates werden mit den vorhandenen Skripten gebaut, und persistente Plattformdaten liegen außerhalb des Release-Symlinks. Codebeleg: `deployment/README.md` — Abschnitte `Docker-freier Windows/WSL2 Offline-Installer`, `Persistenz- und Release-Layout`, `Update-Bundle`; `deployment/build-windows-installer.ps1` — Skripteinstieg; `deployment/export-windows-update.ps1` — Skripteinstieg; `deployment/wsl/apply-release.sh` — `CURRENT_LINK`.

## 9. Exakte vorhandene Test-, Schema- und Buildbefehle

Die folgenden Befehle sind im Repository selbst deklariert oder dokumentiert; sie wurden für diesen reinen Dokumentationsauftrag nicht ausgeführt.

### 9.1 CI-validierter Hauptpfad

| Zweck | Exakter Befehl | Repositorybeleg |
|---|---|---|
| Python-Workspace synchronisieren | `uv sync --dev` | `.github/workflows/platform-validation.yml` — Job `backend-contracts-and-integration`, Schritt `Sync Python workspace` |
| Config-Schema generieren | `uv run python scripts/dev/export_config_schema.py` | `.github/workflows/platform-validation.yml` — Schritt `Refresh generated schema and OpenAPI`; `package.json` — Skript `generate:config-schema` |
| OpenAPI generieren | `uv run python scripts/dev/export_openapi.py` | `.github/workflows/platform-validation.yml` — Schritt `Refresh generated schema and OpenAPI`; `package.json` — Skript `generate:openapi` |
| Generierte Artefakte prüfen | `git diff --exit-code -- config/schemas/platform-settings.schema.json contracts/openapi` | `.github/workflows/platform-validation.yml` — Schritt `Assert generated artifacts are committed` |
| Backend Unit/Contract/Integration | `uv run pytest tests/unit/backend tests/contract tests/integration -ra` | `.github/workflows/platform-validation.yml` — Schritt `Run backend unit, contract, and integration suites` |
| E2E/Performance/Acceptance | `uv run pytest tests/e2e tests/performance tests/acceptance -ra` | `.github/workflows/platform-validation.yml` — Schritt `Run e2e, performance smoke, and acceptance suites` |
| Frontend-Abhängigkeiten | `pnpm install --frozen-lockfile` | `.github/workflows/platform-validation.yml` — Job `frontend-build`, Schritt `Install frontend dependencies` |
| Admin-Web bauen | `pnpm build:admin-web` | `.github/workflows/platform-validation.yml` — Schritt `Build admin web`; `package.json` — Skript `build:admin-web` |

### 9.2 Weitere vorhandene Root-/Frontend-Skripte

| Zweck | Exakter Befehl | Repositorybeleg |
|---|---|---|
| Frontend linten | `pnpm lint:frontend` | `package.json` — Skript `lint:frontend` |
| Frontend testen | `pnpm test:frontend` | `package.json` — Skript `test:frontend` |
| Admin-Web direkt bauen | `pnpm --filter @cv-platform/admin-web build` | `package.json` — Skript `build:admin-web`; `frontend/apps/admin-web/package.json` — Skript `build` (`tsc --noEmit && vite build`) |
| Admin-Web direkt testen | `pnpm --filter @cv-platform/admin-web test` | `frontend/apps/admin-web/package.json` — Skript `test` (`vitest run`) |
| Admin-Web direkt linten | `pnpm --filter @cv-platform/admin-web lint` | `frontend/apps/admin-web/package.json` — Skript `lint` (`eslint src --ext .ts,.tsx`) |

### 9.3 Dokumentierte fokussierte Testpfade

| Zweck | Exakter Befehl | Repositorybeleg |
|---|---|---|
| Runtime-Integration Unit | `python -m pytest tests/unit/backend/test_runtime_pipeline_definition_system.py tests/unit/backend/test_runtime_channel_management.py tests/unit/backend/test_runtime_execution_core.py tests/unit/backend/test_runtime_manual_capture.py tests/unit/backend/test_runtime_lifecycle_core.py tests/unit/backend/test_realtime_live_path.py` | `docs/runtime/end-to-end-runtime-integration.md` — Abschnitt mit fokussierten Runtime-Tests |
| Runtime-Verträge | `python -m pytest tests/contract/test_runtime_plane_contracts.py tests/contract/test_platform_security_contracts.py tests/unit/backend/test_runtime_domain_contracts.py` | `docs/runtime/end-to-end-runtime-integration.md` — Abschnitt mit fokussierten Contract-Tests |
| Acceptance | `uv run pytest tests/acceptance -ra` | `tests/acceptance/README.md` — Abschnitt `Run with` |
| E2E | `uv run pytest tests/e2e -ra` | `tests/e2e/README.md` — Abschnitt `Run with` |
| Performance-Smoke | `uv run pytest tests/performance -ra` | `tests/performance/README.md` — Abschnitt `Run with` |

### 9.4 Vorhandene Windows-/WSL2-Build- und Updatebefehle

| Zweck | Exakter Befehl | Repositorybeleg |
|---|---|---|
| Deployment-UI bauen | `.\deployment\build-windows-ui-exe.ps1` | `deployment/README.md` — Abschnitt `Docker-freier Windows/WSL2 Offline-Installer` |
| Offline-Installer bauen | `.\deployment\build-windows-installer.ps1` | `deployment/README.md` — Abschnitt `Installer ohne UI bauen` |
| Vorbereitete Runtime nur exportieren | `.\deployment\build-windows-installer.ps1 -SkipRuntimePrepare` | `deployment/README.md` — Abschnitt `Installer ohne UI bauen` |
| Update-Bundle exportieren | `.\deployment\export-windows-update.ps1` | `deployment/README.md` — Abschnitt `Update-Bundle` |
| Modellupdate exportieren | `.\deployment\export-pretrained-models-update.ps1 -All` | `deployment/README.md` — Abschnitt `Pretrained-Modelle offline aktualisieren` |
| Zielsystem aktualisieren | `.\update-ybb-inferenzserver.cmd` | `deployment/README.md` — Abschnitte `Updates einspielen`, `Update-Bundle` |

## 10. Kollisionen und offene Abweichungen gegenüber `Spezifikation.md`

| Nr. | Annahme beziehungsweise Zielaussage der Spezifikation | Reale Abweichung | Codebeleg und Status |
|---|---|---|---|
| 1 | Die Zieldarstellung und Sequenztests nennen `source.file_test` als normalen Pipeline-Source. | Der gleichnamige Registry-Node ist versteckt und development-only; neue Operator-Pipelines sollen einen FILE_TEST-Channel mit `source.channel_frame` verwenden. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node `source.file_test`, Node `source.channel_frame`; `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `validate_pipeline_graph`. **Kollision; Spezifikation sollte den Channel-/Source-Adapter-Pfad präzisieren.** |
| 2 | „Echtzeit- oder beschleunigte Wiedergabe“ wird als vorhandene Source-Konfiguration beschrieben. | `FileTestFrameSource` besitzt `fps`/`max_fps` und `loop`, aber keinen expliziten Speed-Faktor oder Vertrag für aufgezeichnete Originalzeitstempel. | `backend/services/realtime-executor/src/realtime_executor/sources.py` — `FileTestFrameSource.__init__`, `FileTestFrameSource.read`. **Teilweise erfüllt; Zeitachsen-Semantik offen.** |
| 3 | Die intrinsische Kalibrierung „erweitert“ den bestehenden Bedienpfad um mehrere Beobachtungen und Berechnung. | Die ursprüngliche Lücke ist geschlossen: Die exklusive Einzelbildaufnahme bleibt bestehen, während der Designer mehrere transiente Beobachtungen sammelt und der bestehende Runtime-Admin-Dienst sie berechnet. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `capture_channel_calibration_frame`, `calculate_channel_camera_calibration`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `CameraIntrinsicCalibrationEditor`. **Erledigt in Phase D.** |
| 4 | Der neue strukturierte Typ `camera_intrinsic_calibration` wird als einzige neue Property angenommen. | Der Vertrag und der notwendige individuelle Renderer sind jetzt vorhanden. Andere neue strukturierte Property-Typen werden weiterhin nicht automatisch gerendert. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type`, `RuntimeCameraIntrinsicCalibrationProfile`; `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`, `CameraIntrinsicCalibrationEditor`. **Erledigt in Phase D.** |
| 5 | `preprocessing.camera_undistort` und `RuntimeImageTransform.transform_type="undistort"` werden vorausgesetzt. | Beide Erweiterungspunkte sind jetzt vorhanden; `CameraUndistortHandler` führt ausschließlich intrinsisches Remapping durch und kennzeichnet die Transformation als nicht projektierbar. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `BUILTIN_NODE_TYPES`; `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `RuntimeImageTransform.transform_type`; `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` — `CameraUndistortHandler`. **Erledigt in Phase D.** |
| 6 | Nur finale und markante Stammresultate sollen in der vorhandenen History landen. | Der erste entscheidungsbereite `stem.orientation.v2`-Stand wird jetzt bei aktivierter History-Policy als semantisches Ereignis sofort persistiert; Wiederholungen des eingefrorenen Ergebnisses werden nicht erneut nach Zeit gesampelt. Andere Resulttypen behalten ihre bestehende Sampling-Semantik. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker._history_result_persist_due`, `_stem_orientation_history_event_key`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `_result_type`. **Für das Vor-Einzug-Hauptereignis in Phase G erledigt.** |
| 7 | Eine beim Runtime-Neustart verlorene, nicht beendete Session soll als `aborted_runtime_restart` resultieren. | Der State wird im `finally`-Block nur verworfen; `RuntimeNodeHandler` besitzt keinen Shutdown-/Finalize-Hook, über den der alte Session-State noch einen Result erzeugen könnte. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `RealtimeRuntimeWorker.run`; `backend/packages/cv-runtime/src/cv_runtime/handlers.py` — `RuntimeNodeHandler`. **Kollision; Lebenszyklus-Hook oder andere bestehende Integrationslösung muss vor Implementierung entschieden werden.** |
| 8 | Die vorhandene Result-History soll Quelle für eine nachträglich versionierte Bedienerannotation sein oder minimal um `stem_orientation_reviews` ergänzt werden. | Das Hauptresult bleibt unverändert in `ResultRecord`; append-only Reviews mit Zielintervallen benötigen wegen Revision, Reviewer und Supersession eine normalisierte Zusatztabelle. Diese Tabelle, Verträge, Result-Service-Routen und das integrierte Review sind jetzt vorhanden. | `backend/packages/cv-db/src/cv_db/models.py` — `ResultRecord`, `StemOrientationReview`; `backend/migrations/versions/20260803_0035_stem_orientation_reviews.py` — `upgrade`, `downgrade`; `backend/packages/cv-db/src/cv_db/result_store.py` — `create_stem_orientation_review`; `backend/services/result-service/src/result_service/api/routes.py` — `get_stem_orientation_reviews`, `append_stem_orientation_review`; `frontend/apps/admin-web/src/features/results/result-history-page.tsx` — `StemOrientationReviewPanel`. **Entschieden und in Phase G umgesetzt.** |
| 9 | Spätere Temporalmodelle könnten nach ADR in Trainer/Modellkatalog/Export/Gateway integriert werden; zugleich darf kein bestehender Task missbraucht werden. | Im Ist-Code gibt es tatsächlich keinen Sequence-, Regression- oder Multi-Task-Vertrag. Auch der Exportinput ist ein einzelner CHW-Bildtensor. | `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType`; `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`; `backend/workers/training-worker/src/training_worker/runtime.py` — `_build_io_schema`; `backend/services/inference-gateway/src/inference_gateway/runtime/public_service.py` — `PublicGatewayMixin._ensure_task`. **Bestätigter offener Punkt, keine vorhandene Unterstützung.** |
| 10 | `deployment/rollback.ps1` wird zusammen mit Windows-/WSL2-Update und -Rollback als Zielpfad genannt. | `deployment/rollback.ps1` arbeitet auf `C:\DockerApps`, `images.tar` und Docker Compose und gehört zur generischen Docker-Bundle-Schiene. Für den Docker-freien WSL2-Updater dokumentiert `deployment/README.md` Export und Apply, aber keinen entsprechenden Release-Rollbackbefehl. | `deployment/rollback.ps1` — Parameter `ServerBaseDir`, Rollback-Block mit `images.tar`/Docker Compose; `deployment/README.md` — Abschnitte `Update-Bundle`, `Offline-Deployment fuer Docker-Anwendungen auf Windows`, `Rollback`; `deployment/update-windows-offline.ps1` — `Backup-WslConfig` sichert nur `.wslconfig`. **Kollision; Docker-freier Rollbackpfad ist offen.** |
| 11 | Die Observability-Anforderungen nennen Runtime-State-Größe und letzte Kalibrierungsvalidität als Metriken. | Der vorhandene Diagnostics-Vertrag und `_live_diagnostics` liefern diese beiden Felder nicht. | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeLiveDiagnostics`; `backend/services/realtime-executor/src/realtime_executor/live.py` — `_live_diagnostics`. **Nicht implementierter Observability-Erweiterungspunkt.** |
| 12 | Stateful-Verhalten wird durch Benutzung von `RuntimeExecutionContext.state` nahegelegt. | Die reale Garantie hängt zusätzlich von der separaten Allowlist ab. Zwei vorhandene state-nutzende Nodes fehlen bereits darin; alle drei implementierten Stamm-Nodes mit zeitlichem State erfüllen den Pflichtschritt inzwischen. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`, `_stateful_pipeline_nodes`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py` — `ActiveStemTrackerHandler.execute`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemSurfaceRotationHandler.execute`, `StemTemporalOrientationHandler.execute`; `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `ObjectSpatialSignalHandler.execute`, `MovementDirectionHandler.execute`. **Für die Stamm-Nodes behoben; bestehende Plattformabweichung außerhalb dieses Auftrags bleibt offen.** |
| 13 | Die aktualisierte Spezifikation ordnet einen reinen Remap-Cache nicht als fachlich stateful ein. | `preprocessing.camera_undistort` steht aus der vorherigen Implementierungsphase weiterhin in `_STATEFUL_PIPELINE_NODE_TYPES` und serialisiert dadurch einen Graphen auch dann, wenn kein fachlich zeitabhängiger Node vorhanden ist. Diese Aufgabe ändert das aus Rückwärtskompatibilitätsgründen nicht. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`; `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` — `CameraUndistortHandler._remap_for_context`. **Kollision zur aktualisierten Spezifikation; offen.** |

### Verifizierte Annahmen ohne Kollision

| Annahme | Ergebnis | Codebeleg |
|---|---|---|
| Node-Katalog ist Registry-getrieben. | Bestätigt. | `backend/packages/cv-db/src/cv_db/pipeline_store.py` — `ensure_builtin_node_types`, `list_runtime_node_types`; `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `get_node_types` |
| Standardfelder brauchen keine neue Seite. | Bestätigt für vorhandene `field_type`-Werte und ohne neue Interaktionssemantik. | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` — `NodeField`; `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeNodePropertySpec.field_type` |
| Einzelbild-Preview reicht nicht für einen vollständigen Temporalablauf. | Bestätigt, weil jeder Preview-Request einen frischen State erzeugt. | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` — `preview_channel_pipeline_graph`; `backend/packages/cv-runtime/src/cv_runtime/context.py` — `RuntimeExecutionContext.state` |
| Erkannte stateful Realtime-Pipelines laufen geordnet mit maximal einem Inflight-Work-Item. | Bestätigt. | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_stateful_pipeline_nodes`, `_pipeline_max_inflight` |
| Große Inline-Bilder sollen nicht in Result-History landen. | Durch den vorhandenen Persistenzpfad bestätigt. | `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `_compact_result_record_payload`, `_without_inline_image_data` |
| TCN/GRU sind nicht bereits als vorhandene Tasks auszugeben. | Bestätigt. | `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType`; `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX` |
| Es soll keine zweite Runtime-, Kamera-, Datenbank-, Trainings- oder Deployment-Anwendung entstehen. | Die erforderlichen Erweiterungspunkte liegen vollständig in den vorhandenen Registry-, Handler-, Channel-Source-, Result-, Trainings- und Deploymentpfaden. | `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `BUILTIN_NODE_TYPES`; `backend/services/realtime-executor/src/realtime_executor/sources.py` — `build_frame_source`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `record_runtime_execution_result`; `backend/packages/cv-training/src/cv_training/trainer.py` — `get_default_trainer`; `deployment/README.md` — Abschnitt `Docker-freier Windows/WSL2 Offline-Installer` |
