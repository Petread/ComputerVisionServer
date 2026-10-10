# Analyse Stammausrichtung, Stammoptimierung und 3D-Messung – 14.09.2026

Der gegenwärtige Hauptengpass ist die Verfügbarkeit **zeitlich zusammengehöriger, gültiger Geometrie- und Winkelbeobachtungen während des kurzen Transfers**. Die Segmentierung allein ist nicht der größte gemessene Zeitverbraucher. Daneben bestehen reproduzierbare Fehler beziehungsweise unzureichend abgesicherte Annahmen in der Phasenverfolgung und im räumlichen Scheibenfit. Eine reine Erhöhung der Kamera-FPS oder Lockerung der Qualitätsgrenzen löst diese Probleme nicht.

Diese Analyse verändert keine Runtime, Konfiguration, Kalibrierung oder SPS-Anbindung auf dem Server. Im Repository wurde nur dieser Bericht ergänzt; lokale Diagnoseartefakte liegen im bereits ignorierten `.test-tmp`.

**1. Untersuchungsumfang und Beweisgrenzen**

Untersucht wurden die Spezifikation zum Krümmungsminimum, die Dokumente zur geometrischen Eindrehoptimierung, beide Repository-Vorlagen, deren Mess-Nodes, Phasenbridge, Scheibenfit, Optimierer sowie Kameraübergabe und Parallelisierung im Realtime-Executor. Über die bestehende Runtime-API wurden Kanalzustände, gespeicherte Definitionen und bildlose Live-Diagnosen gelesen.

Aktiver Kanal: `Stammeindrehung inkl. Modell`, Kanal-ID `chn_8db0e33874024d15b65f370efbbcc12c`, Pipeline v41, Version `plv_9de3adfeb71440cdb13804d68240b65c`. Der Kanal `Stammkrümmungserkennung` mit zugewiesener v117 war gestoppt. Seine letzte Definition enthält bereits einen neueren Entwurf; Entwurf und ausgeführte Version dürfen nicht gleichgesetzt werden.

Die dreiminütige Stichprobe reicht vom **14.09.2026, 20:39:46 bis 20:42:46 MESZ**. Das vorhandene `scripts/diagnostics/runtime_live_probe.py` las 267 Antworten mit 228 unterschiedlichen angezeigten Frame-Zuständen, darunter ein Zustand ohne Frame. Es handelt sich um Polling der letzten Anzeige, nicht um einen lückenlosen Mitschnitt aller verarbeiteten Bilder. Die 47 beobachteten Session-IDs sind deshalb weder eine vollständige Produktionsstatistik noch ein Nachweis über 47 unterschiedliche physische Stämme.

Codebefunde beziehen sich auf den aktuellen Repository-Stand. Die Serverdiagnosen bestätigen die entsprechenden Betriebsmodi; die exakte Übereinstimmung aller ausgelieferten Python-Dateien mit diesem Checkout wurde nicht über einen Release-Hash nachgewiesen. Es wurde kein reales Rohvideo mit bekannter Winkelreferenz ausgewertet. Belichtungszeit, Bewegungsunschärfe während des Einrollens und wahre physische Winkel bleiben damit offen.

**Ergänzung des Betreibers zum realen Prozess:** Der Stamm rollt beim Transfer über eine leichte Erhöhung. Der Rollweg liegt somit nicht auf einer durchgehend planen Ebene. Die Höhenänderung gehört zum regulären Messprozess und muss im Beobachtungsmodell berücksichtigt werden. Höhe, Form und Lage der Erhöhung sowie die Gleichmäßigkeit der Auflage entlang des Stammes sind noch nicht vermessen; Kippen, Schlupf oder Nachschwingen sind mögliche Begleitbewegungen, keine durch diese Angabe bereits nachgewiesenen Vorgänge.

**2. Was die beiden Funktionen tatsächlich entscheiden**

Der gemeinsame Messpfad ist:

```text
camera-media → gemeinsamer Frame-Ring → Realtime-Executor
    → Entzerrung → kanonische Ansicht → Segmentierung → aktiver Stamm
        ├─ Messfreigabe → horizontales Krümmungs-/Breitenprofil
        └─ Maskengeometrie → Seed für schnelle Mantelphase

Quellframes → vorhandener schneller Phasenworker → zeitgebundene Phasenwerte
```

Die Vorlage zum **Krümmungsminimum** führt Krümmung und Phase im `stem_curvature_phase_solver` zusammen. Der Solver bestimmt das Minimum einer periodischen Projektion. Die zentrale Zielphase ist im Code `maximum_phase + 90°`, modulo 180°. Eine explizite fachliche Wahl „Bogen horizontal oder vertikal“ ist damit noch nicht als allgemeines, validiertes Zielmodell beschrieben.

