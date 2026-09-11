# Pipeline der Stamm-Eindrehoptimierung konfigurieren

Stand: 2026-09-01

## Zweck und Sicherheitsgrenze

Diese Anleitung beschreibt die erstmalige Einrichtung der vorhandenen Runtime-Vorlage
`Stammausrichtung – geometrische Eindrehoptimierung` auf dem Zielserver.

Die Funktion arbeitet aktuell ausschließlich als **Shadow-Empfehlung**:

- Die Kamera und die vorhandene Segmentierung liefern die Messdaten.
- Der Bediener wählt die Priorität im Frontend.
- Das Ergebnis ist eine relative Drehung in Grad und ein Geometriegewinn in Millimetern.
- Es gibt keine SPS-Abhängigkeit und keinen SPS-Schreibzugriff.
- Ein ungültiges Modell darf niemals als gültige Drehempfehlung verwendet werden.

Die unten genannten Startwerte sind Inbetriebnahmewerte. Sie sind noch keine fachlich
validierten Produktionswerte. Die Anwendung wird auf dem Zielserver konfiguriert; auf dem
Entwicklungs-PC muss sie dafür nicht gestartet werden.

## 1. Voraussetzungen

Vor dem Anlegen der Pipeline müssen vorhanden sein:

1. Das korrigierte Update wurde auf dem Zielserver erfolgreich installiert. Insbesondere muss
   die Datenbankmigration `20260820_0036` ohne Fehler abgeschlossen sein.
2. Der betreffende Kamerakanal existiert und liefert ein Livebild in der vorgesehenen
   Produktionsauflösung.
3. Für einen Produktionsversuch ist eine intrinsische Kamerakalibrierung für exakt diese
   Auflösung vorhanden. Im aktuellen Testaufbau darf vorübergehend das ausdrücklich als
   Testprofil gekennzeichnete `IDENTITY_PASSTHROUGH` verwendet werden; Millimeterwerte sind
   dann noch nicht produktiv freigegeben.
4. Ein freigegebenes Segmentierungsmodell für Stammkonturen ist als **Deployed model** aktiv.
5. Ein markierter Teststamm oder Referenzzylinder steht für die Winkelkalibrierung bereit.
6. Die realen mechanischen positiven und negativen Drehgrenzen sind bekannt.
7. Betrachtungsstandpunkt und positive Drehrichtung sind an der Maschine eindeutig festgelegt.

Sind Punkt 5, 6 oder 7 noch nicht erfüllt, darf die Pipeline bereits aufgebaut und geprüft
werden, aber noch keine Gradzahl als Maschinenanweisung verwendet werden.

## 2. Pipeline aus der Vorlage anlegen

Im Admin-Web:

1. **Kanäle** öffnen.
2. Den Produktions-Kamerakanal auswählen.
3. Im Bereich der kanalspezifischen Runtime-Pipeline **Aus einer Vorlage erstellen** wählen.
4. Die Vorlage **Stammausrichtung – geometrische Eindrehoptimierung** auswählen.
5. Die vorhandene Verdrahtung beibehalten.
6. Die nachfolgenden Pflichtangaben von links nach rechts konfigurieren.
7. Definition speichern, validieren und veröffentlichen.

Die vorgesehene Kette lautet vereinfacht:

```text
Kamera -> Kameraentzerrung -> kanonische Prozessansicht -> Segmentierung
       -> Stamm-Tracker -> Messfreigabe -> Krümmung + Mantelphase
       -> Scheibenmodell -> Prioritätsadapter -> Drehoptimierer -> Ergebnis/History
```

Keine zusätzliche SPS-, Kamera-, Ergebnis- oder Optimierer-Node ergänzen. Insbesondere darf
der ältere Krümmungs-/Phasen-Solver nicht parallel dieselbe Entscheidung erzeugen.

## 3. Diese Angaben müssen standortbezogen eingestellt werden

Die folgenden Werte dürfen nicht ungeprüft aus der Vorlage übernommen werden:

| Node/Bereich | Pflichtangabe | Was einzustellen ist |
|---|---|---|
| Kamerakanal | Quelle und Auflösung | Richtigen Produktionskanal verwenden. Die Laufzeitauflösung muss exakt zur Kamerakalibrierung passen. |
| `Camera undistort` | `calibration` | Für Produktion ein gültiges intrinsisches Kalibrierprofil der tatsächlich montierten Kamera auswählen und `resolution_policy=exact` belassen. `IDENTITY_PASSTHROUGH` ist nur für den aktuellen Testaufbau/Trackingtest zulässig. |
| `Canonical image view + X/Y calibration` | kanonische Ansicht | Die vier Ecken des realen Messkorridors im entzerrten Bild festlegen. Nur dieser Ausschnitt wird für Modell und Messung verwendet. |
| `Canonical image view + X/Y calibration` | Prozesskalibrierung | Maschinenursprung sowie `x` in Förderrichtung und `y` horizontal/quer einmessen. `z` ist vertikal; die Drehung erfolgt um `x`. |
| Prozesskalibrierung | Betrachtungsstandpunkt | Den tatsächlich verwendeten Kamerablick dokumentieren und auswählen. |
| Prozesskalibrierung | positive Rotation | `CLOCKWISE` oder `COUNTER_CLOCKWISE` entsprechend einer realen Maschinenprüfung auswählen. Das Vorzeichen niemals aus dem Bild raten. |
| `Segmentation` | `Deployed model` | Das aktive Stamm-Segmentierungsmodell auswählen, das zur selben kanonischen Ansicht gehört. Ohne Modellbindung kann die Pipeline nicht veröffentlicht werden. |
| `Active stem tracker` | `class_filter` | Leer lassen, wenn das Modell ausschließlich Stammkonturen liefert. Andernfalls exakt die Stammklasse eintragen. |
| `Measurement eligibility` | `crowd_boundary` | Die Grenze auf die Seite legen, an der ein wartender oder nachfolgender Stamm in den Messbereich hineinragen kann. |
| `Stem surface phase` | `surface_phase_calibration` | Mit dem Assistenten eine gültige Winkelkalibrierung aufnehmen und anwenden; Ablauf siehe Abschnitt 4. |
| `Stem rotation priority adapter` | `priority_source` | Für die aktuelle Ausbaustufe zwingend `FRONTEND`. `PLC` ist nur eine vorbereitete Adaptergrenze und bleibt fail-closed. |
| `Stem rotation optimizer` | Drehgrenzen | `rotation_negative_limit_deg` und `rotation_positive_limit_deg` auf die realen, sicheren Maschinenlimits setzen. `0°` muss innerhalb des Bereichs liegen. |
| Kanalbedienung | `priority_mode` | Für den jeweiligen Versuch `VORSCHNITT_KRITISCH`, `NACHSCHNITT_KRITISCH` oder fail-closed `NONE` wählen. |

### Bedeutung des Prioritätsmodus

| Frontend-Wert | Optimierte Richtung |
|---|---|
| `VORSCHNITT_KRITISCH` | vertikal, oben/unten, Maschinenachse `z` |
| `NACHSCHNITT_KRITISCH` | horizontal, links/rechts, Maschinenachse `y` |
| `NONE` | keine gültige Optimierung und keine Drehempfehlung |

Ein Moduswechsel im Frontend berechnet dasselbe bereits eingefrorene Scheibenmodell neu. Dafür
ist keine neue Kameramessung erforderlich. Quelle, Modus und Revision müssen im Ergebnis zum
aktuellen Frontendzustand passen.

## 4. Winkelkalibrierung durchführen

Die Winkelkalibrierung ist die wichtigste Voraussetzung für eine signierte Gradzahl.

1. In der Node `Stem surface phase` den Kalibrierassistenten öffnen.
2. Einen markierten Teststamm oder Referenzzylinder verwenden.
3. Einen kurzen Live-Kalibrierlauf aufnehmen.
4. Ein scharfes Bild als `0°`-Referenz festlegen.
5. Mindestens drei weitere eindeutige Bilder desselben Tracks auswählen. Sechs oder mehr
   über einen breiten Winkelbereich verteilte Bilder sind für den robusten Fit vorzuziehen.
6. Für jedes ausgewählte Bild nur den wirklich sichtbaren, signierten relativen Winkel
   eintragen. Die Rohphase wird automatisch gemessen und nicht von Hand eingegeben.
