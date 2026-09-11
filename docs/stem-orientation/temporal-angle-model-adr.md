# ADR: Allgemeine temporale Winkel- und Sequenzmodelle

- **Status:** angenommen als Vorgabe für eine spätere Implementierung
- **Datum:** 2026-08-04
- **Geltungsbereich:** Training, Modellkatalog, Export, Deployment, Inference Gateway und Runtime-Binding für `temporal_sequence`
- **Umsetzungsstand:** nur Architekturentscheidung; diese ADR führt keinen Modell-, API- oder Runtime-Code ein

## Kontext und Leitplanken

Die erste Modellgeneration erhält ausschließlich eine kausale Featuresequenz bis zum letzten gültigen Frame vor der visuellen Entscheidungslinie. SPS- oder Bedienertastensignale sind weder Eingabevoraussetzung noch Ground Truth. Diese fachliche Grenze ist in `docs/Stammausrichtung_Inferenzserver/Spezifikation.md` in `17.3 Datenlage der ersten Winkelmodell-Version` und `18.2 Trainingsfenster` festgelegt. Die bereits implementierte Runtime friert die deterministische Vor-Einzug-Entscheidung in `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` in `StemTemporalOrientationHandler.execute` und `_orientation_result` ein; eine spätere Nachkontrolle ist dort als separates Feld `post_rotation_verification` vorgesehen.

Der Auftrag 07E hat die benötigte Datenbasis bereits allgemein angelegt:

- `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType.TEMPORAL_SEQUENCE` definiert den allgemeinen Task-Typ `temporal_sequence`; ein stammspezifischer Task-Typ ist nicht erforderlich.
- `backend/packages/cv-datasets/src/cv_datasets/models.py` — `CanonicalSequenceItem` und `CanonicalSequenceDataset` trennen referenzierte Medien, Frames bis zum Decision Frame, Human Labels und `DerivedTarget` und erzwingen Gruppensplits.
- `backend/packages/cv-datasets/src/cv_datasets/temporal_snapshot.py` — `materialize_temporal_snapshot` materialisiert den unveränderlichen Snapshot, prüft referenzierte Artefakte und ruft den registrierten Target Builder auf.
- `backend/packages/cv-datasets/src/cv_datasets/target_builders/base.py` — `TargetBuildContext` und `TargetBuildResult` sind schemaunabhängige Erweiterungspunkte.
- `backend/packages/cv-datasets/src/cv_datasets/target_builders/stem_orientation.py` — `build_stem_orientation_targets` erzeugt für `stem_orientation_angle.v1` unter anderem `sin_2e`, `cos_2e`, `angle_target_valid`, Labelqualität und die modulo 180° gewrappte Korrektur.
- `backend/packages/cv-datasets/src/cv_datasets/validation.py` — `validate_temporal_sequence_dataset` prüft Gruppen-/Hash-/Zeitüberschneidungen und verbietet Frames nach dem Decision Frame.

Ein konkret materialisiertes 07E-Snapshot-Artefakt ist nicht im Repository eingecheckt. Belegt sind der Materialisierer und seine synthetische Referenz in `tests/unit/backend/test_temporal_snapshot_materialization.py` — `test_temporal_export_materializes_versioned_signed_angle_snapshot` sowie die Vertrags-/Buildertests in `tests/unit/backend/test_temporal_sequence_datasets.py` — `test_versioned_target_builder_derives_angle_from_interval_and_rotation_trace`. Reale Featureverteilungen, die endgültige Featureordnung und ein belastbarer Umfang von Gold-/Silver-Samples können daraus nicht abgeleitet werden und bleiben in dieser ADR offen.

Damit ist die Frage dieser ADR nicht, ob ein zeitlicher Datensatz als Bildklassifikation umgedeutet werden kann, sondern wie der vorhandene Modelllebenszyklus einen weiteren allgemeinen Task-Typ vollständig unterstützt.

## Festgestellter Ist-Zustand