Die aktive **geometrische Eindrehoptimierung** ersetzt diesen Entscheidungsteil durch `stem_slice_model → stem_rotation_optimizer`. Sie rekonstruiert elliptische Scheiben und maximiert die kleinste nutzbare symmetrische Geometrie entlang der priorisierten Maschinenachse. Das berücksichtigt auch Ovalität, Scheibenversatz und Engstellen.

**Die priorisierte Geometrieachse ist nicht automatisch die gewünschte Bogenebene.** Im aktiven Kanal bedeutet `VORSCHNITT_KRITISCH` die Optimierungsachse `VERTICAL_Z`. Daraus folgt nicht allgemein „Krümmung soll vertikal stehen“. Schon ein gerader ovaler Stamm kann für den Geometrieoptimierer eine bevorzugte Drehung haben, obwohl keine eindeutige Bogenrichtung existiert. Für das hier formulierte Ziel sollte die gewünschte Bogenebene ausdrücklich im bestehenden Entscheidungsvertrag abgebildet und mit analytischen Beispielen geprüft werden. Zwei gleichzeitig maßgebliche Winkelausgaben wären ungeeignet.

Quellen im Repository: `pipeline_store.py:6151,6471`, `stem_curvature_phase_solver.py:299`, `stem_rotation_optimizer.py`, `stem_rotation_geometry.py`, `stem_rotation_types.py`.

**3. Gemessener Zustand**

| Kennzahl | Befund der Stichprobe |
|---|---|
| Eingang vor dem Executor | Quellsequenz 8555 → 13943 über 179,597 s: etwa **30,00 Sequenzschritte/s** |
| Vom Executor gemeldete Quellrate | typischer Wert/Median rund **19,6 FPS** |
| Vollständig verarbeitete Rate | Median rund **5,2 FPS**, Minimum 3,6; kurzfristig bis 25,7 bei wechselnder Arbeitslast |
| Angezeigte Verwerfungsrate | Median rund **71,6 %**; berücksichtigt nicht zwingend bereits überschriebenen Ringinhalt |
| Angezeigte Einzellatenz | Median rund **374 ms**, Maximum **1013 ms** |
| Beobachtete Sessions | 47; davon 18 mit gesehenem `result_ready`, **keine mit `result_valid`** |
| Größte Beobachtungszahl einer Station je Session | Median **3**, Maximum 9; nur 3 Sessions erreichten irgendwo mindestens 7 |
| Größte Zahl unterschiedlicher Phasen einer Station je Session | Median **1**, Maximum 3 |
| Größte erfasste Phasenspanne einer Station | **36,32°**, keine Session erreichte die aktiven 60° |

Die annähernd 30 Hz aus dem Quellzähler sind eine starke Indikation für kontinuierliche Bilder vor der Übernahme durch den Executor. Sie sind keine unabhängige optische Bestätigung von 30 unterschiedlichen, scharfen Kamerabildern pro Sekunde.

| Node | Median | 95. Perzentil | Maximum |
|---|---:|---:|---:|
| Horizontale Krümmung | 152,5 ms | 307,5 ms | 448,6 ms |
| Maskengeometrie, bereits `phase_tracking_fast` | 106,3 ms | 288,1 ms | 501,8 ms |
| Mantelphase-Node | 34,7 ms | 234,8 ms | 377,2 ms |
| Segmentierung einschließlich Aufruf | 40,1 ms | 105,8 ms | 334,5 ms |
| Messfreigabe | 11,2 ms | 32,5 ms | 83,3 ms |
| Scheibenmodell | 10,9 ms | 36,4 ms | 102,2 ms |
| Aktiver Stamm | 9,0 ms | 37,0 ms | 89,6 ms |
| Optimierer | 0,48 ms | 0,97 ms | 7,70 ms |

Diese Zeiten enthalten Warte-/Scheduling-Effekte innerhalb des jeweiligen Aufrufs und stammen aus wechselnden Szenen. Die parallelen Zweige dürfen nicht einfach addiert werden. Die sehr kurzen Optimiererzeiten entstehen überwiegend bei ungültigen Eingangsmodellen und belegen keine schnelle vollständige Winkelsuche an einem gültigen Stamm.

In einem separat gesicherten Livezustand erreichte der schnelle Phasenpfad 12,55 FPS bei 25 FPS Soll und 97,5 ms Latenz. Er war also bereits eingeschaltet. Ebenso aktiv waren `pipeline_max_inflight=2`, geordnete Überlappung der zustandsbehafteten Nodes, schnelle Maskengeometrie und 50 ms Mindestabstand der Krümmungsbeobachtungen.

Unter den 227 Zuständen mit eigentlichem Frame waren 91 Phasenzustände `INCREMENTAL`, 79 `LOST`, 47 `HOLDING` und 10 `INITIALIZING`. `PHASE_SEED_STALE` erschien 79-mal, `PHASE_AFFINE_PROPAGATION_FAILED` 23-mal und `PHASE_FEATURE_COUNT_INSUFFICIENT` 22-mal. Die Messfreigabe akzeptierte 158 Zustände und verwarf 69; 27 Verwerfungen enthielten `STEM_CAPTURE_TRACK_NOT_STABLE`.

