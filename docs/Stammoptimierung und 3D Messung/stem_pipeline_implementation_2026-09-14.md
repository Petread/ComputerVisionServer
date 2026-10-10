# Umsetzung Schritte 1–3 – 14.09.2026

Die Änderungen betreffen die bestehende High-Rate-Runtime, ihren Frame-Ring und die Geometrieberechnung. Sie implementieren die ersten drei Schritte des Pipeline-Audits. Deployment und Leistungsabnahme am Förderer stehen separat an. Das physikalische Modell für das Rollen über die Erhöhung und die SPS-Ansteuerung wurden nicht erweitert.

## 1. Bildübernahme

- `HighRateStemPhaseBridge` übernimmt Seeds, Zähler und veröffentlichte Ergebnisse über kurze Sperren. Ausschließlich der Phasenworker verarbeitet den privaten `_PhaseTracker`; Optical Flow, Feature-Erkennung und Replay laufen außerhalb der Übergabesperre.
- Reset, Stamm-/Sessionwechsel, Kalibrierungswechsel und Reconnect invalidieren laufende Phasenergebnisse über Generationen. Kamera-Generationen werden bis zum Seed mitgeführt, damit verspätete Pipelineframes nach einem Reconnect keine alte Spur wieder einsetzen.
- `SharedFrameRingReader.read_next()` liest vorhandene Zwischenframes geordnet. Nicht fortlaufende Kamerasequenzen blockieren den Leser nicht. `read_latest()`, die Aliasnamen und das Shared-Memory-Format bleiben kompatibel. Der Executor wählt geordnetes Lesen bei aktiver High-Rate-Phase.
- Die FPS-Begrenzung berücksichtigt Aufnahmezeitpunkte: Mehrere unmittelbar hintereinander ausgelesene Ringframes gelten dadurch nicht als gleichzeitig aufgenommener Burst. Die bestehenden Queuegrößen, Replay- und Seed-Altersgrenzen bleiben bestehen.
- Worker-Stopp und ausstehende Seeds benötigen keinen weiteren Kameraframe. Veraltete Queue-Einträge werden anhand von Aufnahme- und Empfangszeit verworfen. Ein Fehler während einer veränderlichen Trackingoperation invalidiert die betroffene Generation.

## 2. Replay und Zuordnung

Beim verspäteten Seed werden Rohphase und kumulierte Translation aus **demselben exakten historischen Snapshot** übernommen. Keyframes aus der Zukunft dieses Seeds werden ausgeschlossen. Jeder Replay-Schritt erhält einen eigenen Snapshot.

Die Regression integriert 10 Pixel pro Schritt: Nach Frames 1, 2 und 3 wird Frame 2 neu geseedet und bis Frame 4 replayt. Das Ergebnis beträgt **30 Pixel statt bisher 40 Pixel**. Weitere Tests vergleichen tatsächlichen Optical Flow bei unmittelbarer Verarbeitung und Replay.

Ein Seed für einen durch die FPS-Begrenzung ausgelassenen Frame kann dessen tatsächlich vorhandenes Graubild verwenden. Für einen bereits vergangenen Frame ohne passenden historischen Zustand wird `PHASE_SEED_HISTORY_MISSING` ausgegeben. Es wird kein neuerer Snapshot als Ersatz genommen und keine Ersatzbeobachtung interpoliert.

Die Phase wartet begrenzt auf die passende Sequenz und Generation. Grenzen sind das Replayfenster, bei verfügbaren Aufnahmezeitstempeln dessen verbleibende Dauer und die vorhandene Pipeline-Deadline. Ablauf liefert `PHASE_PAIR_DEADLINE_EXPIRED`; historische Seeds außerhalb des Fensters liefern `PHASE_SEED_HISTORY_EXPIRED`. Dies ist eine Software-Deadline; die bestehenden fachlichen Entscheidungs-/Freeze-Gates bleiben zusätzlich aktiv.