| Schicht | Vorhandener realer Erweiterungspunkt | Belegte aktuelle Grenze |
| --- | --- | --- |
| Task-Vertrag | `backend/packages/cv-status/src/cv_status/enums.py` — `TaskType.TEMPORAL_SEQUENCE` | Der Enum-Wert existiert bereits. |
| Trainingsrequest | `backend/packages/cv-contracts/src/cv_contracts/training.py` — `TrainingJobCreateRequest`; `backend/services/admin-api/src/admin_api/api/routes/training.py` und `backend/services/training-orchestrator/src/training_orchestrator/api/routes.py` — jeweils `request_training_job` | Beide vorhandenen Submit-Routen verwenden `backend/packages/cv-db/src/cv_db/training_store.py` — `create_training_job_request` und stellen `training_worker.jobs.run_training_job` ein. Der Request ist task- und recipebasiert, aber der Store akzeptiert nur Einträge aus `is_supported_architecture`. |
| Capability-/Templatekatalog | `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`, `supported_model_capability`, `DEFAULT_TEMPLATE_DEFINITIONS` | Für `TaskType.TEMPORAL_SEQUENCE` existiert noch keine Architektur oder Vorlage. |
| Recipes | `backend/packages/cv-training/src/cv_training/recipes.py` — `load_training_recipe`, `_recipe_task_dir` | `_recipe_task_dir` lehnt `temporal_sequence` derzeit ab; unter `ml/configs/task-types` und `cv_training/resources/task-types` gibt es kein entsprechendes Verzeichnis. |
| Trainerdispatch | `backend/packages/cv-training/src/cv_training/trainer.py` — `resolve_trainer_architecture`, `get_default_trainer`, `_default_architecture_for_task` | Kein realer Temporal-Trainer wird ausgewählt. `resolve_trainer_architecture` meldet für den Task ausdrücklich, dass kein Backend vorhanden ist. |
| Trainerprotokoll | `backend/packages/cv-training/src/cv_training/trainer_types.py` — `BaseTrainer`, `TrainerConfig`, `TrainerResult`, `TrainerExportArtifact` | Das Protokoll ist wiederverwendbar; `TrainerConfig.image_size` und `TrainerDatasetArtifact.class_map_path` spiegeln jedoch noch den Bildpfad wider. |
| Dataset-Staging | `backend/packages/cv-training/src/cv_training/dataset_staging.py` — `load_snapshot_training_source`, `stage_framework_dataset` | Es wird ausschließlich `CanonicalDataset` mit `label_map.json` und Bilddateien geladen; der Fallback behandelt jeden unbekannten Task als Klassifikation. |
| Worker-Lebenszyklus | `backend/workers/training-worker/src/training_worker/runtime.py` — `run_training_workflow`, `_load_and_merge_recipe`, `_select_trainer`, `_register_successful_training` | Queue, GPU-Reservierung, Checkpoints, MLflow, DVC und Modellregistrierung sind allgemein nutzbar. `_build_trainer_config`, `_load_training_class_map`, `_build_io_schema` und `_build_preprocessing` setzen momentan Bildgröße, Klassenkarte und CHW-Bildtensor voraus. |
| Modellversionierung | `backend/packages/cv-db/src/cv_db/models.py` — `ModelFamily`, `ModelVersion`, `TrainingJob`; `backend/packages/cv-db/src/cv_db/training_store.py` — `register_trained_model_version` | Task, Architektur, Snapshot, Metriken, Manifeste, ONNX, DVC und immutable Metadaten sind bereits versioniert. Es fehlt kein zwingendes relationales Feld für den allgemeinen Temporalvertrag. |
| Promotion und Releases | `backend/packages/cv-db/src/cv_db/registry_store.py` — `_deployment_policy_errors`, `record_deployment_approval`, `create_promotion_request`, `create_deployment_release_for_promotion` | Derselbe Lebenszyklus kann verwendet werden; die bestehende Policy unterscheidet aber noch nicht zwischen temporalem Shadow-Staging und einer Gold-abgesicherten Produktionsfreigabe. |
| Exportmanifest | `backend/packages/cv-deploy/src/cv_deploy/manifests.py` — `ModelIoSchema`, `ExportManifest`, `DeploymentPackageManifest` | `ModelIoSchema` ist tensororientiert. Die beiden Manifeste verlangen trotzdem `image_size`, Klassenkarte und Klassenzahl. |
| Paketierung | `backend/packages/cv-deploy/src/cv_deploy/packaging.py` — `build_deployment_package`, `_deployment_io_schema` | ONNX, TensorRT-Plan und Tensor-Schema werden bereits paketiert; `_class_map_from_export` und mehrere Fallbackdimensionen sind bild-/klassenorientiert. |
| Triton-Release | `backend/packages/cv-deploy/src/cv_deploy/triton.py` — `render_config_pbtxt`, `TritonRepositoryManager.stage_release`, `TritonRepositoryManager.smoke_test_report` | `render_config_pbtxt` rendert beliebige deklarierte Tensoren. Der aktuelle Smoke-Test prüft Repository, Server und Modellbereitschaft, aber noch keine temporale Testinferenz. |
| Produktionsziel | `deployment/wsl/apply-release.sh` — `patch_release_config` | Der installierte Produktionspfad setzt `training.allow_onnx_deployment_fallback=false`; TensorRT-/Triton-Kompatibilität ist daher ein echtes Freigabegate und darf nicht nur auf dem Entwicklungs-PC angenommen werden. |
| Gateway-Routing | `backend/services/inference-gateway/src/inference_gateway/runtime/resolver.py` — `ReleaseResolver`, `ReleaseContext` | Routing und Manifestauflösung sind taskgenerisch. |
| Gateway-Inferenz | `backend/services/inference-gateway/src/inference_gateway/runtime/service.py` — `TritonInferenceBackend._model_outputs_via_triton`, `TritonInferenceBackend._model_outputs_via_local_onnx`, `RealtimeGatewayService.predict` | Beide Backends erzeugen derzeit aus Bildbytes genau einen Bildtensor. `predict` dispatcht nur Klassifikation, Segmentierung, Anomalie oder Detection. |
| Runtime-Client | `backend/packages/cv-runtime/src/cv_runtime/inference_client.py` — `RuntimeInferenceClient`, `HttpInferenceGatewayClient` | Das Protokoll bietet nur `classify`, `detect`, `segment` und `anomaly`, jeweils bildbasiert. |
| Runtime-Modellbindung | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` — `RuntimeLogicalModelRef`; `backend/packages/cv-runtime/src/cv_runtime/context.py` — `RuntimeModelBinding`, `RuntimeExecutionContext.resolve_model`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `build_runtime_model_resolver` | Diese Symbole sind bereits taskgenerisch und prüfen den `required_task_type` exakt; sie müssen nicht durch einen Stamm-Loader ersetzt werden. |
| Runtime-Node-Inferenz | `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `DetectionInferenceHandler`, `ClassificationInferenceHandler`, `SegmentationInferenceHandler`, `AnomalyInferenceHandler`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — die vier `inference.*`-Spezifikationen | Alle vorhandenen Inferenz-Nodes erwarten einen Frame und einen Bildtask. Ein allgemeiner Temporal-Node fehlt. |
| Stateful Vor-Einzugspfad | `backend/services/realtime-executor/src/realtime_executor/live.py` — `_STATEFUL_PIPELINE_NODE_TYPES`, `RealtimeRuntimeWorker._execute_pipeline_frame`; `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemTemporalOrientationHandler` | Der Stamm-Temporalnode ist bereits serialisiert und teilt `RuntimeExecutionContext.state`. Ein Inferenz-Node kann stateless bleiben, wenn er ein vollständig begrenztes kausales Fenster erhält. |

Die Lücken liegen damit an vorhandenen Dispatch-, Manifest- und Inferenzgrenzen. Es gibt keinen belegten Blocker, der eine zweite Trainings- oder Deploymentarchitektur rechtfertigt.

## Betrachtete Alternativen

### A. Integration in den vorhandenen Training-/Modell-/Deploymentpfad

`temporal_sequence` wird als vollwertiger Task in Recipe-Auflösung, Trainerdispatch, Capability-Katalog, Modellregister, ONNX-/TensorRT-Paketierung, Inference Gateway und Runtime-Registry ergänzt. Die bestehenden Jobs, Modellversionen, Promotionen, Releases, Rollbacks und Modellbindungen bleiben die einzige Quelle der Wahrheit.

Reale Anknüpfungspunkte sind `admin_api.api.routes.training.request_training_job` und `training_orchestrator.api.routes.request_training_job`, `training_worker.runtime.run_training_workflow`, `cv_db.training_store.register_trained_model_version`, `cv_deploy.packaging.build_deployment_package`, `cv_db.registry_store.create_deployment_release_for_promotion`, `inference_gateway.runtime.resolver.ReleaseResolver` und `cv_db.runtime_execution_store.build_runtime_model_resolver` in den oben genannten Dateien.

Vorteile:

- einheitliche Lineage von `DatasetSnapshot` bis `DeploymentRelease` über die bestehenden Modelle in `backend/packages/cv-db/src/cv_db/models.py`;
- vorhandene Checkpoint-, MLflow- und DVC-Verarbeitung über `backend/workers/training-worker/src/training_worker/runtime.py` — `TrainingRunController` und `_register_successful_training`;
- vorhandene Freigabe-, Smoke-, Aktivierungs- und Rollbackmechanismen über `backend/workers/control-worker/src/control_worker/jobs/deployment.py` — `stage_model_release`, `promote_model_release`, `rollback_model_release`;
- normale logische Modellbindung in Runtime-Pipelines über `RuntimeLogicalModelRef` und `RuntimeExecutionContext.resolve_model`;
- Wiederverwendung für weitere sequenzielle Klassifikations-, Regressions- und periodische Winkelaufgaben.

Kosten: Die belegten bildzentrierten Stellen müssen erweitert werden. Diese Arbeiten bleiben jedoch innerhalb der vorhandenen Pakete und Services.

