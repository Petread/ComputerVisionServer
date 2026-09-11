# Realtime Performance Follow-up Plan

## Umsetzungsstand

Stand 21. Juli 2026 sind die P0- und P1-Codeumsetzungen abgeschlossen:

- Segmentierungs-NMS wird fuer neue Exporte korrekt manifestiert.
- Bestehende rohe YOLO-Segmentierungsoutputs erhalten den
  Kompatibilitaets-Fallback, klassenweise vektorisierte NMS und Pre-NMS-Top-K.
- Der Executor begrenzt In-Flight-Frames auf das Per-Release-Limit und meldet
  Grenzwerte sowie Begrenzungsgrund in den Live-Diagnostics.
- Modellbindungen werden vor dem ersten Frame aufgeloest und danach lokal
  lock-frei gelesen.
- Capability-Refresh ist aus dem per-Frame-Resolver entfernt; Gateway- und
  Machine-Credential-Cache-Misses sind Single-Flight abgesichert.
- Regressionstests fuer Export-NMS, Legacy-Releases, klassenweise NMS,
  Pre-NMS-Top-K, In-Flight-Clamping und Preloading sind ergaenzt.
- Der Live-Pfad behaelt Kamera-Frames in BGR und kodiert sie direkt ueber
  OpenCV. Die bisherige PIL- sowie BGR-nach-RGB-Zwischenkopie entfaellt.
- Inferenzframes werden vor JPEG auf den Modelleingang verkleinert. YOLO-
  Geometrieaufgaben verwenden das beim Training genutzte Letterboxing; Boxen,
  orientierte Boxen und Polygone werden danach auf das Quellbild zurueckgerechnet.
- Raw- und Overlay-Stream materialisieren ein Frame erst am naechsten
  Publikationszeitpunkt. Die Standardgrenze liegt nun bei 1280x800, wobei das
  Seitenverhaeltnis erhalten bleibt.
- Triton-System-SHM wird pro Release als registrierter Pool in Groesse des
  Per-Release-Concurrency-Limits wiederverwendet und beim Gateway-Shutdown
  sauber abgemeldet.
- Maskenkonturen laufen im Produktionspfad ueber OpenCV `findContours` und
  `approxPolyDP`; der Python-Kantengraph bleibt nur als Dependency-Fallback.
- Native NumPy-/BLAS-/OpenMP- und OpenCV-Threadpools sind fuer die drei
  Inference-Gateway-Prozesse standardmaessig auf einen Thread begrenzt. Der
  A/B-Lauf reduzierte den Realtime-Gateway von 2769 % CPU bei 41 Threads auf
  24,4 % CPU bei 10 Threads. Triton und Training bleiben davon unberuehrt.
- Regressionstests decken BGR-Letterbox und Geometrie-Ruecktransformation,
  Stream-Materialisierung, 1280x800-Standardgrenzen, SHM-Wiederverwendung und
  nicht-rechteckige OpenCV-Konturen sowie die service-spezifischen nativen
  Thread-Limits ab.

Der operative P0/P1-Abnahmelauf bleibt bewusst ein Rollout-Gate: Nach
Installation dieses Releases wird der Zielkanal auf 25 Kamera-FPS und
In-Flight 2 gesetzt und nach dem unten definierten Protokoll gemessen. Die
laufende Anlage wird nicht vor Installation des zu pruefenden Codes
umkonfiguriert.

## Ausgangslage

Die Messung vom 21. Juli 2026 fuer Kanal
`chn_053bebf9dd4a4ab586f3af37d1d20e48` zeigt, dass nicht die reine
TensorRT-Inferenz, sondern der CPU-, Transport- und Scheduling-Pfad den
Durchsatz begrenzt.

| Kennzahl | Beobachtung |
| --- | ---: |
| Kameraeingang | 38,8 FPS |
| Verarbeitete Frames | 14-15,5 FPS |
| Frame-Drops | 58-61 % |
| Pipeline p95 | 318-334 ms |
| Triton Compute-Infer | 12,6 ms |
| Triton inklusive Tensor-I/O | 24,1 ms |
| Realtime-Pipeline In-Flight | 4 effektiv, 10 konfiguriert |
| Gateway-Limit pro Release | 2 |
| Raw-Livestream | ca. 20 FPS bei 1920x1200 |

Das eingesetzte Segmentierungsrelease ist YOLOv11 Small mit 960x960
Modelleingang. Die Live-Ausgabe enthaelt stark ueberlappende Mehrfachtreffer,
weil die YOLO-Segmentierungs-NMS im Deployment-Manifest deaktiviert wird.

## Zielbild

- 25 FPS Kameraeingang als reproduzierbarer Baseline-Test
- mindestens 23 erfolgreich verarbeitete FPS im 60-Sekunden-Fenster
- weniger als 5 % nicht explizit konfigurierte Frame-Drops
- Pipeline-Latenz p50 unter 70 ms und p95 unter 100 ms
- keine gleichklassigen Mehrfachtreffer mit IoU groesser oder gleich 0,65
- keine PostgreSQL-Abfrage oder Capability-Aktualisierung pro Frame
- maximal so viele In-Flight-Frames wie das ausgewaehlte Release zulaesst
- stabiler Raw-Stream mit mindestens 24 FPS bei der vereinbarten Aufloesung

## P0 - Korrektheit und Hot-Path-Stabilisierung

### P0.1 Segmentierungs-NMS

- `TaskType.SEGMENTATION` beim Aufbau des Export-Manifests als NMS-faehig
  behandeln.
- Fuer bestehende YOLO-Segmentierungsreleases, die aufgrund des bisherigen
  Exportfehlers `nms.enabled=false` enthalten, einen eng begrenzten
  Kompatibilitaets-Fallback im Gateway anwenden.
