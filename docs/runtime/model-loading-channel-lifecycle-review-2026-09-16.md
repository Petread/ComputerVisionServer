# Modellladen und Kanal-Lebenszyklus – Prüfung vom 16.09.2026

Nachtrag: Die anschließend beauftragte Umsetzung ist in [Implementierung und Verifikation](model-loading-channel-lifecycle-implementation-2026-09-16.md) beschrieben. Die folgenden Befunde dokumentieren den Stand vor dieser Änderung.

Die Vermutung ist für den regulären Triton-Pfad bestätigt: Modelle werden bereits während des Deployments geladen, genauer beim Staging vor dem Smoke-Test. Ein gestarteter Kanal ist dafür derzeit keine Voraussetzung. Kanalstopp und Modellentladen sind nicht gekoppelt.

Grundlage ist der lokale Arbeitsstand einschließlich vorhandener Änderungen. Es erfolgte eine statische Prüfung der beteiligten Implementierungen und bestehender Tests; keine Ausführung der Anwendung, keine Serverprüfung und keine Änderung am Produktionscode.

## Befund

| Vorgang | Aktuelles Verhalten | Beleg |
| --- | --- | --- |
| Deployment/Staging | `stage_model_release` schreibt das Repository, setzt `LOADING`, ruft `load_release` auf und prüft anschließend den Smoke-Test. Kein Kanalbezug. | `backend/workers/control-worker/src/control_worker/jobs/deployment.py`, `stage_model_release` |
| Manuelles Aktivieren | Lädt das Modell und prüft Triton-Readiness unabhängig von Kanälen. | dieselbe Datei, `activate_deployment_model` |
| Rollback | Lädt das vorherige Release, abgesehen vom expliziten lokalen ONNX-Fallback. | dieselbe Datei, `rollback_model_release` |
| Controller-Neustart | Lädt ausgewählte aktivierte Releases: neuestes Release pro Modell sowie aktive/vorherige Zielbindungen. Keine Prüfung laufender Kanäle. | `backend/services/deployment-controller/src/deployment_controller/lifecycle.py`, `reload_active_releases` |
| Regelmäßiger Abgleich | Prüft alle 60 Sekunden auf Abweichungen. Fehlende aktive Ziel-Releases oder noch geladene deaktivierte Modelle lösen den Abgleich aus. | dieselbe Datei, `_active_release_reconcile_loop`, `_model_serving_state_drift` |
| Kanalstart | Validiert Pipeline und Modellreferenzen, erstellt die Runtime-Instanz und startet den Executor. Kein eigener Triton-Ladeschritt vor `RUNNING`. | `backend/services/runtime-orchestrator/src/runtime_orchestrator/supervisor.py`, `_start`; `backend/packages/cv-db/src/cv_db/runtime_execution_store.py`, `validate_runtime_channel_start_requirements` |
| Kanalstopp | Stoppt die Runtime-Ausführung und aktualisiert Kanal-/Ausführungszustände. Keine Freigabe der geladenen Modelle. | dieselben Dateien, `_stop`, `stop_runtime_execution` |
| Manuelles Deaktivieren | Entlädt den Triton-Modellnamen; der Controller kann deaktivierte Modelle ebenfalls entladen. | `deployment.py`, `deactivate_deployment_model`; `lifecycle.py`, `_unload_deactivated_models` |

Der WSL-Installer startet Triton bereits mit `--model-control-mode=explicit` (`deployment/wsl/apply-release.sh`). Die entscheidenden Ladeaufrufe kommen daher aus den bestehenden Diensten; ein Wechsel des Triton-Betriebsmodus ist für das gewünschte Verhalten nicht erforderlich.

Zusätzlich lädt Triton derzeit nach Modellname: `TritonRepositoryManager.load_release` verwendet die übergebene Versionsnummer nicht für den Ladeaufruf. Die erzeugte Konfiguration enthält `version_policy { all { } }`. Für eine präzise Speichersteuerung müssen deshalb auch mehrere vorhandene Versionen derselben Modellfamilie berücksichtigt werden.

## Trigger und Abruf

Ein Kanal ist an einen Trigger-Endpunkt gebunden, muss aber aktuell nicht gestartet sein. `trigger_endpoint_readiness` und `_source_frame_readiness` in `backend/packages/cv-db/src/cv_db/trigger_endpoint_store.py` geben unter anderem folgende Fälle anhand der Konfiguration frei:

- eingebettetes Testbild in der Pipeline;
- verwaltete Testdatei;
- Snapshot-/HTTP-Bildquelle;
- konfigurierte Kamera für Bilderfassung auf Abruf.

Eine laufende Runtime-Instanz ist dabei keine durchgängige Bedingung. `execute_trigger` prüft diese Readiness vor dem Aufruf, wodurch die genannten Freigaben auch tatsächlich wirksam sind.

Es gibt außerdem einen relevanten Konflikt beim Start: Ein Realtime-Kanal darf aktuell eine Trigger-Pipeline zugewiesen bekommen, sein Live-Start wird dann wegen unterschiedlicher Ausführungsmodi abgelehnt. Der bestehende Test `test_realtime_channel_can_bind_trigger_pipeline_without_live_start` in `tests/unit/backend/test_runtime_lifecycle_core.py` sichert genau dieses Verhalten ab. Eine zusätzliche Prüfung auf „Kanal läuft“ allein würde solche Konfigurationen unbenutzbar machen. Der Kanalstart muss für diesen Fall künftig die Trigger-Bereitschaft herstellen, ohne die Trigger-Pipeline kontinuierlich auszuführen.

