# Wiederanlauf, Seedzuordnung und Geometrie – Umsetzung 15.09.2026

Die Softwarekorrekturen aus der [Analyse nach dem Update](stem_pipeline_postupdate_analysis_2026-09-15.md) sind implementiert. Der Wiederanlauf nach einem veralteten Seed funktioniert jetzt mit einer neuen, getrennten Phasengeneration. Fehlende Seedsequenzen können aus einem zulässigen historischen Trackingzustand mit echten Bildern rekonstruiert werden. Die Leistungsabnahme am realen Rollweg einschließlich Erhöhung bleibt nach dem nächsten Deployment erforderlich.

**1. Wiederanlauf ohne Vermischung von Winkelursprüngen**

`HighRateStemPhaseBridge` erkennt, wenn ein frischer Seed keine gültige historische Fortsetzung besitzt. Nach Prüfung seiner Deadline beginnt der Worker eine neue Generation und initialisiert deren Winkel- und Translationsursprung. Der vorherige Ursprung und dessen Keyframes werden nicht übernommen. Die Generation wird über die bestehende kurze Übergabesperre gewechselt; Bildverarbeitung bleibt außerhalb dieser Sperre.

Auf die alte Generation wartende Pipelineframes erhalten ein ungültiges Ergebnis. Ein bereits wartender neuerer Seed wird beim Generationswechsel ausdrücklich verworfen und gezählt. Nachfolgende reguläre Pipelineframes liefern neue Seeds. Seeds mit einer rückläufigen Sequenz können einen bereits neueren veröffentlichten Seed nicht ersetzen.

Der Slice-Builder verwirft bei Generationswechsel auch ein eingefrorenes Modell. Ein verspäteter alter Phasenwert darf die bereits neu gesammelte Historie nicht wieder zurücksetzen. Der Rotationsoptimierer prüft die Generation vor der Optimierung und nach der Berechnung. Der Krümmungsminimum-Solver prüft sie ebenfalls vor dem Zugriff auf eingefrorene Ergebnisse und alte Beobachtungen. Damit betrifft die Korrektur beide bestehenden Auswertungspfade.

Eine neue Generation bedeutet zunächst nur eine neue relative Phasenspur. Mindestbeobachtungszahl, Winkelabdeckung, Kalibrierungsprüfung und Entscheidungsgates bleiben erforderlich. Die zurückgesetzte Nullphase ist keine Wiederherstellung der absoluten Orientierung des vorherigen Spurteils.

**2. Exakte Seedzuordnung und begrenztes Replay**

Der Phasenworker hält zusätzlich begrenzte Checkpoints gültiger Zustände: Graubild, Maske, Achse, Features, Keyframes, Phase, Translation, Sequenz, Zeitstempel und Generation. Die Bildarrays werden von der Trackingimplementierung ersetzt und nicht nachträglich verändert; skalare Zustände und die veränderliche Keyframe-Liste werden separat kopiert. Zahl und Alter der Checkpoints sind durch `phase_replay_max_frames` und `phase_replay_window_ms` begrenzt.

Fehlt eine Seedsequenz, obwohl der Worker bereits weitergelaufen ist, wird der letzte passende Vorgänger innerhalb des Replayfensters wiederhergestellt. Tatsächlich vorhandene Zwischenbilder und das echte Graubild des Seeds werden verarbeitet. Anschließend werden spätere vorhandene Frames neu replayt. Es wird keine Phase interpoliert und kein neuerer Snapshot als historische Beobachtung eingesetzt.

Ein vorhandener ausdrücklich ungültiger historischer Zustand wird nicht durch eine ältere gültige Revision desselben Frames ersetzt. Ist Kontinuität nicht belastbar rekonstruierbar, greift der getrennte Wiederanlauf aus Schritt 1.

Replay prüft die Seeddeadline zwischen Bildschritten und vor der Seedanwendung. Ein bereits gestarteter OpenCV-Aufruf lässt sich damit nicht hart unterbrechen; weitere Replaybilder beginnen nach Ablauf nicht mehr. Die bestehenden Alters-, Puffer- und Qualitätsgrenzen wurden nicht erhöht. Feature-Erkennung und Maskenaufbereitung für Seeds erfolgen erst nach der historischen Zulässigkeitsprüfung, damit absehbar abzulehnende Seeds diese Arbeit nicht doppelt auslösen.