Der Slice-Builder verlangt bei `HIGH_RATE_SEEDED` dieselbe Frame-Sequenz und lehnt veraltete Phasengenerationen ab. Seine vorhandenen Track-, Kalibrierungs-, Bewegungs- und Qualitätsprüfungen bleiben aktiv. Im synchronen Modus bleibt die bisherige konfigurierbare Sequenz-Toleranz erhalten.

## 3. Geometrie und native Threads

- Ein begrenzter Cache im `RuntimeExecutionContext` hält geeignete Zwischenresultate nur für die jeweilige Ausführung. Graubilder, Oberflächenmasken und identische Geometrieanfragen können wiederverwendet werden. Besitzerreferenzen verhindern Verwechslungen durch wiederverwendete Objekt-IDs.
- ROI-Raster werden unveränderlich und mit maximal acht Einträgen gecacht. Der Schlüssel enthält Kalibrierungshash, Bildgröße und das tatsächlich projizierte Polygon; auch eine geänderte Projektion bei gleichem Hash erzeugt ein neues Raster.
- Die Stationsgrenzen der horizontalen Krümmung werden aus vektorisierten Spaltengrenzen ermittelt. Intervallgrenzen, Rundung, Stationsdefinition, Qualitätsprüfungen und Ablehnungsgründe bleiben gleich. Die Bestimmung der führenden X-Position benötigt keine Koordinatenliste aller Maskenpixel mehr.
- `YBB_REALTIME_EXECUTOR_NATIVE_THREADS=1` beziehungsweise `2` setzt die nativen BLAS-/OpenMP-/OpenCV-Threadgrenzen beim Start des bestehenden Executor-Pakets. OpenCV wird zusätzlich ausdrücklich konfiguriert. Ohne diese Variable erfolgt keine Umstellung. Ungültige Werte brechen den Start mit einer verständlichen Meldung ab.

Die Variable gehört in die Umgebung des bestehenden Realtime-Executor-Dienstes, beispielsweise in dessen vorhandene Dienstkonfiguration. Für einen Vergleich ist jeweils ein Neustart des Executors erforderlich, damit BLAS die Einstellung vor dem ersten Import erhält. Es wurde keine Einstellung auf dem Zielserver geändert.

## Diagnosen

Die zusätzlichen Zähler verwenden die bestehenden freien Timing-/Diagnosefelder; API-Verträge und Frontend-Schemas benötigen keine neuen Felder. Für einen Messlauf jeweils die **Zählerdifferenz** zwischen Beginn und Ende auswerten.

| Feld | Bedeutung |
| --- | --- |
| `frame_ring_overwritten_frames` | Nicht mehr verfügbare Sequenzpositionen im Ring |
| `frame_ring_skipped_frames` | Beim Lesen ausgelassene Sequenzpositionen, einschließlich Lücken der Quelle |
| `high_rate_phase_acquired_frames` | Beim High-Rate-Pfad angekommene Quellframes |
| `high_rate_phase_processed_frames` | Unterschiedliche verarbeitete Sequenzen; Replay zählt diese nicht nochmals |
| `high_rate_phase_replayed_frames` | Zusätzliche Replay-Schritte |
| `high_rate_phase_rate_skips` | Durch die Ziel-FPS-Begrenzung ausgelassene Frames |
| `high_rate_phase_queue_drops` | Aus der begrenzten Queue verworfene Frames |
| `high_rate_phase_stale_frames` | Wegen Altersgrenze verworfene Frames |
| `high_rate_phase_obsolete_frames` / `high_rate_phase_obsolete_seeds` | Wegen Generation verworfene Arbeit |
| `high_rate_phase_duplicate_frames` | Doppelte oder rückläufige Sequenzen |
| `high_rate_phase_processing_failures` | Fehler bei Vorverarbeitung, Seed oder Tracking |
| `high_rate_phase_valid_measurement_pairs` | Exakte Geometrie-/Rohphasenpaare mit verriegelter Phase; noch unabhängig von einer gültigen Gradkalibrierung |
| `high_rate_phase_accepted_profile_pairs` | Vom Slice-Builder tatsächlich akzeptierte kalibrierte Profil-/Phasenpaare |
| `high_rate_phase_pair_timeouts` | Abgelaufene Wartezeiten auf passende Phasen |