Die Modellanzeige enthält unter anderem `FRAME_SEQUENCE_GAP_TOO_LARGE`, `MATERIAL_X_REGISTRATION_STEP_IMPLAUSIBLE`, `CURVATURE_INVALID` und `CALIBRATED_PHASE_INVALID`. Diese Reason-Codes werden teilweise als Sessionhistorie weitergereicht. Ihre Häufigkeit in Antworten ist **keine Anzahl unabhängiger Fehlerereignisse oder Resets**. Da der Krümmungs-Debugoverlay ausgeschaltet war, enthält die bildlose Probe keine vollständige Einzelbegründung der Krümmungsablehnungen.

**4. Priorität 0: Der schnelle Phasenworker kann die Bildübernahme blockieren**

`HighRateStemPhaseBridge.process_frame()` hält dieselbe `RLock` während Seed-Verarbeitung, optischem Fluss und vollständigem Replay. `record_source_frame()`, `publish_seed()`, `snapshot()` und `metrics()` benötigen ebenfalls diese Sperre. Der Quellenlesethread ruft `high_rate_phase_runtime.submit(packet)` auf; schon dessen `record_source_frame()` kann auf diese Arbeit warten. Erst danach wird das Bild in den Hauptpipeline-Puffer gelegt.

Damit ist die Hochfrequenzverarbeitung rechnerisch in einem Thread ausgelagert, aber der Bilderfassungspfad weiterhin an ihre Laufzeit gekoppelt. Währenddessen liest `SharedMemoryFrameSource` beim nächsten Zugriff nur das neueste Bild. Zwischenstände können bereits im Frame-Ring verloren gehen. Das passt zur Differenz zwischen ungefähr 30 Hz Quellsequenz, rund 20 FPS Übernahme und nur etwa 13 FPS im Phasenpfad. Die genaue Aufteilung der Verluste erfordert zusätzliche Laufzeitmessung.

Empfehlung: Der Worker behält alleinigen Besitz seines Trackingzustands. Eine kurze Sperre schützt nur den Austausch unveränderlicher Seeds, Snapshots und Zähler. OpenCV, Replays und Fits laufen außerhalb der gemeinsam benötigten Sperre. Die bestehenden Frame-Ring- und Workermechanismen bleiben erhalten. Für den begrenzten Rollvorgang ist außerdem eine geordnete Übernahme aus dem vorhandenen Ring sinnvoll, mit expliziter Überlauferkennung und Zeitbudget.

Code: `high_rate_phase.py:232,256,292,321,364`; `live.py:591,1565`; `sources.py:113`; `frame_transport.py:244`.

**5. Priorität 0: Verspätete Seeds zählen Translation beim Replay doppelt**

Bei einem Seed zu einem älteren Frame setzt `_apply_seed()` die Rohphase aus `_phase_history` auf dessen historischen Stand zurück. Die kumulierte X-/Y-Translation übernimmt es dagegen aus dem neueren `previous_state`. Danach spielt `process_frame()` die Bilder nach dem Seed erneut ab; `_advance()` addiert deren Translation nochmals.

Isoliert reproduziert mit der produktiven Seed-/Replay-Steuerung und einer deterministischen Bewegung von 10 Pixeln je Schritt:

```text
Frame 1: 0 px → Frame 2: 10 px → Frame 3: 20 px
Jetzt verspäteter Seed von Frame 2; danach Frame 4.
Erwartete kumulierte Translation: 30 px
Ergebnis des vorhandenen Codes: 40 px
Rohphase dagegen korrekt: 0,3
```

Die Bildanalyse wurde in diesem Test durch deterministische Hilfsfunktionen ersetzt, nicht die Seed-/Replaylogik. Das ist ein reproduzierbarer Zustandsfehler, kein Nachweis seiner numerischen Größe an der echten Kamera. Der Fehler kann jedoch unmittelbar die 75-mm-Schrittgrenze der Material-X-Registrierung verletzen.

Empfehlung: Phase, Translation, Geometrie und Gültigkeit gemeinsam pro Zeitstempel/Sequenz speichern und beim Replay atomar auf denselben Ausgangszustand setzen. Ein Zwischenframe darf je Spur genau einmal integriert werden. Wenn der erforderliche historische Zustand fehlt, ist ein expliziter Spurbruch nötig; der aktuelle Bewegungsstand darf nicht stillschweigend auf ein altes Bild gesetzt werden.

Code: `high_rate_phase.py:390–475,477–613`.

**6. Priorität 0: Querbewegung kann als räumlicher Scheibenversatz ausgegeben werden**