7. **Fit and apply degree calibration** ausführen.
8. Das Profil nur verwenden, wenn der Assistent **Surface phase calibration valid** meldet.

Die Vorlage verlangt mindestens `90°` Winkelabdeckung. Bei Richtungswechsel muss der
eingetragene Winkel wieder kleiner werden. Hohe Fit-Fehler werden durch höhere erlaubte
Fehlergrenzen nicht behoben; in diesem Fall Markierung, Vorzeichen, Track-Kontinuität und
Bildschärfe prüfen.

Empfohlene Startgrenzen des Assistenten:

| Parameter | Startwert | Bedeutung |
|---|---:|---|
| Maximum mean error | `3°` | maximaler mittlerer absoluter Fit-Fehler |
| Maximum single error | `7,5°` | maximaler Fehler eines einzelnen Messpaares |
| Durchmesserbereich | realer Einsatzbereich | Außerhalb dieses Bereichs ist die Kalibrierung nicht gültig. Leer bedeutet automatische Ableitung aus den Referenzframes. |
| Positionsmarge X/Y | zunächst `20 %` | zulässige Abweichung der Stammposition von den Kalibrierframes |

## 5. Startwerte für Scheibenmodell und Messqualität

Diese Werte sind in der Vorlage bereits gesetzt. Für die erste Inbetriebnahme zunächst nicht
gleichzeitig verändern.

| Parameter | Startwert | Wirkung |
|---|---:|---|
| `slice_spacing_mm` | `50 mm` | Abstand der Scheiben entlang Stamm-`x` |
| `minimum_observations_per_slice` | `7` | Mindestzahl verwertbarer Winkelbeobachtungen je Scheibe |
| `minimum_unique_phases` | `6` | Mindestzahl ausreichend verschiedener Drehphasen |
| `minimum_phase_span_deg` | `90°` | erforderliche Winkelabdeckung je Scheibe |
| `phase_separation_deg` | `8°` | Mindestabstand zweier als verschieden gezählter Phasen |
| `maximum_center_residual_rms_mm` | `5 mm` | höchster zulässiger RMS-Fehler des Mittelpunkt-Fits |
| `maximum_width_residual_rms_mm` | `5 mm` | höchster zulässiger RMS-Fehler des Breiten-/Ellipsen-Fits |
| `minimum_slice_quality` | `0,25` | Mindestqualität einer gültigen Scheibe |
| `minimum_valid_slice_count` | `3` | mindestens erforderliche gültige Scheiben |
| `minimum_valid_slice_ratio` | `0,60` | mindestens 60 % gültige Scheiben |
| `minimum_model_quality` | `0,25` | Mindestqualität für ein verwendbares Gesamtmodell |
| `degraded_model_quality` | `0,60` | darunter wird das Modell als qualitativ eingeschränkt bewertet |
| `maximum_interpolation_gap_mm` | `100 mm` | nur kurze Lücken bis zu zwei 50-mm-Abständen interpolieren |
| `smoothing_window_slices` | `3` | kleines Glättungsfenster; lokale Engstellen bleiben erkennbar |
| `smoothing_strength` | `0,25` | Stärke der robusten Glättung |
| `maximum_observation_frames` | `180` | Speichergrenze der Beobachtungsfolge |
| `include_raw_observations` | `false` | Normalbetrieb; nur für begrenzte Diagnose-/Replay-Läufe vorübergehend aktivieren |

### Natürliche Rollphase und Material-X-Registrierung

Es gibt keine kontrollierte Messdrehung. Beobachtet wird ausschließlich das natürliche Rollen
beim Einwurf im Transferbereich. Die Vorlage verwendet:

| Parameter | Startwert |
|---|---:|
| `maximum_material_x_step_mm` | `75 mm` zwischen zwei akzeptierten Profilen |
| `maximum_material_x_excursion_mm` | `150 mm` relativ zur ersten akzeptierten Transferlage |
| `maximum_frame_sequence_gap` | `2` zwischen zusammengehörigem Krümmungs- und Phasenpayload |

Der `Active stem tracker` trennt Transfer und Längsförderung zweistufig. Ein einzelner
X-lastiger Maskensprung reicht nicht mehr für die Umschaltung. Als vorläufige Startwerte gelten:

| Tracker-Parameter | Startwert | Bedeutung |
|---|---:|---|
| `process_motion_axis_dominance_ratio` | `1,75` | Ein Frame zählt nur als X-Evidenz, wenn die kalibrierte X-Bewegung gegenüber Y ausreichend dominiert. |
| `minimum_process_motion_mm_per_frame` | `2 mm/Frame` | Unterhalb dieses Betrags gilt die Bewegung nicht als belastbare Prozessevidenz. |
| `longitudinal_motion_confirmation_frames` | `3` | Mindestzahl X-dominanter Beobachtungen im Zeitfenster. |
| `longitudinal_motion_window_frames` | `6` | Begrenztes Zeitfenster für die Umschaltentscheidung. |
| `longitudinal_minimum_forward_displacement_mm` | `120 mm` | Mindest-Nettofortschritt in konfigurierter Förderrichtung; liegt bewusst über der erwarteten Transferdrift von ungefähr `+/-100 mm`. |
| `longitudinal_minimum_forward_progress_ratio` | `0,75` | Netto-X-Fortschritt geteilt durch den gesamten absoluten X-Weg; verhindert eine Umschaltung bei Hin-und-her-Bewegung. |
| `longitudinal_entry_centroid_required` | `true` | Der Stamm-Mittelpunkt muss zusätzlich im Längsförder-ROI liegen. |

Bereits gespeicherte Tracker-Nodes ohne diese vier neuen `longitudinal_*`-Felder verwenden nach
dem Runtime-Update automatisch die genannten Defaults. Der Node muss nicht gelöscht oder neu
angelegt werden. Zum abweichenden Parametrieren die bestehende Node öffnen und die Pipeline als
neue Version speichern.

Transfer- und Längsförder-ROI dürfen sich an der Übergabe leicht überlappen, sollen aber nicht
deckungsgleich oder vollständig ineinander liegen. Der Längsförder-ROI beginnt an der realen
Übergabestelle. Nur so liefert die räumliche Eintrittsbedingung zusätzliche Information zur
zeitlichen Bewegungsprüfung. Die Werte sind Inbetriebnahme-Startwerte und müssen anhand realer
Replay-Daten validiert werden.

Die registrierte Verschiebung stammt aus der vorhandenen Mantelphasenverfolgung und der
Prozesshomographie, nicht aus einer neuen Segmentierung oder einem neuen Tracker. `150 mm` ist
ein vorläufiger Inbetriebnahmewert für die genannte reale Erwartung von höchstens ungefähr
`+/-100 mm`; nach Replay realer Stämme enger parametrieren. Grenzen nicht nur erhöhen, um einen
grünen Status zu erhalten.

Erst wenn X-Evidenz, vorzeichenrichtiger Nettofortschritt, Richtungskonsistenz und räumlicher
Eintritt gemeinsam erfüllt sind, bestätigt der Tracker `longitudinal_conveyor`. Das
Scheibenmodell wird dann eingefroren. Im anschließenden Längstransport findet keine weitere
Scheibenmessung statt. X-lastige Transferframes bleiben bis dahin Messkandidaten; nur ein
tatsächlich plausibler Übergang wird als `LONGITUDINAL_TRANSITION_PENDING` zurückgehalten.

## 6. Startwerte für den Drehoptimierer