Ringverluste werden anhand von Sequenznummern ermittelt. Bei Sprüngen der Kameraquelle sind diese Zahlen keine nachgewiesene Anzahl physisch aufgenommener Bilder.

## Verifikation

Abschließender fokussierter Lauf: **111 Tests bestanden, 1 bekannter unabhängiger Bestandsfehler abgewählt**. Der vorherige Lauf einschließlich dieses Vertragstests hat den unten beschriebenen Fehler bestätigt. Syntaxprüfung und `git diff --check` bestehen; der Ruff-Vergleich gegen HEAD zeigt keine neu eingeführten Befunde. Vorhandene Ruff-Befunde in Bestandsdateien bleiben außerhalb dieses Auftrags.

Geprüft wurden insbesondere blockierender Optical Flow bei gleichzeitigem Handoff/Reset, Ringüberlauf, Latest-Reader-Kompatibilität, doppelte Frames, Quellwechsel, Sessionwechsel, fehlende exakte Historie, Seedfehler, Deadline-Ablauf und die Trennung des synchronen Modus. Die Geometrievergleiche umfassen gerade Stämme, einfache Krümmung, S-Krümmung, leere Masken, Fragmente und Störpixel. Stationswerte und vollständige Messresultate einschließlich Ablehnungsgründen stimmen exakt mit der bisherigen Stationsberechnung überein.

Die lokalen Prüfungen laufen mit Python 3.14.5, NumPy 2.5.1 und OpenCV 4.14.0 unter Windows. Die Zielruntime muss zusätzlich mit ihren ausgelieferten Bibliotheksversionen geprüft werden. Fehlende Testbibliotheken wurden ausschließlich unter `.test-tmp/stem-deps` ergänzt.

Das lokal erzeugte OpenAPI-Schema von `realtime-executor` stimmt strukturell vollständig mit `contracts/openapi/realtime-executor.v1.yaml` überein. Es wurden keine generierten Vertragsdateien verändert.

Ein unabhängiger Bestandsfehler liegt in `test_runtime_service_set_is_complete`: Die erwartete Dienstliste enthält `camera-media` nicht, die bestehende Topologie enthält den Dienst bereits. Test und Topologie sind gegenüber HEAD unverändert. Dieser Fehler wird nicht durch eine Änderung der Dienstliste verdeckt.

Fokussierte Wiederholung in einer eingerichteten Repository-Testumgebung:

```powershell
python -m pytest --confcutdir=tests/unit/backend -q `
  tests/unit/backend/test_stem_high_rate_handoff.py `
  tests/unit/backend/test_stem_geometry_optimization.py `
  tests/unit/backend/test_runtime_stem_surface_phase.py `
  tests/unit/backend/test_stem_horizontal_curvature.py `
  tests/unit/backend/test_stem_geometry.py `
  tests/unit/backend/test_runtime_stem_horizontal_curvature.py `
  tests/unit/backend/test_runtime_stem_geometry.py `
  tests/unit/backend/test_runtime_stem_slice_model.py `
  tests/unit/backend/test_runtime_stem_temporal.py