Der Nutzerprozess enthält ausdrücklich eine ausgeprägte Querbewegung beim Einrollen. `_interpolate_center()` übernimmt jedoch absolute `y_center_mm` in den Scheibenfit. Die Materialregistrierung korrigiert X, nicht die gemeinsame Quertranslation. Der Fit setzt voraus:

```text
y(t) = konstanter_offset + cy*cos(phase(t)) - cz*sin(phase(t))
```

Ein konstanter Offset entfernt keine zeitabhängige Querbewegung. Eine mit der Drehphase korrelierte Bewegung kann mathematisch vollständig in `cy/cz` eingehen.

Durch die bestätigte Erhöhung kommt eine Änderung der Stammhöhe hinzu. In der schrägen Kamera kann auch diese Bewegung die gemessenen Bildkoordinaten und damit die auf die Referenzebene zurückgerechneten `y_center_mm` verändern. Die in diesem Abschnitt empfohlene Bewegungskompensation muss deshalb Quer- **und Höhenbewegung** berücksichtigen; alleinige Korrektur einer Verschiebung in Maschinen-Y genügt nicht allgemein. Der folgende Test isoliert weiterhin ausschließlich Quertranslation und quantifiziert nicht den zusätzlichen Höhenfehler.

Isolierter Aufruf des produktiven `fit_stem_slice()` mit einem geraden, runden Zylinder, konstant 300 mm Breite, 13 Phasen über 120° und reiner gemeinsamer Quertranslation `300 + 40*sin(phase)`:

- ausgegebener räumlicher Versatz `cz = −40 mm`;
- Modell gültig, Qualität etwa **0,935**;
- Fitresiduum praktisch null.

Das Verfahren kann diesen Eingabefall nicht als reine Starrkörperbewegung erkennen. Bei identischer Bewegung aller Längsstationen entsteht keine Biegung; der Geometrieoptimierer erhält dennoch gegenüber seiner Drehachse versetzte Scheiben. Ein kleiner Residualfehler schützt hier nicht vor einer falschen Interpretation.

Empfehlung: Gemeinsame Starrkörperbewegung und stammfeste Form im bestehenden Messpfad gemeinsam schätzen. Für die Bogenorientierung sind Profile relativ zu einer konsistenten stammfesten Sehne/Achse geeignet. Für die zusätzliche Einschnittoptimierung muss separat geklärt werden, wo die reale spätere Dreh-/Zentrierachse liegt. Absolute Maschinen-Y-Werte und reine Bogenresiduen dürfen nicht ohne definierte Achsbindung gegeneinander ausgetauscht werden.

Code: `nodes/stem_slice_model.py:391–459,769–776`; `stem_slice_measurement.py:241–315,394–405`.

**7. Priorität 1: Der zylindrische Phasenfit prüft seine Eindeutigkeit nicht**

Der V2-Schätzer regressiert Querbewegung und Rotation gemeinsam über `[1, sqrt(0.25-u²)]`. Er prüft die Spannweite von `u`, aber weder Rang noch Kondition dieser Designmatrix. Merkmale auf zwei symmetrischen Mantelstreifen können eine große Spannweite besitzen und trotzdem dieselbe Rotationsbasis liefern. Dann sind Quertranslation und Rotation nicht unterscheidbar.

Reproduziert mit 24 fehlerfreien Korrespondenzen auf `u=±0,35`:

- Matrixrang 1 statt 2;
- reine Quertranslation erzeugt eine nichtnullige Rohrotation;
- bei wahrer linearisierter Rohrotation 0,2 liefert der Code nur **0,0226**;
- trotzdem `valid=true` und **Konfidenz 1,0**.

Dies ist ein mathematischer Grenzfall, kein Nachweis derselben Merkmalsverteilung im Produktionsvideo. Er zeigt aber, dass die derzeitige Konfidenz eine grundlegende Unsicherheit nicht erfasst.

Empfehlung: SVD-/Konditionsprüfung, ausreichende Variation der Rotationsbasis, Merkmale über mehrere Quer- und Längsbänder und eine aus dem Fit abgeleitete Winkelunsicherheit. Das jetzige Kleinschrittmodell wird bei großen Sprüngen zudem ungenauer. Große Sprünge sollten über eine nichtlineare zylindrische Abbildung oder belastbaren Re-Lock behandelt werden, nicht über höhere zulässige Schrittgrenzen.

Code: `stem_surface_phase.py:650–849`. Die Einschränkungen des verwendeten lokalen LK-Trackings und die Bedeutung wiederholter Merkmalsprüfung sind auch in der [OpenCV-Dokumentation zum optischen Fluss](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html) beschrieben.

**8. Priorität 1: Historische Phase und Geometrie werden nicht zuverlässig gemeinsam bereitgestellt**