### B. Spezialisiertes Laden im Stamm-Node

Bei dieser Variante würde `analysis.stem_temporal_orientation` ein ONNX-/Torch-Artefakt selbst aus einer URI laden und verwalten.

Diese Variante wird verworfen. Sie würde die reale Modellauflösung in `backend/packages/cv-db/src/cv_db/runtime_execution_store.py` — `build_runtime_model_resolver`, das Gateway-Routing in `backend/services/inference-gateway/src/inference_gateway/runtime/resolver.py` — `ReleaseResolver`, die Concurrency-/Latenzmetriken in `backend/services/inference-gateway/src/inference_gateway/runtime/service.py` — `BaseGatewayService._admission` sowie Release und Rollback in `backend/packages/cv-db/src/cv_db/registry_store.py` umgehen oder duplizieren. Der Stamm-Node müsste zusätzlich Sessions, GPU-/ONNX-Ressourcen, Warmup und Releasewechsel selbst behandeln. Das wäre eine spezialisierte Parallelinfrastruktur und kein bloßer Adapter.

Ein stammspezifischer **fachlicher Ergebnisadapter** hinter einem allgemeinen Temporal-Inferenznode ist dagegen zulässig: Er interpretiert typisierte Modelloutputs und lädt kein Modell selbst.

### C. Abbildung als Bildklassifikation

Diese Variante ist unzulässig und wird verworfen.

`backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` — `ClassificationInferenceHandler` erzeugt aus genau einem `FramePayload` eine `RealtimeClassifyRequest` und liefert `ClassificationPayload`. `backend/services/inference-gateway/src/inference_gateway/runtime/service.py` — `_classification_tensor` baut einen NCHW-Bildtensor. Demgegenüber enthalten `CanonicalSequenceItem.derived_targets` gemischte Klassifikations-, Bool- und Regressionsziele, und `build_stem_orientation_targets` erzeugt ein periodisches Winkelpaar. Eine Bildklassifikation würde Kausalität, Decision Frame, Zeitmaske und Winkelgeometrie verlieren und den tatsächlichen Vertrag falsch ausweisen.

## Entscheidung

**Alternative A wird gewählt.** Es wird ein allgemeiner `temporal_sequence`-Pfad in die vorhandene Architektur integriert. Alternative B bleibt für Modellladung ausgeschlossen; Alternative C bleibt vollständig ausgeschlossen.

Die Entscheidung umfasst folgende Grenzen:

1. Das erste Modell verarbeitet strukturierte Featuresequenzen, kein Rohvideo.
2. Der Modellkern und sein Servingvertrag bleiben allgemein; `stem_orientation_angle.v1` ist eine Recipe-/Head-Konfiguration, kein neuer Task-Typ.
3. Die deterministische Baseline bleibt verpflichtender Vergleich und unabhängiger Runtime-Fallback.
4. Ein Modellresultat bleibt zunächst Shadow Mode. Es führt zu keiner SPS-Schreiboperation.
5. Ohne kompatible Feature-, Rotations-, Kalibrierungs- und Richtungsversion oder ohne ausreichende Modellkonfidenz wird keine Gradzahl ausgegeben.

## Allgemeiner Sequenz- und Winkelvertrag

### Kausaler Eingang

Für Phase 1 wird ein statisch begrenzter Tensor `input__sequence_features` mit Form `[N, F]` pro Request festgelegt; Triton ergänzt die Batchdimension. `N` und `F` sind keine global geratenen Konstanten, sondern stehen im versionierten Modellvertrag. Fehlende frühe Zeitschritte werden links aufgefüllt. Ein explizites Gültigkeitsmerkmal je Zeitschritt sowie die fachlichen Validitätsmasken sind Teil der geordneten Featureliste. Dadurch bleibt die erste Servingversion bei einem Eingangstensor, obwohl `backend/packages/cv-deploy/src/cv_deploy/manifests.py` — `ModelIoSchema.input_tensors` spätere Mehrfacheingänge grundsätzlich abbilden kann.

Der Vertrag enthält mindestens:

- `feature_schema_id`, Schema-Digest und exakt geordnete Featurenamen;
- Datentyp, `minimum_window_steps`, `maximum_window_steps`, Paddingwert und Paddingrichtung;
- Abtastraten-/Zeitabstandsregeln und pro Zeitschritt Frameindex plus Zeitstempel;
- `causal=true`, `input_end=decision_frame_or_earlier` und `post_decision_frames_allowed=false`;
- train-only Scalerreferenz und Checksumme;
- optionale Missing-/Validity-Masken als benannte Features;
- unterstützte Kalibrierungs-, Rotationstrace- und Blickkonventionen.

Die erste stammbezogene Featurefolge wird aus den bereits typisierten Beobachtungen in `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `StemGeometryPayload` und `StemRotationPayload` aufgebaut. Sie enthält die in `Spezifikation.md` unter `18.1 Eingang pro Zeitschritt` geforderten Profile, Masken, Qualitäts-, Tracking-, Positions-, Rotations- und Merkmalsstatistiken. Die endgültige Featureordnung bleibt bis zur Implementierung offen und wird erst nach Inventarisierung der realen `features.jsonl`-Felder in `backend/packages/cv-runtime/src/cv_runtime/temporal_samples.py` — `_write_primary_artifacts` als neues Schema versioniert; sie wird nicht in dieser ADR geraten.

### Allgemeine Modelloutputs

Der ONNX-Vertrag deklariert benannte Outputs statt eines impliziten Klassenvektors:

- `output__bow_relevance_probability`;
- `output__target_observed_probability`;
- `output__angle_sin_2e`;
- `output__angle_cos_2e`;
- `output__angle_confidence` oder eine äquivalente, im Vertrag benannte Unsicherheit;
- optional `output__final_quality_probabilities` mit der versionierten Klassenreihenfolge `good`, `acceptable`, `bad`;
- optional `output__out_of_distribution_score` als separat kalibrierter Fail-closed-Indikator gemäß `docs/Stammausrichtung_Inferenzserver/Spezifikation.md` — Abschnitt `18.3 Ausgänge`; er darf nicht ohne definierte Trainings-/Validierungsbasis aus der Winkelconfidence abgeleitet werden.

`sin(2e)` und `cos(2e)` bilden den 180°-periodischen Winkel allgemein ab. Die dekodierte Korrektur lautet `0.5 * atan2(sin_2e, cos_2e)` und wird mit derselben Regel nach `[-90°, +90°)` normalisiert, die `backend/packages/cv-datasets/src/cv_datasets/target_builders/stem_orientation.py` — `wrap_to_half_turn` für das Label verwendet. Der Servingvertrag muss zusätzlich die Normierungs-, Deadband- und Confidence-Kalibrierungsregeln enthalten; ein fehlendes oder zu kleines Winkelvektormaß führt fail-closed zu keinem Winkel.

Human Labels und abgeleitete Ziele bleiben getrennt. Ungültige Winkelziele maskieren ausschließlich Winkelverlust und Winkelmetriken; `bow_relevance`, `target_visible` und die optionale Endqualität können weiter trainiert werden. Diese Semantik folgt `CanonicalSequenceItem.human_labels`, `CanonicalSequenceItem.derived_targets` und `build_stem_orientation_targets`.

### Runtime-Ausgabe für Stammausrichtung

Der allgemeine Temporal-Inferenzvertrag liefert Wahrscheinlichkeiten, Winkelvektor, Confidence, Route und Modellversion. Ein fachlicher Adapter erzeugt daraus ein Vor-Einzug-Ergebnis mit:

- `decision_ready` und dem tatsächlich verwendeten `decision_frame`;
- `recommended_rotation_deg_signed`, Absolutwert und Richtung;
- `bow_relevant`, `target_observed`, Confidence und Reason Codes;
- `estimate_method` (`temporal_tcn`, `temporal_gru` oder `projected_baseline`);
- Feature-/Scaler-/Confidence-/Modell-/Snapshot-/Kalibrierungs-/Blickprovenienz;
- der gleichzeitig berechneten deterministischen Baseline als Vergleich, nicht als stillschweigend überschriebenes Feld.

Die Struktur baut auf dem realen Resultat aus `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_orientation_result` auf. `post_rotation_verification` bleibt ein separates sekundäres Feld beziehungsweise Resultat. Nach dem Decision Frame beobachtete Werte dürfen weder den eingefrorenen Vor-Einzug-Winkel ändern noch in `input__sequence_features` gelangen.

Ein Winkel ist nur gültig, wenn Modellvertrag, Feature-Digest, Winkel-/Blickkonvention, Kalibrierung, Mindestfenster, Target-Observed-Gate und Confidence-Gate übereinstimmen und ein optional vorhandener OOD-Indikator nicht sperrt. Andernfalls sind Winkel und Richtung `null`/`manual_required`; 0° darf nicht als Ersatzwert dienen. Die deterministische Runtime verwendet diese Fail-closed-Semantik bereits in `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `_temporal_result`.