```

## Reproduzierbarer Benchmark

`scripts/benchmark/stem_geometry_benchmark.py` misst die vorhandenen Geometriefunktionen: reguläre Geometrie, schnelle Phasengeometrie und horizontale Krümmung. Ein oder zwei gleichzeitig berechnete Frame-Aufträge erlauben einen Vergleich der CPU-Konkurrenz. Kamera, Inferenz, Phasentracking, Warteschlangenlatenz und fachliche Entscheidungsgates sind nicht Bestandteil dieses Mikrobenchmarks.

Eingabe ist eine JSON-Datei mit:

- `process_calibration`: vollständiges gültiges Prozesskalibrierungsprofil;
- `aligned_mask_path`: relativer Pfad einer zweidimensionalen `uint8`-NPY-Maske im ausgerichteten Prozessraster;
- `polygon_normalized`: Segmentpolygon desselben Stamms im kanonischen Bild, als Liste von `[x, y]`;
- optional `view_transform_parameters`: die Transformationsparameter des zugehörigen Runtimeframes;
- optional `geometry_config` und `curvature_config`: die zu vergleichenden Algorithmusparameter.

Maske, Polygon, Bildgröße, Prozessgrenzen und Kalibrierung müssen zu derselben Aufnahme gehören. Der Benchmark startet keine Anwendung und schreibt nur die angegebene Ergebnisdatei.

Die vier Kombinationen in separaten Prozessen ausführen:

```powershell
foreach ($frameCount in 1, 2) {
  foreach ($nativeCount in 1, 2) {
    python scripts/benchmark/stem_geometry_benchmark.py `
      --input benchmark-case.json --frames 200 `
      --pipeline-frames $frameCount --native-threads $nativeCount `
      --output "geometry-f$frameCount-t$nativeCount.json"
  }
}
```

Eingabehash, Ergebnisfingerabdruck, Bibliotheksversionen, P50/P95-Rechenzeit und Durchsatz werden ausgegeben. Der Runner prüft jeden berechneten Frame gegen einen seriellen Referenzlauf. Für den Vergleich zwischen Konfigurationen müssen zusätzlich deren `result_sha256`-Werte übereinstimmen. Mindestens drei Durchläufe pro Kombination unter gleicher Hintergrundlast ausführen.

Ein lokaler Funktionslauf mit derselben synthetischen Maske (640 × 480, jeweils 30 Frame-Aufträge) ergab bei allen vier Kombinationen denselben Ergebnisfingerabdruck:

| Gleichzeitige Frames | Native Threads | P50 pro Frame | P95 pro Frame | Kernel-Frame-Aufträge/s |
| --- | --- | --- | --- | --- |
| 1 | 1 | 70,82 ms | 80,00 ms | 13,74 |
| 1 | 2 | 71,99 ms | 91,05 ms | 13,13 |
| 2 | 1 | 188,42 ms | 208,55 ms | 10,52 |
| 2 | 2 | 221,46 ms | 251,46 ms | 9,07 |

Separat sank die mediane Stationsberechnung im lokalen Vergleich von 2,26 ms auf 0,99 ms. Diese synthetischen Messungen bestätigen die Funktionsfähigkeit des Vergleichs; die Betriebseinstellung wird durch die Serverabnahme bestimmt.

### Abnahme der vollständigen Pipeline auf dem Zielserver

1. Dieselbe aufgezeichnete Rollsequenz, Kalibrierung, Pipelineversion, Belichtung und Segmentierung für alle Läufe verwenden. Release-Hash und Bibliotheksversionen festhalten.
2. Im bestehenden Executor die Konfiguration für maximal einen beziehungsweise zwei gleichzeitig laufende Pipelineframes und jeweils einen beziehungsweise zwei native Threads vergleichen. Der Mikrobenchmark-Schalter `--pipeline-frames` ändert keine Serverkonfiguration.
3. Neben P50/P95 der Geometriestufen insbesondere übernommene Quellframes, verarbeitete Phasenframes, Verluste, Deadline-Abläufe und **akzeptierte Profil-/Phasenpaare pro etwa 0,5 Sekunden Rollvorgang** erfassen.
4. Fälle mit fehlender Phase oder Generationwechsel müssen ohne gültige Gradfreigabe bleiben. Freigegebene Korrekturen weiterhin nur mit gültiger Winkelkalibrierung und vor dem bestehenden Entscheidungszeitpunkt bewerten.
5. Die Messreihen im Shadow-Betrieb auswerten. Die Wahl der Thread-/Parallelitätseinstellung anhand gültiger Messpaare und rechtzeitiger Ergebnisse treffen; die physikalische Validierung der Erhöhung bleibt ein eigener nachfolgender Schritt.