Die Oberfläche-Node veröffentlicht einen Seed und liest unmittelbar anschließend einen Snapshot mit höchstens der Geometriesequenz. Der Worker verarbeitet den Seed asynchron. Während eines Replays werden Phasen in `_phase_history` geschrieben; die vollständigen Snapshots werden dagegen hauptsächlich am Ende von `process_frame()` gespeichert. Ein passend verarbeiteter Zwischenstand muss daher noch nicht für die anfragende Geometriesequenz verfügbar sein. Fehlt ein passender Kandidat vollständig, bleibt in `snapshot()` sogar zunächst der aktuelle Snapshot als Rückfallwert bestehen.

Der Builder lehnt einen Abstand von mehr als zwei Quellsequenzen ab. Das schützt vor groben Fehlpaarungen, kostet aber Beobachtungen. Die vorhandene Warnung `FRAME_SEQUENCE_GAP_TOO_LARGE` passt dazu; die genaue Ursache je Frame lässt sich aus dem Overlay-Polling nicht vollständig zuordnen.

Empfehlung: Einen begrenzten Join-Puffer im bestehenden Runtimepfad verwenden: Geometrie von Sequenz N wartet bis zu einer klaren Deadline auf die rekonstruierte Phase von N. Jeder replayte Zwischenstand bekommt einen vollständigen Snapshot. Interpolation ist nur zwischen gültigen benachbarten Zuständen derselben Spur mit Zeit-/Unsicherheitsgrenze zulässig. Kein Zählen fortgeschriebener Masken oder mehrfach genutzter Bilder als neue Geometriemessungen.

Zusätzlich fällt auf, dass ein gültiger `frozen_payload` früh zurückgegeben wird, bevor neuere Mantelphasen verarbeitet werden. Geometrie-Freeze und aktuelle Endlage sollten getrennt bleiben, falls nach Beginn des Längstransports noch ein Nachrollen bis zum eigentlichen Entscheidungszeitpunkt möglich ist.

Code: `nodes/stem_surface_phase.py:364–390`; `high_rate_phase.py:292–347`; `nodes/stem_slice_model.py:101–107,330–334`.

**9. Priorität 1: Das verfügbare Zeitfenster passt nicht zu Beobachtungszahl und Startfreigabe**

Der aktive Builder verlangt weiterhin sieben gültige Beobachtungen pro Scheibe, obwohl Phasenanzahl und Spanne bereits von den Vorlagenwerten 6/90° auf 3/60° abgesenkt wurden. Bei 5,2 FPS kommen in 0,5 Sekunden durchschnittlich **2,6 Pipelineframes** an. Drei stabile Trackframes als Voraussetzung verbrauchen bei dieser Rate bereits ungefähr 0,38 Sekunden zwischen erster und dritter Beobachtung.

Die reine Größenordnung lautet `N ≈ FPS × 0,5 s × Annahmequote`. Sie ersetzt keine Auswertung tatsächlicher Aufnahmezeitpunkte:

| Durchsatz | Frames im Rollfenster, vor Qualitätsverlusten |
|---:|---:|
| 5 FPS | etwa 2–3 |
| 10 FPS | etwa 5 |
| 20 FPS | etwa 10 |
| 30 FPS | etwa 15 |
| 60 FPS | etwa 30 |

Ein Durchsatz von mindestens ungefähr 14 vollständig gültigen Beobachtungen/s ist schon ohne Reserve nötig, um durchschnittlich sieben Beobachtungen in 0,5 s zu erhalten. Als erstes Entwicklungsziel sind etwa 20–30 echte Geometriebeobachtungen/s und eine Phasenspur nahe der Quellrate sinnvoll. Ob das genügt, hängt von realer Rollgeschwindigkeit, Winkelabdeckung, Schärfe und erforderlicher Winkelgenauigkeit ab.

Auch die Basistemplate hat eine Zeitbudget-Unstimmigkeit: Das Krümmungsminimum setzt 100 ms Mindestabstand und der Solver verlangt standardmäßig sieben Beobachtungen. Zwischen erster und siebter Beobachtung liegen mindestens 600 ms. Das kann nicht vollständig in ein 500-ms-Rollfenster passen. Vor-/Nachframes können die Anzahl erhöhen, liefern aber nicht automatisch neue Rollstellungen. Die geometrische Vorlage überschreibt den Abstand bereits auf 50 ms.

Empfehlung: Den bereits sichtbaren wartenden Stamm vor dem Einrollen stabil zuordnen und seine Oberflächenmerkmale vorbereiten. Die eigentliche Messsammlung beginnt mit physischer Transferbewegung. Eine sichere frühe Identität muss aus Reihenfolge und Prozesszonen entstehen; `prefer_longitudinal_on_acquisition=true` ist bei gleichzeitig sichtbarem vorherigem Stamm ausdrücklich zu prüfen. Die Anzahl realer Stämme lässt sich aus den wechselnden Track-IDs allein nicht ableiten.

Code: `pipeline_store.py:6254,6483`; `stem_curvature_phase_solver.py:65`; `nodes/stem_measurement_eligibility.py:105–110`; `nodes/stem_slice_model.py:250–253`.