## Modellbaselines

### Kleine kausale TCN als erste Baseline

Die erste lernende Baseline ist eine kleine kausale TCN auf Featuresequenzen. Die Faltung ist links gepaddet, Ausgaben werden nur am letzten gültigen Präfixschritt gelesen, und Dilatation, Kanalzahl, Kernelgröße und Kontextlänge werden ausschließlich über eine versionierte Recipe festgelegt.

Begründung:

- Die Daten liegen bereits als begrenzte, geordnete Featurefolge in `CanonicalSequenceItem.frames`, `feature_manifest` und `rotation_trace` vor.
- Ein TCN hat keinen versteckten Runtime-State außerhalb des übergebenen Fensters; Neustart und Trackwechsel werden durch das neue Fenster eindeutig.
- Die vorhandene Paketierung kann ein statisches ONNX-Tensorschema über `ModelIoSchema` und `render_config_pbtxt` transportieren.
- Kausale 1D-Faltungen sind gegenüber einem recurrenten Export der risikoärmere erste Kandidat für den verpflichtenden TensorRT-Build; die tatsächliche Kompatibilität bleibt dennoch ein zu prüfendes Gate.

### GRU als Vergleich

Eine kleine GRU wird auf **demselben Snapshot, denselben Gruppen-Splits, derselben Featureordnung, demselben Scaler und denselben Head-/Maskenregeln** trainiert. In Phase 1 erhält auch sie das vollständige begrenzte Fenster und exportiert keinen serverseitig versteckten Hidden State. Dadurch bleiben Requests reproduzierbar und Trackwechsel benötigen keine neue Gateway-Sessionsemantik.

Ein späterer expliziter GRU-Streaming-State wäre eine neue Vertragsversion und ist nicht Teil von Phase 1. Ob der mit dem vorhandenen Produktions-TensorRT kompatible Export gelingt, ist offen; ein nicht paketierbarer GRU-Lauf bleibt ein Offlinevergleich und wird nicht über `supported_model_capability` als end-to-end unterstützt markiert.

### Verpflichtende nichtlernende Vergleiche

Jeder Trainings-/Evaluationsreport enthält mindestens:

1. die deterministische Projektionsbaseline aus `backend/packages/cv-runtime/src/cv_runtime/stem_temporal.py` — `evaluate_projected_pre_entry_baseline`;
2. eine Decision-Frame-Baseline ohne zeitliches Modell;
3. die TCN;
4. die GRU, sofern der Lauf fachlich und technisch gültig ist.

Alle Vergleiche verwenden identische Sequenz-IDs und `group_id`-Splits aus `CanonicalSequenceDataset`. Ein besserer Silver-Winkelwert allein ersetzt keine Gold-Evaluation.

### Bewusst zurückgestellte Bildmodelle

Ein kleiner 2D-Frame-Encoder wird erst geprüft, wenn die Fehler-/Sliceanalyse auf denselben Splits ein Informationsdefizit der gespeicherten Features belegt. Sein Embedding würde als neues, versioniertes optionales Feature in denselben Temporalvertrag aufgenommen. Ein Rohvideo-Transformer ist in Phase 1 ausgeschlossen; er würde die aktuelle Feature-, Speicher-, Gateway- und Latenzgrenze ohne belegten Nutzen vergrößern.

## Training und Evaluation

### Multi-Task-Training

Die allgemeine Recipe beschreibt Heads und Zielnamen. Für `stem_orientation_angle.v1` gilt:

- binärer Head für Bow-Relevance;
- binärer Head für beobachtbare Zielstellung;
- periodischer Regressionshead für `sin_2e` und `cos_2e`, nur wenn `angle_target_valid=true`;
- Confidence-/Unsicherheitshead mit nachgelagerter Kalibrierung ausschließlich auf dem Validation-Split;
- optionaler Drei-Klassen-Head für `final_orientation_quality`.

Lossgewichte, Deadband, Confidence-Zieltoleranz und Kalibrierungsverfahren werden in der Recipe versioniert. Ihre numerischen Werte sind offen und werden nicht in dieser ADR erfunden. Scaler, Schwellen und Confidence-Kalibrierung dürfen nur aus dem Train- beziehungsweise dafür vorgesehenen Validation-Split entstehen; `val` und `test` dürfen nicht in den Feature-Scaler einfließen. Diese Trennung wird zusätzlich zu `validate_temporal_sequence_dataset` im Trainer-Staging geprüft.

### Kausalitätsprüfungen

Der Trainer muss folgende Invarianten als Artefaktvalidierung ausweisen:

- kein geladener Frameindex größer als `CanonicalSequenceItem.decision_frame_index`;
- keine nichtkausale Faltung oder bidirektionale Rekurrenz;
- Präfixaugmentation nur mit neu abgeleitetem Ziel zum neuen Endframe;
- alle Varianten einer `group_id` im selben Split;
- ein nach dem betrachteten Präfix veränderter Zukunftsteil ändert dessen Output nicht;
- Scaler- und Schwellenfit ohne Val-/Test-Leakage.