**3. Geometrie und Diagnose**

Die Längs- und Querprojektion der Geometrie verwendet denselben zentrierten Punktesatz. Die beiden Quantilgrenzen eines Querprofils werden gemeinsam berechnet. Messdefinitionen, Filter und Ablehnungsgründe bleiben erhalten. Ein Vergleich gegen die tatsächliche vorherige `stem_geometry.py` aus Git sowie Tests beider Geometriemodi bestätigen identische Ergebnisse für die geprüften Fälle.

Neue Diagnosefelder verwenden bestehende freie Timingfelder; das Realtime-OpenAPI-Schema bleibt unverändert:

| Feld | Bedeutung |
| --- | --- |
| `high_rate_phase_seed_deadline_expirations` | Abgelaufene Seed-/Replayfristen, getrennt von technischen Fehlern |
| `high_rate_phase_processing_failures` | Technische Fehler; Seedablauf wird hier nicht mehr mitgezählt |
| `high_rate_phase_recovery_generations` | Wiederanläufe mit getrenntem Winkelursprung |
| `high_rate_phase_restored_seed_frames` | Rekonstruktionen aus historischen Vorgängerzuständen |
| `high_rate_phase_worker_tracking_ms` | Letzte Workerbearbeitung einschließlich gegebenenfalls Seed und Replay |
| `high_rate_phase_preprocessing_ms` | Quellframeaufbereitung bis zum kanonischen Bild |
| `executor_opencv_threads` | Tatsächlicher Wert von `cv2.getNumThreads()` |
| `<Geometrie-Node>_support_ms` | Raster-/Komponenten-/Stützpunktaufbereitung |
| `<Geometrie-Node>_axis_ms` | Achse, Projektion und zugehörige Vorprüfungen |
| `<Geometrie-Node>_cross_sections_ms` | Querprofile einschließlich Achsenverfeinerung und Filter |
| `<Geometrie-Node>_total_ms` | Gesamter Geometrieaufruf einschließlich Cachezugriff |
| `<Phasen-Node>_image_preparation_ms` | Graubild, Maske und Achsenaufbereitung der Pipeline-Node |
| `<Phasen-Node>_seed_handoff_wait_ms` | Seedübergabe und Warten auf die passende Phase |

Die Stufenwerte beschreiben die jeweilige letzte Ausführung, keine kumulativen Zähler. Bei frühem Abbruch fehlen nachgelagerte Geometriestufen. OpenCV-Threadzahl ist nicht automatisch die effektive Threadzahl jeder BLAS-Bibliothek. Die bestehende explizite Startoption `YBB_REALTIME_EXECUTOR_NATIVE_THREADS=1|2` bleibt erhalten; ohne sie erfolgt keine automatische Umstellung.

`scripts/diagnostics/runtime_live_probe.py` speichert nun `stage_timings_ms` in jeder Probe. `scripts/benchmark/stem_geometry_benchmark.py` gibt zusätzlich `stage_p50_ms` aus; Ergebnisfingerabdrücke enthalten weiterhin ausschließlich fachliche Werte.

**4. Verifikation**

- Im breiten abschließenden Lauf bestanden 185 Tests. Die einzige verbliebene Prüfung verwendete in einem neu ergänzten Test versehentlich `solution.valid` statt des tatsächlichen Solver-Vertrags. Nach Korrektur der Assertion bestanden alle 24 Tests des gezielten Wiederanlauf-/Solver-Nachlaufs. Damit sind alle 186 unterschiedlichen Testfälle dieses Prüfsets erfolgreich abgedeckt.
- Abgedeckt sind unter anderem: Stale-/Reseed-Wiederanlauf, korrekte Generationswechsel, Schutz eingefrorener Modelle, Reset während der Optimierung, verspätete alte Phasen, ältere Seeds, begrenzte Checkpoints und Replaydeadline.
- Die Wiederherstellung einer durch Ratengrenze ausgelassenen Sequenz wurde mit echtem Optical Flow gegen unmittelbare Verarbeitung verglichen. Die Regression für verspätete Seeds liefert weiterhin 30 statt 40 Pixel Translation.
- Realtime-OpenAPI entspricht strukturell der eingecheckten YAML-Datei. Syntaxprüfung und der Ruff-Vergleich gegen HEAD zeigen keine neuen Befunde; der neue Regressionstest besteht Ruff direkt. `git diff --check` besteht.
- Für die Live-Executor-Tests wurde das beschädigte zentrale 1×1-PNG durch ein korrektes PNG gleicher Abmessungen ersetzt. Außerdem akzeptiert deren Diagnoseassertion den bereits implementierten Grund `global_inference_concurrency`. Dies sind Testkorrekturen, keine Änderungen der Produktionsgrenzen.
- Eine timingabhängige Prüfung zur Veröffentlichung nach Busy-Drops fiel in einem Zwischenlauf aus und bestand im anschließenden breiten Lauf. Eine vollständige Plattform- oder Zielserverfreigabe wird daraus nicht abgeleitet.