| Parameter | Startwert | Wann ändern? |
|---|---:|---|
| `slice_spacing_mm` | `50 mm` | Muss zum Scheibenmodell passen. |
| `x_eval_start_mm` | leer | Anfang der Auswertung; erst nach realen Messungen zum Ausschluss unzuverlässiger Enden setzen. |
| `x_eval_end_mm` | leer | Ende der Auswertung; leer wertet die gesamte gültige Messlänge aus. |
| `smoothing_window_slices` | `3` | Zusammen mit dem Builder beurteilen; nicht erhöhen, wenn lokale Engstellen verschwinden. |
| `coarse_angle_step_deg` | `1°` | Grobsuche; zunächst belassen. |
| `fine_angle_step_deg` | `0,1°` | Feinsuche; zunächst belassen. |
| `fine_search_half_width_deg` | `2°` | Feinbereich um das Groboptimum; zunächst belassen. |
| `score_tolerance_mm` | `1 mm` | Innerhalb dieser Score-Differenz gewinnt der kleinere absolute Drehweg. |
| `min_gain_mm` | `3 mm` | Unter diesem Geometriegewinn wird keine Korrekturdrehung empfohlen. Nach Wiederholmessungen auf einen Wert oberhalb des Messrauschens einstellen. |
| `min_rotation_deg` | `1°` | Kleinere Stellwinkel werden unterdrückt. An reale Stellgenauigkeit anpassen. |
| `min_valid_slice_ratio` | `0,60` | Mit dem Scheibenmodell konsistent halten. |
| `min_valid_slice_count` | `3` | Mit dem Scheibenmodell konsistent halten. |
| `min_model_quality` | `0,25` | Freigabegrenze; nicht absenken, nur um Ergebnisse zu erzwingen. |
| `max_interpolation_gap_mm` | `100 mm` | Mit dem Scheibenmodell konsistent halten. |
| `rotation_negative_limit_deg` | **standortbezogen** | Reale sichere negative Grenze; `-90°` der Vorlage ist nur ein Platzhalter/Startwert. |
| `rotation_positive_limit_deg` | **standortbezogen** | Reale sichere positive Grenze; `+90°` der Vorlage ist nur ein Platzhalter/Startwert. |

Für den ersten Versuch sind besonders die beiden mechanischen Grenzen zwingend zu ersetzen.
`min_gain_mm`, `score_tolerance_mm` und `min_rotation_deg` werden anschließend anhand mehrerer
Wiederholmessungen realer Stämme beurteilt.

## 7. Weitere Vorlagenwerte zunächst beibehalten

Die Vorlage ist für die neue Messkette bereits vorbereitet:

- horizontale Krümmungsmessung höchstens alle `50 ms`; damit sind bis zu `20 Hz`
  echte Silhouettenbeobachtungen möglich, sofern Segmentierung und restliche Runtime diese
  Rate tatsächlich liefern,
- Phasenschätzer `CYLINDRICAL_SURFACE_V2`,
- Ausführungsmodus `HIGH_RATE_SEEDED`,
- Zielrate `25 fps`,
- maximale Trackingbreite `960 px`,
- Replayfenster `500 ms` mit höchstens `16` Frames,
- maximale Seed-Alterung `300 ms`,
- Mindestunschärfemaß `18`,
- maximaler Vorwärts-/Rückwärtsfehler `1,5 px`,
- minimale Feature-Spanne `0,35`.

Diese Werte nur ändern, wenn reale Diagnoseaufzeichnungen einen konkreten Engpass zeigen.

### Frame-synchrone Parallelverarbeitung

Die Segmentierung bleibt unverändert und ist weiterhin die alleinige Quelle jeder
Silhouettenbeobachtung. Nach einer Segmentierung läuft der Datenfluss so:

```text
Segmentierung -> Active-Stem-Tracker (geordnet, eindeutige Stamminstanz)
                           |
                           +-> Messfreigabe -> Krümmungs-/Breitenprofil --+
                           |                                               |
                           +-> schnelle Maskengeometrie -> Phasenwert ------+-> Join
```

Die beiden Äste werden innerhalb desselben Frames mit zwei Workern parallel ausgeführt. Der
Scheibenmodell-Builder wird erst gestartet, wenn beide Äste abgeschlossen sind. Er erhält daher
weiterhin genau zusammengehörige Track-, Frame- und Kalibrierungsdaten. Ein Fehler oder fehlender
Pflicht-Input in einem Ast sperrt den Join; es wird kein altes Teilergebnis mit einem neuen Frame
kombiniert.

Zusätzlich dürfen bei dieser exakt erkannten Stamm-Messpipeline bis zu zwei Frames
stufenweise gleichzeitig laufen:

```text
Frame N:   Undistort -> kanonisch -> Segmentierung -> Tracker[N] -> Geometrie/Krümmung/Phase -> Builder[N]
Frame N+1:    Undistort -> kanonisch -> Segmentierung -> Tracker[N+1] -> Geometrie/Krümmung/Phase -> Builder[N+1]
                                                        ^ jeder Stateful-Node hat eine eigene Sequenzbarriere
```