**10. Priorität 1: CPU-Arbeit gezielt reduzieren, Parallelität messen**

Die schnelle Maskengeometrie nutzt bereits ein orientiertes Polygon-ROI, reduzierte Querprofile und keine Achsenverfeinerung. Trotzdem bleiben Punktaufbereitung, PCA, Sortierungen und Querprofilanalyse. Die horizontale Krümmung rastert/transformiert Masken, bearbeitet Zusammenhangskomponenten und ermittelt bei 96 Stationen separat Pixelgrenzen. Mehrere Nodes erzeugen erneut Masken oder prüfen dieselben Kalibrierungsprofile. Im Phasenworker wird der Laplace-Schärfewert über das gesamte reduzierte Bild berechnet.

Verbesserungen innerhalb des vorhandenen Systems:

- Pro echtem Frame ausgewählte Maske, Basisachse und geeignete Zwischenresultate gemeinsam wiederverwenden; die unabhängigen Qualitätsanforderungen der Messzweige erhalten.
- Kalibrierungsvalidierung und feste Masken/Transformationen anhand unveränderlicher Kalibrierungs-Hashes cachen.
- Krümmungsgrenzen in einem vektorisierten Durchlauf beziehungsweise im relevanten ROI bestimmen; Gleichwertigkeit an problematischen Masken prüfen.
- Phasentracking auf ausreichend breite aktive Mantelbänder begrenzen; nicht unkontrolliert Breite reduzieren, da gerade die Querstreuung für die Winkelidentifikation nötig ist.
- Native OpenCV-/BLAS-Threadzahlen zusammen mit ein/zwei Pipelineframes unter realer Serverlast vergleichen. Der vorhandene WSL-Wrapper begrenzt diese Threads explizit für `inference_gateway.*`, nicht allgemein für den Realtime-Executor. Überbelegung ist deshalb eine plausible, noch nicht gemessene Ursache. OpenCV dokumentiert die entsprechende [Threadsteuerung](https://docs.opencv.org/4.13.0/dc/ddf/tutorial_how_to_use_OpenCV_parallel_for_new.html).

Zuerst die Sperren und tatsächlichen Hotspots beheben. Mehr Inflight-Frames können sonst nur Warteschlangen, Speicherdruck und veraltete Seeds vermehren. Eine weitere GPU oder ein anderes Segmentierungsmodell ist durch diese Messung nicht als erster Schritt begründet.

Code: `nodes/stem_orientation.py:185`; `stem_geometry.py:255,698,892,906`; `stem_horizontal_curvature.py:162,342`; `high_rate_phase.py:963`; `deployment/wsl/apply-release.sh:1226`.

**11. Physikalische Grenze: Projektionsminimum ist nicht automatisch vertikaler Bogen**

Die aktive intrinsische Kalibrierung ist `IDENTITY_PASSTHROUGH` mit null tatsächlichen Kalibrierbeobachtungen. Das ist ein dokumentiertes Testprofil, keine gemessene Objektiventzerrung. Die Prozesshomographie wurde aus vier manuell definierten XY-Punkten erzeugt. Ihr winziger Fehler auf diesen vier Stützpunkten belegt nicht die metrische Genauigkeit auf realer Stammhöhe.

Eine Homographie bildet eine Referenzebene ab. Punkte über dieser Ebene folgen einer anderen Projektion. Das ist die grundlegende Voraussetzung der [OpenCV-Homographie-Dokumentation](https://docs.opencv.org/4.13.0/d9/dab/tutorial_homography.html). Bei einer schrägen Kamera kann die gemessene laterale Abweichung lokal vereinfacht `p = a·Y + b·Z` sein. Eine ebene XY-Entzerrung entfernt den Z-Anteil nicht automatisch. Das Minimum von `|p|` muss daher nicht bei einer physisch vertikalen Bogenebene liegen.

Die gespeicherte Gradkalibrierung bindet die Mantelphase an Referenzwinkel. Sie beweist für sich allein nicht, welche Phase des Krümmungssignals „horizontal“ oder „vertikal“ bedeutet. Der aktive Fit verwendet vier Referenzbeobachtungen über etwa 1,72 Sekunden mit Faktor −31,93 Grad/Rohphase und angegebenem mittlerem Fehler 2,00°. Das Profil ist softwareseitig gültig, aber eine erneute dynamische Prüfung am realen 0,5-s-Prozess und über mehrere Durchmesser/Positionen bleibt nötig. Ein Wechsel des Schätzverfahrens erfordert eine passend erfasste Referenz; aus gespeicherten Metadaten allein lässt sich die Entstehung der vier Rohphasen nicht nachprüfen.

Für das Nutzerziel sollte ein gebogener Referenzkörper mit bekannter horizontaler und vertikaler Bogenebene über mehrere Rollstellungen geprüft werden. Darauf aufbauend ist entweder ein kalibrierter Projektionsversatz im bestehenden Bogenverfahren oder ein geometrisch begründetes Beobachtungsmodell mit Kamerapose und Referenzhöhe nötig. „3D“ bedeutet hier ein aus mehreren Projektionen gefittetes Modell unter Annahmen, keine direkte Tiefenmessung. Die physische Richtung braucht weiterhin den versionierten Blickstandpunkt.

Im Screenshot sind mehrere Stämme und begrenzte sichtbare Endbereiche vorhanden. Eine Aussage über den Gesamtbogen setzt einen ausreichend langen, zwischen Frames wirklich gemeinsamen Materialabschnitt voraus. Ein guter Fit auf einem wechselnden Reststück ersetzt diese Voraussetzung nicht.

**Ergänzung: Rollen über die Erhöhung.** Während des Überrollens ändern sich Position und Höhe der Stammachse; bei ungleichmäßiger Auflage kann zusätzlich ihre Neigung wechseln. Das Kameramodell bildet räumliche Lageänderungen perspektivisch auf das Bild ab. Daraus folgt für diesen Aufbau, dass Bildposition, sichtbare Breite und Oberflächenbewegung sich auch ohne entsprechende zusätzliche Eigenrotation ändern können. Das zugrunde liegende Projektionsmodell ist in der [OpenCV-Dokumentation zur Kamerakalibrierung](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html) beschrieben; die Größe dieser Effekte an der Anlage ist noch zu bestimmen.

Die Interpretation der Bildfolge muss daher drei Anteile auseinanderhalten: Transport der gesamten Stammachse einschließlich Höhe, mögliche Änderung ihrer räumlichen Richtung und Eigenrotation des Stammes um seine Längsachse. Der tatsächliche Bogen wird im stammfesten Bezugssystem bestimmt. Ein Wechsel der scheinbaren Bildbreite darf nicht unmittelbar als Ovalität gelten, eine Bewegung des gesamten Stammes nicht als Biegung oder Mantelrotation. Eine starre Translation allein macht eine ideale gerade 3D-Linie unter idealer perspektivischer Projektion nicht krumm; problematisch sind insbesondere wechselnde Maßstäbe, Sichtabschnitte und die Zuordnung absoluter Profilwerte zum Form-/Rotationsmodell.

Für die Weiterentwicklung ist die vermessene Förderkontur eine zusätzliche geometrische Randbedingung im vorhandenen Prozesskalibrierungspfad. Sie legt die reale Bahn der Stammachse jedoch nicht allein fest: Durchmesser, Form und Kontaktstellen spielen ebenfalls eine Rolle. Eine einzige feste Referenzhöhe oder ein konstanter Winkelkorrekturwert darf daher nur nach entsprechender Fehlerprüfung verwendet werden. Ebenso darf der Winkel nicht aus dem quer zurückgelegten Bildweg über eine angenommene ebene Rollbeziehung abgeleitet werden; die gültige visuelle Rotationsspur bleibt erforderlich.

Zur Prüfung gehören ein gerader markierter Referenzkörper und ein Körper mit bekanntem Bogen, jeweils über **denselben realen Weg einschließlich Erhöhung**, mit mehreren Durchmessern und bekannten Endorientierungen. Getrennt auszuwerten sind Aufstieg, Übergang über die Erhöhung und anschließendes Aufliegen. Ein pauschales Verwerfen aller Frames an der Erhöhung würde möglicherweise gerade den größten Teil der verfügbaren Rotation entfernen. Stattdessen müssen diese Frames entweder mit ausreichender geometrischer Sicherheit ausgewertet oder mit ihrer konkreten Unsicherheit abgelehnt werden. Die gemessenen FPS-Verluste und reproduzierten Codefehler bleiben von dieser Prozesserweiterung unberührt.

**12. Ergebniszeitpunkt, Diagnose und konkret empfohlene Reihenfolge**

Die aktive Konfiguration setzt Freeze- und Decision-Gate auf dieselbe obere Bildkante; `decision_margin_mm=0`. Das ist keine nachgewiesene Zeitreserve vor dem Walzenpaar. Ein ausschließlich durch Bildaustritt ausgelöstes Ergebnis kann bei mehreren hundert Millisekunden Verarbeitung zu spät sein. Die Freigabe sollte auf stabilem Aufliegen beziehungsweise bestätigt beginnendem Längstransport beruhen; das räumliche Gate begrenzt den spätesten zulässigen Zeitpunkt. Die notwendige Reserve ergibt sich aus gemessener Fördergeschwindigkeit und Gesamtlatenz.

Die kanalspezifische Result-History ist deaktiviert und `include_raw_observations=false`. Ein zusätzlicher lesender Abruf der Result-Service-Historie wurde mit `authentication_failed` abgewiesen. Daraus folgt nicht, dass keine Ergebnisse irgendwo gespeichert werden; diese Historie war mit dem bereitgestellten Zugang nicht auswertbar. Die vorhandene Liveprobe hängt zudem für manche Fachwerte an eingeschalteten Overlays. Technische Messdiagnosen sollten über die bestehenden Payloads unabhängig von Zeichenoverlays verfügbar sein.

Einige gespeicherte Werte erschweren die Nachvollziehbarkeit: Builder-Raster 100 mm gegenüber Optimierer-/Anzeigekonfiguration 50 mm sowie ältere Parameter `maximum_axial_delta_mm`, `maximum_cumulative_axial_motion_mm` und `maximum_transverse_delta_mm`, die der aktuelle Builder nicht auswertet. Effektive Parameter sollten eindeutig aus der ausführenden Node stammen; eine Änderung eines ungenutzten Wertes verbessert nichts.

Empfohlene Umsetzung in dieser Reihenfolge:

1. **Datenintegrität:** Replay-Translation korrigieren, vollständige historische Snapshots bereitstellen, zeitlich exakte Paarung von Phase und Geometrie, kurze Bridge-Sperren. Gegen verspätete Seeds, Bildlücken, Sessionwechsel und reine Translation testen.
2. **Messdurchsatz:** Native Threadzahlen und reale Subschrittzeiten instrumentieren; Masken-/Geometriearbeit reduzieren. Übernommene und übersprungene Ringsequenzen separat zählen. Einen vollständigen 0,5-s-Burst im vorhandenen Capture-/Replaypfad sichern.
3. **Prozesszustand:** Wartenden Stamm früh stabil zuordnen, Transfer über die Erhöhung und abschließendes Aufliegen verlässlich erkennen, Mess-Freeze und Endphase trennen. Nachbarstamm und vorige Session dürfen keine Ausgangsdaten ersetzen.
4. **Winkel-/Formmodell:** Gemeinsame Quer-/Höhenbewegung und mögliche Achsneigung von Biegung und Eigenrotation trennen, Konditions-/Unsicherheitsprüfung des Mantelfits ergänzen, reale horizontale/vertikale Zieldefinition kalibrieren. Die Erhöhung als vermessene Randbedingung berücksichtigen. Die geometrische Einschnittoptimierung als eigene fachliche Zielfunktion innerhalb der bestehenden Pipeline behandeln.
5. **Abnahme am Zielserver:** Pro Stamm echte Quellframes, gültige Masken, gültige Phasen, gepaarte Beobachtungen je Materialstation, Winkelspanne, größte Zeitlücke, Winkelunsicherheit und Zeitpunkt der fertigen Entscheidung zählen. Wiederholmessungen mit markierten geraden und gebogenen Referenzkörpern über den realen 0,5-s-Transfer einschließlich Erhöhung durchführen. Erst danach Grenzwerte anhand gemessener Fehler festlegen.

Eine höhere Kamerarate und kürzere Belichtung können anschließend zusätzliche Reserve liefern. Aus einem Standbild lässt sich die aktuelle Bewegungsunschärfe nicht bestimmen. Eine pauschale Empfehlung einer bestimmten Kamera oder Belichtungszeit wäre hier unbelegt. Unzureichende Winkelabdeckung bleibt auch bei hoher FPS möglich; in diesem Fall muss die Entscheidung ungültig bleiben oder der beobachtbare Abschnitt/Prozess geändert werden.

**13. Verifikation und lokale Artefakte**

- **56 vorhandene Mathematiktests bestanden**: `test_stem_surface_phase.py`, `test_stem_slice_measurement.py`, `test_stem_curvature_phase_solver.py`, `test_stem_rotation_optimizer.py`.
- Ausführung isoliert mit `--confcutdir=tests/unit/backend` und direktem Laden des `cv_runtime`-Untermodulpfads. Der normale Paket-Gesamtimport benötigt lokal nicht installiertes OpenCV. Das ist keine vollständige Backend-, Runtime- oder Kamera-Testfreigabe.
- Drei Arten von Schwachstellen mit produktiven numerischen Funktionen beziehungsweise produktiver Replay-Steuerung reproduziert. Vier konkrete Ausgaben liegen im Prüfartefakt vor, da der Rangdefekt sowohl mit Translation als auch mit Rotation untersucht wurde.
- Keine Produktionscodeänderung, kein Kanalstart/-stopp, keine Kalibrierungsänderung und kein SPS-Schreibzugriff.

Lokale, nicht versionierte Nachweise:

```text
.test-tmp/stem-audit-20260914-live.json
.test-tmp/stem-audit-20260914/evidence-summary.json
.test-tmp/stem-audit-20260914/check_findings.py
.test-tmp/stem-audit-20260914/check_findings_results.json
.test-tmp/stem-audit-20260914/live-initial.json
```

Der API-Schlüssel ist nicht in Bericht oder Diagnoseartefakten gespeichert. Quell-URLs und Zugangsdaten wurden aus den gespeicherten Kanaldetails entfernt.