Das Ergebnis wird in `TrainerArtifactValidation.checks` aus `backend/packages/cv-training/src/cv_training/trainer_types.py` aufgenommen und durch `_validate_deployable_artifacts` in `backend/workers/training-worker/src/training_worker/runtime.py` vor der Modellregistrierung verlangt.

### Metriken und Gold-/Silver-Trennung

`backend/packages/cv-training/src/cv_training/catalog.py` — `metric_definitions_for_task`, `default_best_checkpoint_rule` und `default_promotion_criteria` werden für `TaskType.TEMPORAL_SEQUENCE` explizit erweitert; der aktuelle Classification-Fallback darf nicht verwendet werden.

Der Evaluationsreport enthält getrennte Namespaces `gold/*`, `silver/*` und `all_valid/*` für:

- Mean, Median, P90 und P95 des modulo-180-Winkelfehlers;
- Anteile innerhalb ±5°, ±10°, ±15° und ±20°;
- Richtungsgenauigkeit oberhalb des versionierten Deadbands;
- Gegenrichtungsrate und Fehler über 30°;
- No-Rotation-Fehlerrate bei `nearly_straight`;
- Confidence-Coverage, Reliability/Calibration Error und Fehler über Coverage;
- Bow-Relevance- und Target-Observed-Metriken;
- Slices nach Sichtbarkeit, Bogenstärke, Tracequalität, Stammgröße und Sequenzlänge.

Die Separation verwendet `SequenceLabelQuality.tier` aus `backend/packages/cv-datasets/src/cv_datasets/models.py`. Silver ist für Training und Shadow-Vergleich zulässig. Eine Produktionsfreigabe des Winkeloutputs benötigt einen unabhängigen Gold-Testbestand; die noch festzulegende Mindestanzahl und die numerischen Grenzwerte sind als offene Produkt-/Abnahmeentscheidung markiert.

## Modellartefakt und Servingvertrag

Zusätzlich zu Checkpoint und `model.onnx` enthält jede Temporal-Modellversion checksum-gebunden:

| Artefakt | Verbindlicher Inhalt |
| --- | --- |
| `temporal-contract.json` | Vertragsversion, Task, Feature-Schema/Digest/Reihenfolge, Tensorformen, Kausalitätsflag, Mindest-/Maximalfenster, Padding, Abtastregeln, Headnamen und Decision-Deadline-Semantik |
| `scaler.json` oder äquivalentes begrenztes Binärartefakt | train-only Lage-/Skalenwerte je Feature, Missing-Value-Regel, Fit-Split-Digest und Scaler-Version |
| `angle-contract.json` | Periodizität 180°, `sin(2e)`/`cos(2e)`, `[-90°, +90°)`-Wrap, Deadband, Positivrichtung und erforderliche Blickkonvention |
| `confidence-calibration.json` | Methode, Fit-Split, Parameter, Gültigkeitsbereich, Zieldefinition und Version |
| `evaluation-summary.json` | getrennte Gold-/Silver-/Gesamtmetriken, Baselinevergleich, Slices, Kausalitäts- und Deadlinebericht |
| `export_manifest.json` | benannte Ein-/Ausgabetensoren, alle Artefakthashes, Snapshot/Builder/Recipe/Code-Version und Hardwarevertrag |

Diese Dateien werden über den vorhandenen `build_training_artifact_manifest_payload`, DVC-Bundle und MLflow-Registrierung in `backend/workers/training-worker/src/training_worker/runtime.py` — `_register_successful_training` aufgenommen. `ModelVersion.artifact_manifest`, `export_manifest`, `immutable_metadata` und die zugehörigen Digests in `backend/packages/cv-db/src/cv_db/models.py` speichern die Referenzen; große Sequenzen oder Scalerarrays werden nicht inline in die Datenbank kopiert.

## Einbindung in den vorhandenen Lebenszyklus

### Orchestrator und Worker

Es entsteht keine neue Trainings-API. Die beiden bereits vorhandenen Eintrittspunkte `backend/services/admin-api/src/admin_api/api/routes/training.py` und `backend/services/training-orchestrator/src/training_orchestrator/api/routes.py` — jeweils `request_training_job` — verwenden weiterhin gemeinsam `backend/packages/cv-db/src/cv_db/training_store.py` — `create_training_job_request` und stellen `training_worker.jobs.run_training_job` ein. `TrainingJobCreateRequest` erhält nur allgemein wiederverwendbare Temporal-Trainingseinstellungen beziehungsweise referenziert eine Temporal-Recipe; die persistierbaren Werte landen wie bisher in `TrainingJob.config_snapshot` und `TrainingJob.resolved_training_defaults`.

Der Worker behält `run_training_workflow`, GPU-Reservierung, Resume, Checkpointing, MLflow, DVC und Registrierung. Nur die folgenden Dispatches werden ergänzt:

- Temporal-Snapshot-Staging statt `load_snapshot_training_source`/Bildfallback;
- Temporal-Recipe in `_recipe_task_dir` und `_expected_recipe_path`;
- TCN-/GRU-Auswahl in `resolve_trainer_architecture` und `get_default_trainer`;
- Temporal-Preflight für das bereits im Root-`pyproject.toml` unter `dependency-groups.training-runtime` vorhandene PyTorch;
- taskabhängige, nicht bildgebundene Erstellung von IO-, Pre-/Postprocessing- und Artefaktmanifesten in `_register_successful_training`.

### Modellkatalog und Versionierung

`SUPPORTED_ARCHITECTURE_MATRIX` erhält allgemeine, konkret implementierte Einträge für eine kleine kausale TCN und eine kleine GRU. `supported_model_architectures` darf `supported=True` erst liefern, wenn reales Training, Checkpoint, ONNX-Export, Paketierung und Inferenz für die jeweilige Architektur getestet sind. Bis dahin lautet der Capability-Status `experimental` oder der Eintrag bleibt verborgen; dies folgt der End-to-end-Prüfung, die `supported_model_architectures` heute bereits in seinem `support_reason` behauptet.

`ModelFamily.input_schema_ref` und `output_schema_ref` referenzieren allgemeine Temporal-Schemas. `ModelVersion` behält Snapshot-, Code-, DVC-, MLflow-, Manifest- und Aliasprovenienz. Die vorhandenen String- und JSON-Felder in `TrainingJob`, `ModelFamily` und `ModelVersion` reichen für diesen Plan aus; **eine Alembic-Migration ist nach aktueller Codeprüfung nicht erforderlich**. Falls die spätere Implementierung relationale Abfragen auf einzelne Temporal-Vertragsfelder verlangt, ist das ein neuer, gesondert zu begründender Migrationsentscheid und nicht Teil dieser ADR.

### Export, Paketierung und Release

`ModelIoSchema` bleibt der gemeinsame Tensorvertrag. Für Temporalmodelle wird ein eigenes, rückwärtskompatibles Manifestmodell beziehungsweise ein taskabhängiger Builder ergänzt, statt Bildfelder mit erfundenen Werten zu füllen. Die existierenden Bildmanifeste bleiben semantisch unverändert.