Active-Stem-Tracker, Krümmungszustand, Phasenzustand und Scheibenmodell-Builder behalten jeweils
für sich die strikte Reihenfolge nach Frame-ID. Sie bilden aber keine gemeinsame globale Sperre
mehr: Sobald der Tracker von Frame N fertig ist, darf der Tracker von Frame N+1 arbeiten, während
Frame N noch Maskengeometrie oder Krümmung berechnet. Entsprechend wartet der Builder von Frame
N+1 nur auf den Builder von Frame N und nicht auf eine globale Framefreigabe. Die Phasenmessung
verwendet weiterhin ausschließlich die echte Segmentierung und die Maskengeometrie desselben
Frames. `pipeline_max_inflight=2` ist dafür der Startwert; höhere Werte werden für diesen Pfad
nicht benötigt und bleiben durch die vorhandenen Inferenzgrenzen beschränkt.

Die Aktivierung erfolgt fail-closed nur für den bekannten Graphen mit genau einer Segmentierung,
dem Active-Stem-Tracker und den beiden unabhängigen Messzweigen. Zusätzliche unbekannte
zustandsbehaftete Nodes, insbesondere SPS-Schreibnodes, schalten auf die bisherige vollständige
Frame-Serialisierung zurück. Die Änderung erzeugt keine Zwischenmasken und keine erfundenen
Messwerte: Jede Krümmungs-/Breitenbeobachtung stammt weiterhin aus der tatsächlichen Ausgabe des
Segmentierungsmodells.

In den Runtime-Diagnosedaten zeigt `timing.node_execution_mode = parallel_dag`, dass der Pfad
aktiv ist. `timing.parallel_node_keys` muss die vier Nodes `measurement_eligibility`,
`stem_geometry`, `measure_curvature` und `track_surface_phase` enthalten. Die Aktivierung erfolgt
nur, wenn die erwarteten Graphkanten ohne Querabhängigkeit vorhanden sind; andernfalls bleibt die
Engine fail-closed im seriellen Modus.

Die frameübergreifende Stufe ist aktiv, wenn zusätzlich
`timing.ordered_stateful_execution = true`, `timing.ordered_stateful_mode = per_node` und
`diagnostics.pipeline_inflight_limit_reason = ordered_stateful_overlap` erscheinen.
`stage_timings_ms.ordered_stateful_wait_ms` zeigt die größte Wartezeit eines Stateful-Nodes im
Frame. Zusätzlich werden Werte wie
`stage_timings_ms.ordered_stateful_wait_active_stem_ms` und
`stage_timings_ms.ordered_stateful_wait_build_slice_model_ms` ausgegeben. Damit lässt sich
unterscheiden, ob Tracker, Krümmung, Phase oder Builder die Folgeframes begrenzen. Eine hohe
Wartezeit ist kein Datenfehler, sondern ein Hinweis auf den konkreten Durchsatzengpass.

Für eine bereits veröffentlichte Pipeline muss nach dem Update kein Node gelöscht oder neu in den
Designer gezogen werden, sofern diese vier Nodes und ihre Standardkanten bereits vorhanden sind.
Der Wert `minimum_observation_interval_ms` des bestehenden `measure_curvature`-Nodes wird durch
ein Update einer veröffentlichten Pipelineversion jedoch nicht automatisch geändert: für den
15–20-Hz-Versuch auf `50 ms` setzen, die Pipeline neu veröffentlichen und den Kanal anschließend
neu laden. Das Segmentierungsmodell und sein Letterboxing bleiben dabei unverändert.

## 8. Pipeline aktivieren und Ergebnis prüfen

Nach Speichern, Validieren und Veröffentlichen:

1. Den Kanal mit der veröffentlichten Pipeline starten beziehungsweise neu laden.
2. Im Frontend prüfen: `Quelle: Frontend (Test/Inbetriebnahme)`.
3. `optimization_enable` aktivieren.
4. Zunächst `priority_mode = NONE` verwenden und Segmentierung, Messkorridor, Tracker sowie
   Kalibrierstatus beobachten.
