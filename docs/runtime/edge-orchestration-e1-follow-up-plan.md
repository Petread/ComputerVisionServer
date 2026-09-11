# Edge-Orchestrierung E1 – Productive Edge Execution

- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`
- Stand: 2026-07-22
- Grundlage: M0–M5 und `SPEC_GAPS_Edge_Orchestration.md`

## Ergebnis

E1 schließt den softwareseitigen P0-Datenpfad zwischen einem lokal erfassten Frame und der
vorhandenen zentralen Result-Synchronisation. Der Edge-Agent lädt den übertragenen, strikt
validierten Pipeline-Graphen, führt registrierte Nodes topologisch aus, lädt die beim Prepare
gebauten TensorRT-Pläne und erzeugt echte `runtime.execution.result.v1`-Payloads. Diese werden
weiterhin als `EdgeResultEnvelope` in der SQLite-WAL-Outbox persistiert und erst nach dem
serverseitigen Commit bestätigt.

Der zweite Listener auf TCP 9444 ist bewusst keine zweite Verwaltungs-API. Er stellt einer
freigegebenen SPS oder Anlagensteuerung nur Health, einen begrenzten Kanaltrigger und das letzte
Triggerergebnis bereit. Geräteverwaltung, Artefakte, Deployment und Result-ACK bleiben
ausschließlich auf der serverseitigen Control-API 9443.

## Implementierter Scope

### Graph-Executor und Handler-Registry

`edge_agent.graph.EdgeGraphExecutor`:

- lädt `edge.pipeline.graph.v1` ausschließlich aus dem SHA-256-adressierten Blob-Store;
- validiert den Inhalt als `RuntimePipelineGraph` und lehnt unbekannte Nodes oder Zyklen ab;
- sortiert Nodes topologisch und routet Werte anhand der Graph-Ports;
- misst Dauer pro Node und für die gesamte Ausführung;
- hält keine freie Python-Plugin- oder Code-Loading-Schnittstelle offen;
- erzeugt serialisierbare Resultate mit Modellroute, Roh-Tensor-Metadaten, normalisierten
  Outputs, abgeleiteten Outputs, Persistenzprojektionen und Overlay-Primitiven.

Die produktive Registry enthält die freigegebenen Source-, Trigger-, Resize-, Crop-, Sampler-,
Classification-, Detection-, Oriented-Detection-, Segmentation-, Anomaly-, Overlay-, Result-,
Persisted-Result-, HTTP-Trigger-Response- und Siemens-SPS-Schreib-Nodes. Die Capability-Liste wird mit dieser Registry
geschnitten. Eine konfigurierte, aber nicht installierte Node-Art kann dadurch nicht mehr als
Capability gemeldet oder bei `prepare` zugelassen werden.

### TensorRT-Ausführung

`edge_agent.inference.TensorRtEngineSession`:

- deserialisiert echte `.plan`-Dateien mit der lokal installierten TensorRT-Python-Runtime;
- unterstützt die Tensor-API von TensorRT 10 und einen Binding-Fallback für TensorRT 8;
- setzt dynamische Input-Shapes vor der Ausführung;
- verwaltet CUDA-Stream sowie Host-to-Device- und Device-to-Host-Transfers;
- adressiert Eingabe- und Ausgabetensoren anhand der Engine-Metadaten;
- serialisiert parallele Zugriffe pro Engine-Session;
- führt vor der ersten produktiven Inferenz standardmäßig zwei Warmup-Läufe aus;
- gibt CUDA-, Shape-, Binding- und Enginefehler als deterministische Admission-/Runtimefehler
  zurück.

Produktive Modell-Deployments akzeptieren keine simulierte Engine als stillen Fallback. Der
Modus `simulated` bleibt ausschließlich für M0–M5-Protokoll- und Deploymenttests erhalten. Die
reale Runtime importiert `tensorrt` und `cuda-python` erst beim Laden einer Engine, sodass der
Control-Agent ohne GPU weiterhin diagnostizierbar bleibt.

### Preprocessing, Postprocessing und Golden Outputs

Workload Releases enthalten nun zusätzlich `io_schema`, `preprocessing`, `postprocessing`, NMS
und Warmup-Konfiguration aus dem freigegebenen Model-Export-Manifest. Implementiert sind:

- RGB/BGR-Konvertierung, Stretch oder Letterbox, Padding, Scale, Mean/Std und CHW/HWC;
- Classification mit Softmax-Erkennung, Top-K und Class-Map;
- Detection mit NMS, Confidence-Grenze und Rückprojektion in Quellbildkoordinaten;
- NVIDIA-EfficientNMS-Mehrfachoutputs sowie rotierte Boxen mit Winkel und vier Eckpunkten;
- Segmentation mit Klassenmaske und begrenzten Polygon-/Flächenprojektionen;
- Anomaly-Score, Grenzwert und OK/Anomaly-Entscheidung;
- Roh-Tensor-Summaries ohne große Tensorpayloads in der Result-Outbox.

Golden Tests behalten `artifact_integrity` für alte Releases. Sobald ein Expected Output
vorhanden ist, verwendet der Release-Builder standardmäßig `numeric_allclose`. Unterstützt sind
exakte oder tolerierte `.npy`-/`.npz`-Vergleiche sowie strukturierte `normalized_json`-
Vergleiche. Shape-, Tensorname- und Toleranzabweichungen blockieren `prepare`.

### Resultate und Overlays

Jede erfolgreiche Graphausführung erzeugt eine `runtime.execution.result.v1`-Payload mit:

- Channel-, Pipeline-, Workload- und Correlation-ID;
- normalisierten Task-Ergebnissen und kompakten Roh-Tensor-Metadaten;
- Model-Version und Deployment-Release pro Inference-Node;
- Node- und Gesamtlatenz;
- finaler beziehungsweise triggerbezogener Antwort;
- Overlay-Primitiven in Quellbildkoordinaten.

Der vorhandene M4-Previewpfad überträgt Overlay-Metadaten und zeichnet Boxen, Polygone oder
Labels nur, wenn mindestens eine aktive Viewer-Lease `include_overlay=true` fordert. Ohne Lease
entsteht weiterhin kein Livebild-Uplink.

### Plant-/SPS-Trigger-Service 9444

Der Dual-Server-Entrypoint `python -m edge_agent.server` bindet beide Listener ausschließlich an
die konfigurierte feste Edge-Geräte-IP (API-Feld `management_ip`):

- `GET /edge-plant/v1/health`;
- `PUT /edge-plant/v1/channels/{channel_id}/trigger`;
- `GET /edge-plant/v1/channels/{channel_id}/last-result`.

Sicherheits- und Belastungsgrenzen:

- IP-Allowlist aus `YBB_EDGE_PLANT_ALLOWED_IPS`, zusätzlich lokale Loopback-Diagnose;
- keine Auswertung von Forwarded-Headern und keine freie Ziel-URL;
- standardmäßig maximal 64 KiB Requestbody;
- verpflichtender `X-Idempotency-Key` und persistenter Request-Digest;
- standardmäßig vier tatsächlich parallel laufende Trigger;
- maximal 20 Requests pro Sekunde und Client-IP;
- serverseitige Antwortfrist maximal 2500 ms, pro Request nur weiter reduzierbar;
- ein nach Timeout weiterlaufender GPU-Auftrag persistiert sein Ergebnis weiterhin genau einmal
  und kann mit demselben Idempotency-Key oder über `last-result` abgeholt werden;
- kein Artefakt-, Deployment-, Shell-, Datei- oder allgemeiner SPS-Schreibendpunkt auf 9444.

VLAN, Switch-ACL und Host-Firewall bleiben Teil des Trust-Modells. UFW und Windows Firewall
erhalten getrennte Regeln: 9443 nur für die Orchestrierungsserver-IP, 9444 nur für die
expliziten HTTP-Trigger-Clients.

## Geänderte Deployment-Artefakte

- systemd und WinSW starten den Dual-Listener über `edge_agent.server`;
- Jetson- und Windows-Beispielkonfigurationen enthalten Allowlist und Triggerlimits;
- der Offline-Wheelhouse muss zusätzlich `numpy`, `Pillow` und `cuda-python` enthalten;
- `contracts/openapi/edge-plant.v1.yaml` ist der versionierte Plant-Vertrag;
- der State-Store migriert additiv auf Schema 5 und persistiert Trigger-Idempotenz.

## Automatisierte Abnahme

Abgedeckt sind:

- Letterbox/Normalize/Layout und task-spezifisches Classification-Postprocessing;
- tolerierter Golden-Tensorvergleich;
- topologische Graphausführung, Modellprovenienz, Persisted Result und Overlay;
- Plant-Health, Trigger, durable Idempotenz, Last-Result und Bodylimit;
- M0-, M2-, M3- und M4-Regressionssuite;
- Ruff und Python-Kompilierung der geänderten Module;
- OpenAPI-Export und strikte Plant-Verträge.

Der finale automatisierte Edge-Korridor umfasst 75 bestandene Tests, davon neun neue
E1-Unit-, Integrations- und Contract-Tests. Ruff, Python-Kompilierung und `git diff --check`
sind für die geänderten E1-Module ebenfalls erfolgreich.

## Hardware-Gate

Der Software-Slice ist abgeschlossen, aber nicht hardwareabgenommen. Auf dem bereitgestellten
Jetson Orin Nano sind mindestens nachzuweisen:

1. lokaler FP16- und optional INT8-Build mit dem freigegebenen ONNX-Modell;
2. Deserialisierung, Warmup und 30 Minuten echte TensorRT-Inferenz;
3. Golden `.npz` oder strukturierter Golden Output innerhalb der freigegebenen Toleranzen;
4. Detection, Classification, Segmentation oder Anomaly passend zum Pilotmodell;
5. echte Kamera bei Zielauflösung und Samplingrate;
6. Result-Replay und Overlays während Server-/VLAN-Unterbrechung;
7. Trigger von der freigegebenen SPS-IP sowie Ablehnung einer fremden VLAN-IP;
8. Timeout, Rate Limit, Concurrency Limit und Idempotency-Replay;
9. GPU-, RAM-, Temperatur-, FPS- und P50/P95/P99-Messwerte.

Bis dieser Bericht akzeptiert ist, bleibt E1 insgesamt `pending_hardware` und die
Gesamtlösung `not_production_complete`.

## Bewusst verbleibende Grenzen

- `runtime_plugin`-Artefakte führen weiterhin keinen fremden Code aus; zusätzliche Nodes
  benötigen eine geprüfte Registry-Erweiterung und gehören zu E2/Hardening.
- Reale Aravis-, Basler- und FLIR-Adapter sowie Prozessisolation bleiben E2.
- Automatischer Offline-Bundle-Build und reale Capability-Inventarisierung bleiben E2.
- Der zentrale `uv.lock`-Snapshot stammt aus der Zeit vor der vollständigen M1–M5-
  Workspace-Erweiterung. Seine Regeneration wird derzeit durch den auf Python 3.11 begrenzten
  `training-worker` bei gleichzeitig deklarierten Python-3.12-Workspace-Umgebungen blockiert.
  E1 deklariert seine Laufzeitabhängigkeiten im Agent-Paket; der reproduzierbare,
  plattformspezifische Offline-Lock bleibt Bestandteil von E2/M2-G01.
- Physische Orin-/AGX-/Windows-Abnahme bleibt E3.
- Persistente Alert-Lifecycles, Fleet-Wellen und Runtime-Updates bleiben E4/M6.