Direkte Modellaufrufe existieren weiterhin im Inference-Gateway: öffentliche `/infer/*`-Routen und interne typisierte Inferenzrouten. Die gelesenen Aufruf- und Release-Auflösungspfade setzen keinen gestarteten Kanal voraus. Modelle ausschließlich über Kanal-Pipelines auszuführen ist deshalb eine zusätzliche Änderung am Ausführungszugang, nicht allein am Triton-Laden. Interne Gateway-Aufrufe der bestehenden Runtime müssen dabei erhalten bleiben.

Andere Backends benötigen dieselbe Lebenszyklusregel: Der Gateway erstellt lokale ONNX-Sessions und OpenVINO-Anomalie-Inferencer erst beim Inferenzbedarf und speichert sie in Prozess-Caches (`_onnx_sessions`, `_inferencers` in `inference_gateway/runtime/service.py`). Ein Triton-Unload gibt diese Ressourcen nicht frei.

## Vorgeschlagenes verbindliches Verhalten

| Zustand/Aktion | Gewünschtes Verhalten |
| --- | --- |
| Modell deployen | Artefakte und Verträge bereitstellen; noch keine dauerhafte Runtime-Aktivierung. |
| Kanal konfigurieren | Veröffentlichte Pipeline und Modellreferenzen zuordnen; keine Modelle laden. |
| Kanal starten | Benötigte Releases auflösen, Modellbedarf registrieren, laden und Betriebsbereitschaft prüfen. Erst anschließend Kanal freigeben. |
| Trigger-/Abruf-Kanal gestartet | Modelle bleiben bereit; die Pipeline läuft erst beim Trigger/Abruf. |
| Trigger/Abruf bei gestopptem oder noch startendem Kanal | Vor Bilderfassung und Inferenz ablehnen. Ein Trigger startet den Kanal nicht selbst. |
| Kanal stoppen | Neue Aufträge sperren, laufende Ausführungen beenden bzw. geordnet auslaufen lassen, anschließend Modellbedarf freigeben. |
| Mehrere Kanäle verwenden dasselbe Modell | Gemeinsam geladen halten; erst nach dem letzten Nutzer und dessen laufenden Aufträgen entladen. |
| Dienstneustart/Recovery | Bedarf aus gültigen Kanal-/Runtime-Bindungen rekonstruieren; nicht pauschal alle deployten Releases laden. |

„Deployt/verfügbar“ und „geladen/betriebsbereit“ müssen getrennte Aussagen bleiben. Ein deploytes, momentan ungenutztes Modell darf in der Pipeline weiterhin auswählbar sein.

## Umsetzung im bestehenden System

1. Die vorhandenen Runtime-Modellbindungen verwenden (`runtime_plan.binding_signature.model_bindings` sowie `build_runtime_model_resolver`); zusätzlich die freigegebenen Trigger-Pipelines des Kanals erfassen, die von der Live-Pipeline abweichen können. Keine zweite Modellverwaltung aufbauen.
2. Start-/Stopp-Ablauf im Runtime-Orchestrator um Modellbereitschaft und gemeinsam genutzten Modellbedarf ergänzen. Bedarf muss pro konkretem Release nachvollziehbar und bei parallelen Kanalstarts/-stopps konsistent sein. Die Freigabe muss laufende Trigger berücksichtigen, nicht nur den Live-Executor.
3. Deployment, manuelles Aktivieren, Rollback und Controller-Recovery auf denselben Bedarf umstellen. Die bestehende Smoke-Prüfung muss ausdrücklich eingeordnet werden: Bei der strikten Regel „kein Laden ohne Kanalstart“ ist der ausführende Lade-/Readiness-Test bis zum Kanalstart zu verschieben; beim Deployment bleiben Paket-/Kompatibilitätsprüfungen. Ein nur vorübergehendes Laden zum Deployment-Test wäre eine Ausnahme von dieser Regel.
4. Trigger-Readiness und tatsächliche Ausführung an den gestarteten, bereiten Kanal binden, einschließlich erneuter Prüfung nach Wartezeiten. Den oben beschriebenen Konflikt zwischen Kanalmodus und Trigger-Pipeline im Startablauf lösen.
5. Direkte Modell-Inferenzzugänge auf die Kanalregel umstellen; interne Pipeline-Inferenz weiterhin über das bestehende Gateway ausführen. Ein pauschales Sperren nur anhand „irgendein Kanal nutzt dieses Modell“ reicht für „Ausführung ausschließlich über Kanal-Pipeline“ nicht aus. Batch-, Label-Studio- und Preview-Aufrufstellen sind bei dieser Umstellung ebenfalls zu erfassen.
6. Triton-Versionenauswahl und lokale ONNX-/OpenVINO-Caches in dieselbe Ressourcenfreigabe einbeziehen. Deploymentstatus, Betriebsbereitschaft und gegebenenfalls UI-Verträge nach den bestehenden Konventionen trennen.

Nötige Regressionstests: Deployment ohne Kanalstart; Realtime-Start; Trigger-/Abruf-Bereitschaft ohne Dauerausführung; Ablehnung vor Start/nach Stopp; zwei Kanäle mit gemeinsamem Modell; verschiedene Versionen derselben Familie; laufender Trigger beim Stopp; fehlgeschlagener Modellstart; Recovery; lokale Backend-Freigabe; Verhinderung direkter Inferenz ohne Kanal-Pipeline. Live- und GPU-Speicherprüfung erfolgen auf dem Zielserver.

Die Implementierung dieser Umstellung ist mit dieser Prüfung noch nicht erfolgt.