5. Für den Shadow-Test `VORSCHNITT_KRITISCH` oder `NACHSCHNITT_KRITISCH` wählen.
6. Einen Stamm im normalen Ablauf von rechts einrollen lassen. Prüfen, dass Beobachtungen nur
   während `transfer` entstehen und das Modell beim Wechsel zu `longitudinal_conveyor`
   eingefroren wird.
7. Nur ein Ergebnis mit `result_ready=true` und `result_valid=true` fachlich auswerten.
8. Zusätzlich prüfen, dass `shadow_mode=true` und `plc_writes_suppressed=true` protokolliert sind.

Erwartete Zustandsfolge:

```text
IDLE -> MEASURING -> MODEL_BUILDING -> OPTIMIZING -> RESULT_READY
```

`should_rotate=false` ist nicht automatisch ein Fehler. Bei ausreichender Modellqualität kann
es korrekt bedeuten, dass Gewinn oder Stellwinkel unter den Mindestgrenzen liegen.

Bei einem neuen Stamm müssen alte Ergebnisse verschwinden beziehungsweise eindeutig als Historie
gekennzeichnet sein. Während `MEASURING`, `MODEL_BUILDING` oder `OPTIMIZING` darf kein altes
Ergebnis als aktuelle Empfehlung erscheinen.

### LiveView-Ansichten prüfen

Der Kanal veröffentlicht drei kontinuierliche, latenzarme WebRTC-Basisstreams. Die vier Tabs
verwenden diese Streams und legen die Runtime-Overlays im jeweils richtigen Bildraum darüber:

| Tab | Basisstream | Overlay-Koordinatenraum |
|---|---|---|
| `Full camera view` | verzerrtes Rohbild | auf die intrinsische Kameraverzerrung zurückprojiziert |
| `Undistorted full view` | vollständig intrinsisch entzerrtes Bild | entzerrtes Vollbild |
| `Canonical model view` | kanonisches Pipeline-/Label-Studio-Bild | kanonische Modellansicht |
| `Full view + projected overlay` | vollständig intrinsisch entzerrtes Bild | entzerrtes Vollbild |

In jedem Tab müssen dieselben verfügbaren Overlay-Layer schaltbar sein. Masken und Heatmaps werden
als Bildtexturen projiziert; Vektoren und Messwerte bleiben eigenständige Overlays. Die Streams
enthalten die Overlays absichtlich nicht im H.264-Bild, damit das Video unabhängig von der
Inferenz flüssig bleibt und Layer ohne erneute Kodierung ein- oder ausgeschaltet werden können.
Entzerrung und kanonische Transformation laufen als getrennte Latest-Frame-Stufen; ein langsamer
kanonischer Transform darf deshalb keinen Frame-Rückstau im entzerrten Vollbild erzeugen.

Vor einem Versuch müssen `Production stream ready`, `Undistorted live stream ready` und
`Canonical live stream ready` angezeigt werden. Bei Ruckeln zuerst im Stream-Status
`transform_fps`, `transform_dropped_frames`, `processing_latency_ms`, `output_fps`,
`dropped_frames` und `source_frame_age_ms` prüfen. Dadurch lässt sich ein Engpass der
Bildtransformation von Encoder-, Publikations- oder Browserproblemen unterscheiden.
Im stabilen Betrieb sollen Transform- und Ausgabe-FPS nahe an der Kamera-FPS liegen; fortlaufend
steigende Drop-Zähler oder ein wachsendes Frame-Alter sind ein Inbetriebnahmefehler.

## 9. Häufige ungültige Ergebnisse