Lokaler Vergleich einer synthetischen Polygongeometrie mit 1600×640 Pixeln, Fast-Modus, einem OpenCV-Thread und jeweils 30 gewerteten Durchläufen: Median **5,02 → 4,63 ms**, P95 **6,52 → 5,46 ms**, identischer Ergebnisfingerabdruck. Das sind etwa 8 % weniger Rechenzeit in diesem konkreten Fall; daraus folgt keine entsprechende Verbesserung der gemessenen 174 ms auf dem Server.

Vier lokale Mikrobenchmarkläufe mit derselben vorbereiteten synthetischen 640×480-Maske und jeweils 30 Frameaufträgen ergaben ebenfalls identische Ergebnisfingerabdrücke:

| Gleichzeitige Frameaufträge | Native Threads | Median pro Frameauftrag |
| --- | --- | ---: |
| 1 | 1 | 31,35 ms |
| 1 | 2 | 31,06 ms |
| 2 | 1 | 76,44 ms |
| 2 | 2 | 78,22 ms |

Der Mikrobenchmark umfasst reguläre Geometrie, schnelle Geometrie und Krümmung. Er enthält weder Inferenz noch Bildübernahme und ist kein Nachweis des erreichbaren Produktionsdurchsatzes. Lokale Umgebung: Python 3.14.5, NumPy 2.5.1, OpenCV 4.14.0 unter Windows. Artefakte: `.test-tmp/stem-recovery-validation/verification.json` und die vier `f*-t*.json`.

**5. Abnahme nach dem nächsten Deployment**

Die neue Runtime wurde in diesem Implementierungsauftrag nicht deployt. Eine reale aufgezeichnete Rollsequenz mit bekannter Winkelreferenz wurde nicht bereitgestellt; Schritt 4 des Analyseplans bleibt daher eine physikalische Abnahme am Zielsystem.

Für die Softwareabnahme dieselbe reale Aufnahme mit identischer Kalibrierung und Segmentierung verwenden. Jeweils einen/zwei Pipelineframes und einen/zwei native Threads in getrennten Läufen vergleichen. Die Pipelineparallelität wird über das bestehende `source_config.pipeline_max_inflight` gesetzt; die effektive Begrenzung zeigt die Live-Diagnose. Die native Startoption benötigt einen Executor-Neustart. Bestehende Kanal- und Dienstkonfiguration vor dem Vergleich sichern und anschließend die anhand der Messergebnisse gewählte Einstellung übernehmen.

Pro Lauf Zählerdifferenzen, Subschrittzeiten und akzeptierte Profilpaare je physischem Rollvorgang auswerten. Erwartet wird vor allem, dass frische Seeds nach einem Spurverlust nicht über Sekunden am alten Seed hängen bleiben. Ein höherer Recovery-Zähler allein ist kein Erfolg: Entscheidend sind eine anschließende stabile Spur, ausreichende neue Winkelabdeckung und ein rechtzeitig gültiges Ergebnis. Bei Unterbrechung darf keine Gradkorrektur aus vermischten Generationen erscheinen.

Für horizontale/vertikale Zielorientierung bleiben der vermessene Rollweg einschließlich Erhöhung, markierte Referenzkörper und bekannte Endlagen erforderlich. Physikalische Kalibrierung und SPS-Schreibfreigabe wurden durch diese Softwarekorrekturen nicht ersetzt.