`build_deployment_package` kopiert auch Temporalvertrag, Scaler, Winkelvertrag und Confidence-Kalibrierung, berechnet deren Hashes und erzeugt wie bisher Realtime-/Batchvarianten. `_deployment_io_schema` verwendet bei `temporal_sequence` die ONNX- und Vertragsdimensionen und niemals `class_count` als Fallback. `render_config_pbtxt` und `TritonRepositoryManager.stage_release` bleiben der reale Repositorypfad.

Weil `patch_release_config` den ONNX-Fallback in Produktion deaktiviert, sind für jede Architektur verpflichtend:

1. ONNX-Exportvalidierung;
2. TensorRT-Build auf der tatsächlich gebündelten Toolchain;
3. ONNX-vs.-TensorRT-Parität auf einer checksum-gebundenen Smoke-Sequenz;
4. Triton-Testinferenz mit Form-, Finite- und Wertebereichsprüfung;
5. Deadline-/Latenzbericht auf Zielhardware.

`TritonRepositoryManager.smoke_test_report` wird dafür allgemein um manifestdefinierte Testtensoren erweitert. Ein fehlgeschlagener TCN-/GRU-Export oder Smoke-Test blockiert nur diese Modellversion; er rechtfertigt kein Laden im Stamm-Node.

### Promotion

Die bestehenden Staging-/Production-Symbole bleiben erhalten. Für Temporalmodelle wird `_deployment_policy_errors` scopebewusst erweitert:

- **Staging/Shadow:** gültiger Snapshot, Kausalitätschecks, ONNX/TensorRT/Triton-Smoke, Featurevertrag und konfigurierte Silver-/all-valid-Mindestmetriken;
- **Production:** zusätzlich unabhängige Gold-Abdeckung, Gold-Grenzwerte, Confidence-Kalibrierung und Zielhardware-Deadline.

Die unverhandelbaren Invarianten `causal`, keine Post-Decision-Frames, gültige Richtungskonvention und Gold-Nachweis für den produktiven Winkel dürfen nicht durch `allow_quality_gate_bypass` aufgehoben werden. Bis die offenen Gold-Grenzen fachlich festgelegt sind, bleibt Production für den Winkelkopf gesperrt; ein gestagter Release kann über `RuntimeLogicalModelRef.selection_policy="staged_test"` im Shadow Mode ausgewertet werden.

`backend/workers/control-worker/src/control_worker/jobs/orchestration.py` — `run_post_training_automation` darf ein temporales Modell nur dann automatisch in Production überführen, wenn dieselben scopeabhängigen Gates erfüllt sind. Andernfalls endet der Job nachvollziehbar bei Paketierung oder Staging und verändert den aktiven Release nicht.

## Inference Gateway und Runtime

### Gateway-Erweiterung

Der vorhandene Router `backend/services/inference-gateway/src/inference_gateway/api/realtime.py` wird um einen typisierten `temporal-sequence`-Endpoint ergänzt; es entsteht kein neuer Service. Der Vertrag in `backend/packages/cv-contracts/src/cv_contracts/inference.py` enthält:

- Deploymentziel und optionale Release-ID wie `BaseInferenceRequest`, aber keine `image_base64`;
- Feature-Schema-ID/Digest, geordneten begrenzten Featuretensor, Frameanzahl, letzten Frameindex und Zeitstempel;
- Decision-/Deadline-Metadaten und Versions-/Richtungskonvention;
- eine Response mit benannten Heads, dekodiertem Winkelkandidaten, Confidence, Route, Timing und Reason Codes.

`TritonInferenceBackend` erhält einen numerischen Tensorpfad, der die im Servingmanifest deklarierte Form, Reihenfolge, Finite-Werte und Fenstergrenzen prüft und dann denselben Triton-/ONNX-Release verwendet. Er darf keine Feature-Reihenfolge erraten. `RealtimeGatewayService` prüft `route.task_type == TaskType.TEMPORAL_SEQUENCE`; `RealtimeGatewayService.predict` darf nicht länger unbekannte Tasks in Detection fallen lassen.

### Runtime-Node und Binding

Geplant ist ein allgemeiner `inference.temporal_sequence`-Node nach der bestehenden `RuntimeNodeHandler`-/Registry-Konvention in:

- `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — neue allgemeine, typisierte Featuresequenz- und Prediction-Payloads sowie Aufnahme in `AnyRuntimePayload`;
- `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py` oder ein fachlich allgemeines Temporal-Inferenzmodul — neuer Handler, der ausschließlich `RuntimeExecutionContext.resolve_model` und `RuntimeInferenceClient` verwendet;
- `backend/packages/cv-runtime/src/cv_runtime/registry.py` — Node-Spezifikation mit `required_task_type=temporal_sequence` und normalem `model_ref`;
- `backend/packages/cv-runtime/src/cv_runtime/inference_client.py` — Methode des bestehenden Clients, kein direkter ONNX-Loader.

Der Node ist stateless: Er bekommt das vollständige begrenzte Präfix. Die bestehende Zustandshaltung und Frameordnung verbleiben in `analysis.stem_temporal_orientation`, der bereits in `realtime_executor.live._STATEFUL_PIPELINE_NODE_TYPES` steht. Dadurch benötigt der allgemeine Inferenznode keinen serverseitigen Track-/GRU-State und keinen neuen Eintrag in dieser Liste.

Für die Stamm-Pipeline wird `StemTemporalOrientationHandler` rückwärtskompatibel um einen optionalen Featuresequenz-Ausgang ergänzt. Ein nachgeschalteter fachlicher Adapter verbindet den allgemeinen Prediction-Payload mit dem bestehenden `stem.orientation.v2`-Ergebnis. Bestehende Pipelines ohne diese Kante behalten ausschließlich die deterministische Baseline. Das Modellresultat ist zunächst eine zusätzliche Shadow-Auswertung; die Hauptempfehlung wird erst nach einer explizit versionierten Auswahlpolicy umgeschaltet.

### Decision Deadline und Fehlerverhalten

`backend/services/realtime-executor/src/realtime_executor/live.py` — `_pipeline_work_deadline_seconds`, `_pipeline_max_inflight` und die in `RuntimeExecutionContext.metadata` gesetzte `default_inference_timeout_ms` bleiben die operativen Zeitgrenzen. Der temporale Request wird auf jedem konfigurierten gültigen Präfix oder mindestens auf dem letzten gültigen Präfix vor der Decision Line ausgeführt. Am Decision Frame wird das letzte rechtzeitig abgeschlossene gültige Ergebnis eingefroren.

Timeout, Schemaabweichung, Trackwechsel, Runtime-Neustart, ungültige Rotationstrace, Konventionsabweichung, zu kurzes Fenster, geringe Target-Observed-Wahrscheinlichkeit oder geringe Winkelconfidence erzeugen einen typisierten Reason Code und keine Gradzahl. Die deterministische Baseline bleibt sichtbar; ein Fehler des Lernmodells darf die bestehende Pipeline nicht zum Spezialloader umleiten.

## Konkreter Dateiänderungsplan für eine Folgeimplementierung

Alle in der Spalte „geplante Symbole“ genannten Symbole sind **neu geplant** und existieren noch nicht.

| Schritt | Reale Datei(en) und vorhandene Symbole | Geplante Änderung |
| --- | --- | --- |
| 1. Allgemeine Trainingsverträge | `backend/packages/cv-contracts/src/cv_contracts/modeling.py` — `TrainingDefaults`; `backend/packages/cv-contracts/src/cv_contracts/training.py` — `TrainingJobCreateRequest`; `backend/packages/cv-training/src/cv_training/trainer_types.py` — `TrainerConfig`, `TrainerDatasetArtifact` | Rückwärtskompatible optionale Temporal-Einstellungen; Bildfelder für den Temporalpfad nicht mit Dummywerten füllen; allgemeine Manifestreferenzen im Dataset-Artefakt. |
| 2. Temporal-Staging | `backend/packages/cv-training/src/cv_training/dataset_staging.py` — `load_snapshot_training_source`, `stage_framework_dataset` | Neuer geplanter `load_temporal_snapshot_training_source`/`stage_temporal_sequence_dataset`; lädt `CanonicalSequenceDataset`, train-only Scalerbasis, Headmasken und referenzierte Features, ohne Videos zu dekodieren. |
| 3. Recipes | `backend/packages/cv-training/src/cv_training/recipes.py` — `_recipe_task_dir`; `backend/workers/training-worker/src/training_worker/runtime.py` — `_expected_recipe_path` | Mapping `temporal_sequence`; identische TCN-/GRU-Recipe-Schemas unter `ml/configs/task-types/temporal_sequence` und `backend/packages/cv-training/src/cv_training/resources/task-types/temporal_sequence`. |
| 4. Trainer | `backend/packages/cv-training/src/cv_training/trainer.py` — `resolve_trainer_architecture`, `get_default_trainer`; `backend/packages/cv-training/src/cv_training/trainer_types.py` — `BaseTrainer` | Geplanter allgemeiner `TemporalSequenceTrainer` mit Recipe-Auswahl TCN/GRU, Multi-Task-Masken, Checkpoint/Resume, Evaluation und ONNX-Export. Kein Stamm-Task im Dispatch. |
| 5. Katalog und UI-Auswahl | `backend/packages/cv-db/src/cv_db/model_catalog.py` — `SUPPORTED_ARCHITECTURE_MATRIX`, `DEFAULT_TEMPLATE_DEFINITIONS`; `frontend/apps/admin-web/src/features/training/training-page.tsx` — `defaultForm`, `recipeForArchitecture`, `supportedTaskTypes` | TCN/GRU erst bei end-to-end belegter Capability anbieten; Temporal-Dataset und Recipe über die bestehende Trainingsseite auswählbar machen. |
| 6. Worker-Manifeste/Evaluation | `backend/workers/training-worker/src/training_worker/runtime.py` — `_build_trainer_config`, `_build_evaluation_summary`, `_build_io_schema`, `_register_successful_training` | Taskabhängiger Temporalvertrag, Gold-/Silver-Metriken, Baselinevergleich, Scaler-/Winkel-/Confidence-Artefakte und Kausalitätschecks; Bildpfad unverändert lassen. |
| 7. Modellregister/Promotion | `backend/packages/cv-db/src/cv_db/training_store.py` — `create_training_job_request`, `register_trained_model_version`; `backend/packages/cv-db/src/cv_db/registry_store.py` — `_deployment_policy_errors`, `record_deployment_approval`, `create_promotion_request` | Bestehende Versionierung verwenden; scopeabhängige Temporal-Gates ergänzen. Nach aktuellem Stand keine Migration. |
| 8. Export/Paket | `backend/packages/cv-deploy/src/cv_deploy/manifests.py` — `ExportManifest`, `DeploymentPackageManifest`, `ModelIoSchema`; `backend/packages/cv-deploy/src/cv_deploy/packaging.py` — `build_export_manifest_payload`, `build_deployment_package`, `_deployment_io_schema` | Rückwärtskompatibler Temporal-Manifesttyp/-Builder; zusätzliche Vertrag-/Scalerdateien; keine Klassenkarten-Fallbacks für Temporaloutputs. |
| 9. Triton/Release | `backend/packages/cv-deploy/src/cv_deploy/triton.py` — `render_config_pbtxt`, `TritonRepositoryManager.smoke_test_report`; `backend/workers/control-worker/src/control_worker/jobs/deployment.py` — `build_deployment_package`, `stage_model_release` | Vorhandenes Releaseverfahren behalten; numerische Smoke-Sequenz, ONNX/TensorRT-Parität und Deadlinebericht ergänzen. |
| 10. Inferenzvertrag/Gateway | `backend/packages/cv-contracts/src/cv_contracts/inference.py`; `backend/services/inference-gateway/src/inference_gateway/api/realtime.py` — `router`; `backend/services/inference-gateway/src/inference_gateway/runtime/service.py` — `TritonInferenceBackend`, `RealtimeGatewayService` | Geplante `RealtimeTemporalSequenceRequest`/`TemporalSequenceInferenceResponse`, Endpoint und numerischer Tensorpfad im bestehenden Gateway. |
| 11. Runtime-Client/Node | `backend/packages/cv-runtime/src/cv_runtime/inference_client.py` — `RuntimeInferenceClient`, `HttpInferenceGatewayClient`; `backend/packages/cv-runtime/src/cv_runtime/payloads.py` — `AnyRuntimePayload`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `BUILTIN_NODE_TYPES` | Geplante Clientmethode, allgemeine Payloads und `inference.temporal_sequence` mit normaler logischer Modellbindung. |
| 12. Stammadapter | `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` — `StemTemporalOrientationHandler`, `_orientation_result`, `_temporal_result`; `backend/packages/cv-runtime/src/cv_runtime/registry.py` — `analysis.stem_temporal_orientation` | Optionaler Featuresequenz-Ausgang und nachgeschalteter Ergebnisadapter; Baseline, Pre-Entry-Freeze und getrennte Nachkontrolle bleiben erhalten. |
| 13. Generierte Schemas | `frontend/packages/schemas/src/index.ts` — `TaskType` und generierte Inferenz-/Trainingstypen; `frontend/packages/api-client/src/index.ts` — bestehende Training-/Inference-Clients | Nach OpenAPI-Konvention neu generieren; keine handgeschriebene zweite Client-API. |

## Test- und Abnahmeplan der Folgeimplementierung

Die vorhandenen Tests werden erweitert, nicht durch eine separate Testsuite ersetzt:

- `tests/unit/backend/test_temporal_snapshot_materialization.py`: Snapshot-zu-Trainer-Projektion, Headmasken, Gold/Silver und keine Post-Decision-Frames;
- `tests/unit/backend/test_temporal_sequence_datasets.py`: identische Gruppensplits und train-only Scaler;
- neuer fokussierter Temporal-Trainer-Test neben `tests/unit/backend/test_torchvision_training_backends.py`: TCN und GRU auf demselben Mini-Snapshot, Checkpoint/Resume, ONNX-Export und Outputnamen;
- `tests/unit/backend/test_model_capability_matrix.py`: Capability erst nach vollständigem End-to-end-Vertrag;
- `tests/unit/backend/test_training_orchestration.py` und `test_training_worker_runtime_source.py`: unveränderter Orchestrator-/Workerpfad ohne Bildfallback;
- `tests/unit/backend/test_deployment_packaging.py`: Temporalmanifest, Scaler-/Vertragshashes, statische Tensorformen und unveränderte Bildpakete;
- `tests/unit/backend/test_inference_gateway.py`: Task-Dispatch, Schema-/Finite-/Fensterprüfung, ONNX/Triton-Outputparsing und Fail-closed-Fehler;
- `tests/unit/backend/test_runtime_inference_client.py`: neuer Endpoint im bestehenden Client;
- `tests/unit/backend/test_runtime_stem_temporal.py`: Featurefenster endet vor der Linie, Modellresultat wird eingefroren, Baseline bleibt erhalten, Nachkontrolle überschreibt nicht;
- `tests/unit/backend/test_model_registry_lifecycle.py` und `test_project_pipeline_deploy.py`: Staging/Shadow, Gold-Produktionsgate, Rollback und kein Quality-Gate-Bypass der Kausalitätsinvarianten.

Zusätzliche modellfachliche Akzeptanztests:

1. Eine Änderung beliebiger Frames nach einem ausgewerteten Präfix ändert dessen Output nicht.
2. TCN, GRU und Baselines sehen exakt dieselben Sequence-/Group-Splits.
3. Reproduktion des abgeleiteten Winkels aus `sin_2e`/`cos_2e` ist nahe ±90° sprungfrei.
4. Ein ungültiges Winkelziel beeinflusst die anderen Heads, aber nicht den Winkelverlust.
5. Gold und Silver werden in Training, Evaluation und Promotion getrennt berichtet.
6. ONNX und TensorRT liefern innerhalb einer festgelegten numerischen Toleranz dieselben Heads.
7. Der letzte gültige Modelloutput liegt innerhalb der gemessenen Decision-Deadline; andernfalls lautet das Runtimeergebnis `manual_required`.
8. Bestehende Bildrecipes, Bildmanifeste, Bildnodes und Pipelines bleiben unverändert.

Auf diesem Entwicklungs-PC kann die Anwendung gemäß Threadvorgabe nicht ausgeführt werden. Die in dieser ADR geforderten Runtime-, Triton- und Zielhardwaretests sind deshalb spätere CI-/Builder-/Zielsystem-Gates und hier ausdrücklich nicht als bestanden behauptet.

## Migrations- und Einführungsfolge

1. **Vertragsphase:** allgemeine Temporal-Training-/Inferenceverträge, Snapshot-Staging und Katalogeinträge zunächst verborgen/experimentell.
2. **Offlinephase:** deterministische Baseline, TCN und GRU auf identischen immutable Snapshots; noch kein Runtime-Release.
3. **Exportphase:** ONNX, Temporalmanifest, Scaler, Confidence-Kalibrierung und Paketierung; Capability erst nach realer Artefaktvalidierung.
4. **Stagingphase:** TensorRT-/Triton-Smoke und Runtime-Shadow über `staged_test` oder expliziten Release. Die bestehende Hauptbaseline bleibt aktiv.
5. **Gold-Gate:** unabhängige Gold-Evaluation und fachlich festgelegte Grenzwerte. Erst danach kann der Temporalwinkel als produktionsfähiges Modellresultat ausgewählt werden; SPS-Schreiben bleibt weiterhin separat gesperrt.
6. **Optionale Erweiterung:** kleiner 2D-Frame-Encoder nur bei belegtem Informationsdefizit. Kein Rohvideo-Transformer in Phase 1.

Rückwärtskompatibilität entsteht durch neue Task-/Manifest-/Node-Versionen und Opt-in-Pipelinekanten. Bestehende Bildmodellversionen behalten ihre bisherigen `ExportManifest`-Felder und Handler. Ein Rollback verwendet weiterhin `backend/packages/cv-db/src/cv_db/registry_store.py` — `rollback_target_to_previous_release` und `backend/workers/control-worker/src/control_worker/jobs/deployment.py` — `rollback_model_release`; Dataset-Snapshots und Modellartefakte bleiben immutable.

## Offene Punkte

Die folgenden Punkte sind bewusst offen und dürfen in der Implementierung nicht geraten werden:

1. **Exakte Featureordnung und Dimension `F`:** aus den real aufgezeichneten Featuremanifesten ableiten, als Schema einfrieren und hashen.
2. **Fensterlänge `N`, Mindestfenster und Sampling:** anhand Sequenzdauer, Decision-Deadline und Zielhardwaremessung festlegen.
3. **TCN-/GRU-Hyperparameter und Lossgewichte:** über versionierte Recipes und identische Vergleichsläufe bestimmen.
4. **Confidence-Modell und Kalibrierer:** Zieldefinition, Toleranz und Verfahren anhand Validation-Daten festlegen und versionieren.
5. **Gold-Mindestumfang und Produktionsgrenzen:** fachliche Abnahmeentscheidung; bis dahin keine Production-Freigabe des Winkelkopfs.
6. **TensorRT-Kompatibilität:** TCN und insbesondere GRU mit der tatsächlich ausgelieferten Torch-/ONNX-/TensorRT-Version bauen und messen. Ein Offlineerfolg ist kein Deploymentnachweis.
7. **Latenzbudget des Temporalaufrufs:** auf Zielhardware innerhalb der gesamten serialisierten Pipeline messen; keine Zahl wird auf dem Entwicklungs-PC behauptet.
8. **Arbitration Baseline gegen Modell:** versionierte Shadow-/Auswahlpolicy festlegen; niemals ein bestehendes Hauptresultat still überschreiben.
9. **Nutzen eines 2D-Encoders:** nur durch vorab definierte Slice-/Ablationsanalyse belegen.
10. **Unabhängige Gold-Referenzquelle:** bekannte Winkelmarkierung oder unabhängige Messung organisatorisch und technisch festlegen.

## Folgen der Entscheidung

Die Folgeimplementierung ist größer als ein einzelner Trainer, bleibt aber auf die vorhandenen Erweiterungspunkte begrenzt. Sie schafft einen allgemeinen Temporalpfad mit derselben Auditierbarkeit, Versionierung, Promotion und Rollbackfähigkeit wie die vorhandenen Bildmodelle. Die Stammlogik bleibt fachlich gekapselt; Modellladung, GPU-Inferenz und Releaseauswahl bleiben im bestehenden Plattformpfad. Bis alle offenen Gates geschlossen sind, bleibt die deterministische Vor-Einzug-Baseline die nachvollziehbare Referenz und jedes lernende Ergebnis Shadow Mode.