| Anzeige/Grund | Zuerst prüfen |
|---|---|
| unzureichende Winkelabdeckung | natürliche Rollstrecke im Transfer, Anzahl unterschiedlicher Phasen, Phasenkalibrierung und sichtbare Rindenmerkmale |
| ungültiges Scheibenmodell | Segmentierung, Kalibrierungen, gültiger Scheibenanteil und Fit-Residuen |
| geringe Modellqualität | Bildschärfe, Verdeckung, mehrere Stämme, Endbereiche und Track-Verlust |
| `MATERIAL_X_REGISTRATION_UNAVAILABLE` | Mantelphase gültig/locked, Prozesshomographie gebunden und metrische Translation im Payload vorhanden |
| `MATERIAL_X_REGISTRATION_STEP_IMPLAUSIBLE` / `MATERIAL_X_EXCURSION_TOO_LARGE` | Trackingausreißer, Prozesskalibrierung und tatsächliche Transfer-X-Bewegung prüfen; Grenze nicht blind erhöhen |
| `LONGITUDINAL_TRANSITION_PENDING` | X-Richtung, Nettofortschritt, Konsistenz und räumlicher Eintritt sprechen bereits für beginnenden Längstransport; dieser Verdachtsframe wird nicht gesammelt |
| `LONGITUDINAL_TRANSPORT_STARTED` | erwartetes Messende; Modell muss nun eingefroren sein und darf keine neuen Beobachtungen erhalten |
| mechanischer Grenzkonflikt | reale positive/negative Grenzen und Vorzeichenkonvention |
| `PLC_PRIORITY_PROVIDER_NOT_CONNECTED` | `priority_source` zurück auf `FRONTEND`; aktuell existiert keine aktive SPS-Quelle |
| niedriger Gewinn/kleiner Winkel | korrektes Ergebnis möglich; `min_gain_mm` und `min_rotation_deg` nicht vorschnell absenken |

## 10. Empfohlene Reihenfolge der Inbetriebnahme

Immer nur eine Fehlerklasse beziehungsweise Parametergruppe gleichzeitig bearbeiten:

1. Kameraauflösung und intrinsische Kalibrierung.
2. Kanonische Ansicht und Prozesskoordinaten.
3. Segmentierungsmodell und Stammklasse.
4. Tracker, Messfreigabe und Crowd-Grenze.
5. Reale Winkelkalibrierung einschließlich Vorzeichenprüfung.
6. Scheibenabdeckung, Fit-Residuen und Modellqualität an realen Stämmen.
7. Mechanische Drehgrenzen.
8. Wiederholmessungen zur Bestimmung des Messrauschens.
9. Erst danach `score_tolerance_mm`, `min_gain_mm` und `min_rotation_deg` anpassen.
10. Scheibenabstand oder Glättung nur ändern, wenn reale Replays die Notwendigkeit zeigen.

Reale Stämme haben dabei Vorrang. Für problematische Fälle den Result-History-Snapshot exportieren
und mit dem vorhandenen Replay-Werkzeug sichern. Rohbeobachtungen nur für zeitlich begrenzte
Diagnoseläufe einschalten.

## 11. Kurzcheck vor jedem Maschinenversuch

- [ ] Produktionsversuch: richtiges intrinsisches Kalibrierprofil und exakt passende
      Auflösung aktiv; reiner Testaufbau: `IDENTITY_PASSTHROUGH` sichtbar als noch nicht
      metrisch freigegeben dokumentiert
- [ ] Kanonische Ansicht und Prozesskoordinaten passen zur realen Maschine
- [ ] Segmentierungsmodell ist aktiv und liefert nur den vorgesehenen Stammtrack
- [ ] Winkelkalibrierung ist gültig und an dieselbe Ansicht/Kalibrierung gebunden
- [ ] Positive Drehrichtung wurde an der Maschine verifiziert
- [ ] Reale mechanische positive und negative Grenzen sind eingetragen
- [ ] Prioritätsquelle ist eindeutig `FRONTEND`
- [ ] Gewählter Prioritätsmodus entspricht dem Versuch
- [ ] Modellqualität und gültiger Scheibenanteil reichen aus
- [ ] Scheibenbeobachtungen stammen nur aus `transfer`; bei `longitudinal_conveyor` ist das
      Modell eingefroren
- [ ] Registrierte X-Auslenkung bleibt plausibel (Testaufbau ungefähr `+/-100 mm`)
- [ ] Ergebnis ist `ready` und `valid`
- [ ] Shadow-Modus und unterdrückte SPS-Schreibzugriffe sind sichtbar
- [ ] Logging/Result-History ist aktiv und der Versuch kann replayt werden

Ergänzende technische Details stehen in
[`stem_rotation_runtime_integration.md`](stem_rotation_runtime_integration.md),
[`stem_slice_measurement.md`](stem_slice_measurement.md) und
[`stem_rotation_validation_and_commissioning.md`](stem_rotation_validation_and_commissioning.md).