- NMS klassenweise und vektorisiert vor der Maskenrekonstruktion ausfuehren.
- Kandidaten vor NMS auf ein konfigurierbares Top-K begrenzen.
- `max_segments` erst auf die unterdrueckten Instanzen anwenden.
- Tests fuer gleiche Klasse, verschiedene Klassen, deaktivierte NMS und alte
  fehlerhafte Segmentierungsmanifeste ergaenzen.

### P0.2 Parallelitaetsgrenzen

- Die Realtime-Pipeline auf das Per-Release-Concurrency-Limit begrenzen.
- Eine Kanalangabe oberhalb des Limits darf keine zusaetzlichen wartenden
  Gateway-Requests erzeugen.
- Den effektiven Grenzwert und seine Herkunft in den Live-Diagnostics
  ausweisen.
- Stateful Pipelines bleiben strikt bei einem In-Flight-Frame.

### P0.3 Modellbindung und Datenbankzugriffe

- Alle logischen Modellreferenzen beim Start der Runtime einmalig aufloesen.
- Der Frame-Kontext verwendet danach nur noch unveraenderliche Bindings aus
  einem lokalen Lookup.
- `refresh_runtime_model_capability_cache` aus dem per-Frame-Resolver
  entfernen. Die Aktualisierung erfolgt bei Deployment-Aenderungen oder in
  einem expliziten/background Refresh-Pfad.
- Cache-Misses im Gateway und in der Machine-Credential-Aufloesung mit
  Single-Flight gegen parallele Refresh-Stampedes absichern.

### P0.4 Baseline-Betrieb

- Nach Auslieferung Kamera auf 25 FPS und Pipeline In-Flight auf 2 stellen.
- Je einen 60-Sekunden-Lauf ohne und mit Raw-Livestream aufzeichnen.
- Erst nach bestandener 25-FPS-Baseline auf 30 FPS erhoehen.

## P1 - CPU- und Transportkosten

Status: Code abgeschlossen, Zielsystem-Benchmark nach Rollout ausstehend.

- BGR-Frames direkt per OpenCV kodieren; PIL- und BGR-nach-RGB-Kopie aus dem
  Inferenzpfad entfernen.
- Inferenzbild vor der Kompression auf die Modelleingangsgroesse reduzieren
  und das zum Training passende Letterboxing verwenden.
- Raw-Stream vor `tobytes()` auf den Publikationstakt begrenzen und bei Bedarf
  auf 1280x800 reduzieren.
- Pro Release einen persistent registrierten Shared-Memory-Pool verwenden.
- Maskenkonturen ueber OpenCV `findContours` und `approxPolyDP` statt ueber
  Python-Kantengraphen erzeugen.
- Native CPU-Threadpools nur in den Inference-Gateway-Prozessen begrenzen;
  Standardwert `1`, bei Bedarf ueber
  `YBB_INFERENCE_GATEWAY_NATIVE_THREADS` abstimmbar.

## P2 - GPU-naher Datenpfad

- Realtime-Triton verwendet auf dem Zielprofil zwei getrennte
  TensorRT-Instanzen passend zum Per-Release-In-Flight-Limit von zwei. Die
  Instanzzahl ist ueber `triton.realtime.instance_group_count` konfigurierbar;
  dynamisches Batching bleibt fuer niedrige Latenz deaktiviert.
- Neue zustandslose Kanaele verwenden standardmaessig zwei parallele
  Pipeline-Frames. Bestehende Kanaele mit explizitem
  `pipeline_max_inflight=1` muessen einmalig auf `2` gestellt werden.
  Zustandsbehaftete Pipelines werden unabhaengig davon weiterhin auf einen
  Frame begrenzt.
- uint8/BGR- oder NV12-Tensoren statt Full-Resolution-JPEG transportieren.
- Resize, Letterbox und Normalisierung in einen Triton-Ensemble-, DALI- oder
  CUDA-Preprocessing-Pfad verschieben.
- GPU-/TensorRT-Postprocessing fuer NMS und Masken evaluieren.
- Eine zweite Triton-Modellinstanz erst nach Beseitigung der CPU- und
  Transportengpaesse benchmarken.
- Stream-Publikation bei geeigneter MediaMTX-Integration bedarfsgesteuert
  starten.

## Mess- und Abnahmeprotokoll

Jeder Lauf dauert nach einer Aufwaermphase von 30 Sekunden weitere 60
Sekunden. Erfasst werden:

- Source-, Processed- und Stream-FPS
- Buffer-, Busy-, Deadline- und Stale-Result-Drops getrennt
- Pipeline p50/p95/p99
- Model-Resolve, Image-Prepare, Admission-Wait und Client-Roundtrip
- Gateway Auth, Release-Resolve, Preprocess, Triton und Postprocess getrennt
- Triton Queue, Compute-Input, Compute-Infer und Compute-Output
- CPU-Gesamtlast und pro Prozess
- GPU Compute, Speicher, Copy Engine und NVENC getrennt
- Anzahl Resultate pro Klasse sowie gleichklassige IoU-Duplikate

Die Reihenfolge der A/B-Laeufe ist:

1. P0-Code, 25 FPS, In-Flight 2, Raw-Stream aus
2. identische Konfiguration, Raw-Stream an
3. Stream auf 1280x800 begrenzt
4. P1-Bildpfad aktiv
5. 30-FPS-Ziellauf

Ein Schritt gilt nur dann als verbessert, wenn der Durchsatz steigt, ohne dass
p95-Latenz, Fehlerquote oder Erkennungsqualitaet regressieren.
