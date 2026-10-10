# Serveranalyse nach dem Recovery-Update – 15.09.2026

Das Update verbessert die zeitliche Zuordnung und den Wiederanlauf deutlich. In einem dreiminütigen Messfenster wurden **222 akzeptierte Profil-/Phasenpaare statt zuvor 35** gezählt. Trotzdem wurde **kein gültiges Endergebnis** beobachtet. Die verbleibenden Hauptprobleme sind häufig unterbrochene Phasenspuren, hohe Last im Phasenworker und eine zu geringe zusammenhängende Winkelabdeckung der akzeptierten Profile.

**Messgrundlage**

Lesende HTTPS-Diagnoseabfragen auf `172.23.27.242`, Kanal `Stammeindrehung inkl. Modell`, ID `chn_8db0e33874024d15b65f370efbbcc12c`. Fenster der angezeigten Zustände: **15.09.2026, 08:44:04–08:47:04 MESZ**. 712 Pollantworten enthalten 537 unterschiedliche Frame-/Zeitstempelzustände; 536 liefern die regulären FPS-Metriken, 533 Phasenmetadaten. Dedupliziert wurde anhand von Quellsequenz und Quellzeitstempel. Mediane und P95 beziehen sich auf diese Stichprobe; P95 ist der sortierte Wert am nullbasierten Index `floor(0,95 × n)`.

Die aktive Pipeline bleibt **v41**, `plv_9de3adfeb71440cdb13804d68240b65c`. Ihr erneut abgerufener Graph entspricht strukturell der vorherigen Aufzeichnung. Neue Recovery-, Replay- und Subschrittdiagnosen bestätigen die Ausführung des aktualisierten Runtimepfads. Ein serverseitiger Release-/Dateihash wurde nicht nachgewiesen. Die Systemstatusabfrage liefert weiterhin HTTP 401.

Der Vergleich zur [vorherigen Analyse](stem_pipeline_postupdate_analysis_2026-09-15.md) ist **kein kontrollierter A/B-Test**: Damals lief zusätzlich `Basler Beleimung`. Dieser Kanal steht beim jetzigen Statusabruf auf `down / Heartbeat stale`, trotz Sollzustand `running`; letzter Heartbeat 08:32:57 MESZ. Seine angezeigten 8,5 FPS stammen aus diesem alten Zustand und belegen keine aktuelle Hintergrundlast. Die Ursache des fehlenden Heartbeats wurde hier nicht untersucht. Verbesserte Laufzeiten dürfen deshalb nicht vollständig dem Update zugeschrieben werden.

Polling zeigt jeweils die letzte Anzeige und erfasst weder alle Ergebnisse noch alle realen Rollvorgänge. Es wurden keine Bilder oder Videos abgerufen, keine Servereinstellungen geändert und keine Neustarts oder SPS-Zugriffe ausgeführt.

**1. Was sich verbessert hat**

| Kennzahl | Vorher, 07:08–07:11 | Jetzt, 08:44–08:47 |
| --- | ---: | ---: |
| Quellrate, Median | 30,0 FPS | **30,0 FPS** |
| Vollständige Pipeline, Median | 4,9 FPS | **7,85 FPS** |
| Angezeigte Einzellatenz, Median | 349 ms | **241 ms** |
| Einzellatenz, P95 | 606 ms | **420 ms** |
| Verwerfungsrate, Median | 83,4 % | **73,8 %** |
| Exakte verriegelte Rohphasen-/Geometriepaare, Zählerdifferenz | 82 | **866** |
| Akzeptierte Profil-/Phasenpaare, Zählerdifferenz | 35 | **222** |
| Paarwartezeit abgelaufen, Zählerdifferenz | 148 | **24** |
| Alter des angewandten Seeds, Median | 2.310 ms | **117 ms** |
| Höchstes angezeigtes Seedalter | 8.360 ms | **1.845 ms** |

Die Softwarekorrektur wirkt in die erwartete Richtung: **242 Seedframes** wurden aus historischen Vorgängerzuständen rekonstruiert. `PHASE_SEED_HISTORY_MISSING` wurde in den angezeigten Phasen nicht mehr gesehen; dieser interne Anlass kann jetzt durch den Recoverypfad abgefangen werden. `PHASE_SEED_STALE` erschien noch in 16 Zuständen statt zuvor 214. Das ist mit einem funktionierenden Wiederanlauf vereinbar; es beweist nicht, dass jede Recovery eine anschließend ausreichend lange Spur liefert.

In 360 der 533 Zustände mit Phasenmetadaten waren **sowohl** `phase_locked` als auch `calibrated_phase_valid` wahr, etwa 67,5 %. Zuvor waren es 37 von 424, etwa 8,7 %. `calibrated_phase_valid` allein genügt nicht: Auch gehaltene Zustände können dieses Flag tragen, obwohl keine neue verriegelte Messung vorliegt.

**2. Bildübernahme stabil, Phasenworker weiterhin überlastet**

Kumulative Zählerdifferenzen über das Messfenster:

| Zähler | Differenz |
| --- | ---: |
| High-Rate-Quellframes übernommen | **5.412** |
| Ringsequenzpositionen überschrieben | **0** |
| Ringsequenzpositionen übersprungen | **0** |
| Als verarbeitet gezählte Phasenframes | **2.784** |
| Durch Ziel-FPS-Begrenzung ausgelassen | **923** |
| Aus der Phasenqueue verworfen | **1.548** |
| Wegen Generation verworfene Frames | **484** |
| Wegen Queuealter verworfene Frames | **0** |
| Doppelte Frames | **0** |
| Zusätzliche Replay-Schritte | **1.903** |
| Recovery mit neuer Generation | **353** |
| Veraltete Seeds verworfen | **21** |
| Seed-/Replaydeadline abgelaufen | **54** |
| Technische Phasenverarbeitungsfehler | **0** |

Die Zähler sind keine disjunkte Bilanz: Replay, Seedbearbeitung, Generationwechsel und zeitversetzte Veröffentlichung überschneiden sich. `processed_frames` zählt auch ungültige Bearbeitungen. Die 866 Rohphasenpaare sind deshalb ebenfalls nicht mit den 222 akzeptierten Profilpaaren gleichzusetzen.

Der kumulative Ringverlust steht bereits am Anfang auf 13.288 und wächst im Fenster nicht. Dieser Anfangswert ist kein Nachweis von Verlusten während der Messung. Der Reader startet beim Anbinden an einen bereits laufenden Ring mit `_last_sequence = 0`; damit können auch Sequenzpositionen vor dem Kanalstart im Zähler enthalten sein. Kamerareconnects und Quellfehler stehen in allen aufgezeichneten Metriken auf null.

Der gleitende Phasendurchsatz liegt jetzt bei **15,45 FPS im Median**, gegenüber zuvor 24,5 FPS, trotz deutlich mehr gültiger Paare. Die 2.784 Bearbeitungen entsprechen rund 15,5/s. Der Worker führt zusätzlich etwa 10,6 Replay-Schritte/s aus. Seine letzte Bearbeitungszeit liegt im Median bei **55,8 ms**, P95 **165,3 ms**; diese Zeit umfasst gegebenenfalls Seedverarbeitung und mehrere Replaybilder und ist keine reine Optical-Flow-Zeit pro Bild.

Damit liegt der Verlust nun klar hinter der vollständigen Kameraübernahme. Die Zielrate beträgt unverändert 25 FPS bei drei Queueplätzen. Ein bloßes Anheben auf 30 FPS oder Vergrößern der Queue beseitigt die gemessene Rechenlast nicht und kann die zeitgerechte Zuordnung verschlechtern.

**3. Recovery verhindert das Festhängen, unterbricht aber häufig die Messhistorie**

353 Recovery-Neuanläufe in rund 180 Sekunden entsprechen etwa **zwei neuen Winkelursprüngen pro Sekunde**. Das ist eine Gesamtrate über den Kanal, keine gemessene Lebensdauer einer Spur innerhalb jedes Rollvorgangs.

95 angezeigte Phasenzustände tragen `PHASE_FAST_PATH_GENERATION_MISMATCH`. Der Code erklärt diesen Mechanismus: Wenn ein historischer Seed nicht mehr an eine gültige Spur anschließen kann, startet der Worker eine neue Generation. Bereits auf die alte Generation wartende Pipelineframes werden ungültig. Der Slice-Builder trennt daraufhin die Messhistorien. Dieser Schutz darf nicht aufgehoben werden, um scheinbar mehr Beobachtungen zusammenzubekommen.

Die konkrete Ursache jedes Wiederanlaufs ist aus dem letzten angezeigten Zustand nicht rekonstruierbar. Ein Recovery kann seinen ursprünglichen Fehler bereits verdeckt haben. Sichtbar sind unter anderem:

| Phasengrund | Unterschiedliche angezeigte Zustände |
| --- | ---: |
| `PHASE_FEATURE_COUNT_INSUFFICIENT` | 34 |
| `PHASE_RELOCK_NO_PLAUSIBLE_MATCH` | 35 |
| `PHASE_AFFINE_PROPAGATION_FAILED` | 10 |
| `PHASE_SEED_STALE` | 16 |
| `PHASE_PAIR_DEADLINE_EXPIRED` | 16 |
| `PHASE_CALIBRATION_OUTSIDE_DIAMETER_RANGE` | 2 |

Gründe können gemeinsam auftreten; sie sind keine unabhängigen Ereigniszähler. Die im Phasenstatus gemeldete Sequenzlücke hat P95 sieben und erreicht maximal 47. Weil der Tracker bei gehaltenen Zuständen seine letzte gültige Referenz behalten kann, ist diese Größe nicht gleichbedeutend mit entsprechend vielen verlorenen Kameraframes.

In den sichtbaren Zuständen liegen der mediane Blur-Score bei etwa 1.042 gegenüber einer Schwelle von 18 und der mediane Vorwärts-/Rückwärtsfehler bei 0,033 Pixel. Es wurde kein `PHASE_FRAME_BLURRED` in der Anzeige gesehen. Daraus folgt keine Freigabe der Belichtungszeit: Gerade die nicht erfolgreich verarbeiteten Bilder während der schnellen Rotation fehlen in dieser Stichprobe.

**4. Fachlicher Engpass: zu wenig zusammenhängende Winkelabdeckung**

Beobachtet wurden **69 Session-IDs**, davon 15 mit gesehenem `result_ready`, **keine mit gesehenem `result_valid`**. Eine Session-ID ist keine bestätigte physische Stammzählung. `result_ready` bedeutet hier nicht, dass eine gültige Drehkorrektur vorliegt.

Die höchste gesehene Zahl akzeptierter Beobachtungen je Materialstation und Session liegt im Median weiterhin bei **eins**, maximal jetzt bei **neun** statt zuvor vier. Ein konkreter Fit mit neun Beobachtungen und drei unterschiedlichen Phasen scheitert ausschließlich an `PHASE_COVERAGE_INSUFFICIENT`: Er deckt **21,27°** ab. Die größte in irgendeiner Stationsdiagnose beobachtete Spanne beträgt **42,09°**. Kein angezeigter Stationsfit erfüllt gleichzeitig die drei unveränderten Mindestbedingungen: sieben Beobachtungen, drei unterschiedliche Phasen und 60° Spanne.

Diese Winkel sind Ausgaben der konfigurierten visuellen Kalibrierung, keine unabhängig vermessenen physischen Drehwinkel. Die reale Bewegung über die Erhöhung ist damit noch nicht validiert.

Bei 7,85 vollständigen Pipelineframes/s entstehen rechnerisch durchschnittlich **3,9 Ergebnisse in 0,5 Sekunden**, noch vor Qualitäts-, Bewegungs- und Paarablehnungen. Sieben Beobachtungen in diesem Zeitfenster benötigen bereits größenordnungsmäßig **14 akzeptierte Geometriebeobachtungen/s**. Das ist eine Durchsatzorientierung, keine Garantie für sieben zeitgerecht verteilte Messpunkte. Die Akzeptanz muss während des tatsächlichen Rollens und auf demselben Materialabschnitt erreicht werden; Messungen im Warten oder Längstransport helfen diesem Ziel nicht automatisch.

Weitere angezeigte Ergebnisgründe sind `CURVATURE_INVALID`, `LONGITUDINAL_TRANSITION_PENDING` und `MATERIAL_X_REGISTRATION_STEP_IMPLAUSIBLE`. Der Slice-Builder sammelt diese in einer `reason_code_history`. Ihre Häufigkeit darf daher nicht als aktuelle Ablehnungszahl oder neue Ursache in jedem Frame interpretiert werden. Die Detailursache einer aktuellen Krümmungsablehnung fehlt weiterhin, weil deren Debugoverlay und die Rohbeobachtungsausgabe deaktiviert sind.

**5. Neue Zeitmessungen zeigen die nächsten Leistungshebel**

| Node, abgeschlossene angezeigte Ausführungen | Median vorher | Median jetzt | P95 jetzt |
| --- | ---: | ---: | ---: |
| `stem_geometry` | 174 ms | **67,5 ms** | **160,2 ms** |
| `measure_curvature` | 137 ms | **68,6 ms** | **126,7 ms** |
| `track_surface_phase` | 42 ms | **68,1 ms** | **180,0 ms** |
| `segment_stem` | 54 ms | **51,7 ms** | **212,9 ms** |

Die nun gemessenen Geometriesubschritte liefern weitere Anhaltspunkte:

| Subschritt | Median | P95 |
| --- | ---: | ---: |
| Geometrie: Stützpunkt-/Maskenaufbereitung | 3,3 ms | 16,0 ms |
| Geometrie: Achse und Projektion | 6,3 ms | 22,1 ms |
| Geometrie: Querprofile | **30,8 ms** | **102,6 ms** |
| Geometrie: Gesamtmessung | 66,0 ms | 158,8 ms |
| Phasen-Node: Bildaufbereitung | 4,5 ms | 16,8 ms |
| Phasen-Node: Seedübergabe und Warten | **50,0 ms** | **152,7 ms** |
| High-Rate-Quellframeaufbereitung | 6,0 ms | 15,8 ms |

Die Subschritte decken nicht jede Geometrieoperation ab. Mediane dürfen weder zur Nodezeit noch zu einer Pipeline-Endlatenz addiert werden. Auch die Pipelineanzeige enthält nur einen Ausschnitt der tatsächlich ausgeführten Frames.

**OpenCV meldet durchgehend 32 Threads**, bei zwei gleichzeitig erlaubten Pipelineframes und zusätzlichem Phasenworker. Die globale Inferenzparallelität bleibt vier, pro Release zwei. Das macht CPU-Konkurrenz zu einem konkret prüfbaren Ansatz; es beweist weder 32 gleichzeitig aktive Threads noch die genaue BLAS-Konfiguration. Ob ein oder zwei native Threads besser sind, muss gemessen werden.

**Nächste Schritte nach erwartetem Nutzen**

1. **Native Threadkonkurrenz kontrolliert reduzieren und vergleichen.** Die vorhandene Startoption `YBB_REALTIME_EXECUTOR_NATIVE_THREADS=1` beziehungsweise `2` zusammen mit `source_config.pipeline_max_inflight=1` beziehungsweise `2` in den vier dokumentierten Kombinationen testen. Gleiche reale Aufnahme, gleiche Kalibrierung und kontrollierte Hintergrundlast verwenden. Entscheidend sind rechtzeitige akzeptierte Profile pro Rollvorgang, zusammenhängende Phasenabdeckung, Queueverluste und Recoveries. Executor-Neustarts und Konfigurationsänderungen gehören in diesen gesonderten Leistungsversuch; sie wurden hier nicht ausgeführt. Die native Threadbegrenzung ist bereits implementiert und benötigt dafür keine neue Algorithmusversion.
2. **Recovery-Ursachen und doppelte Replayarbeit gezielt sichtbar machen.** Pro Wiederanlauf den auslösenden Grund, Seedsequenz, historischen Vorgänger, alte/neue Generation, ursprüngliches Seedalter und Replay-Schrittzahl erfassen. Dazu aktuelle Profilablehnungen separat von der bisherigen Grundhistorie ausgeben. Anschließend prüfen, ob geordnete Seedbearbeitung und Wiederverwendung gültiger Checkpoints unnötige erneute Verarbeitung späterer Bilder vermeiden können. Die 1.903 zusätzlichen Replay-Schritte sind ein messbarer Optimierungsansatz; die sichere Generations- und Sequenzprüfung bleibt erhalten.
3. **Querprofile und Krümmung unter der gewählten Threadkonfiguration profilieren.** Die Querprofile bilden mit P95 103 ms einen wesentlichen Geometrieposten. Die Krümmungs-Node benötigt weiterhin etwa 69 ms im Median. Wiederverwendung von Masken-/Stationsdaten nur dort erweitern, wo Koordinatenraum, Definition und Qualitätsprüfungen identisch bleiben. Geometrieergebnisse und Ablehnungsgründe gegen dieselben Bilder vergleichen. Die lokale Mikrobenchmarkverbesserung allein erklärt die beobachtete Live-Beschleunigung nicht.
4. **Eine reale Rollsequenz mit Winkelreferenz vollständig auswerten.** Vorwarteposition, Überrollen der Erhöhung, Übergang zum Längsförderer und Entscheidungsframe gemeinsam erfassen. Prüfen, wann Materialvergleichbarkeit verloren geht, welche Bewegung als Rotation oder Translation interpretiert wird und ob die nutzbare Spanne vor dem Längstransport genügt. Erst damit lässt sich entscheiden, ob nach den Softwareverbesserungen noch ein Modell-, Sichtfeld- oder Kalibrierungsproblem dominiert.

Die Mindestwinkelabdeckung oder die Generationssicherheit zu lockern würde die fehlende Messinformation nicht ersetzen. Eine belastbare horizontale oder vertikale Zielkorrektur ist durch dieses Messfenster weiterhin nicht nachgewiesen.

**Nachweise und Änderungsumfang**

Lokale Diagnoseartefakte liegen unter `.test-tmp/stem-recovery-live-20260915/`: `live.jsonl`, `summary.json`, `analysis.json`, `details.json`, `live-initial.json`, `channels.json`, `channel.json`, `status.json`, `definition.json` und `active-version.json`. Die Erfassung und Auswertung verwenden das vorhandene `scripts/diagnostics/runtime_live_probe.py` sowie lokale Hilfsskripte `stem_recovery_live_probe.py`, `stem_recovery_live_config.py`, `stem_recovery_live_analyze.py` und `stem_recovery_live_detail.py` unter `.test-tmp/`. Der API-Schlüssel wurde über die Prozessumgebung übergeben und nicht in die Diagnoseartefakte geschrieben.

In diesem Analyseauftrag wurde ausschließlich dieser Bericht als Repositorydatei ergänzt. Produktionscode, Kanal-/Dienstkonfiguration und SPS-Ausgaben wurden nicht geändert. Die Anwendungs- und Regressionstests aus der [Implementierungsdokumentation](stem_pipeline_recovery_implementation_2026-09-15.md) wurden für die reine Diagnoseauswertung nicht erneut ausgeführt.

**Nachtrag: Prüfung der aktiven Pipeline gegen die aktuelle Vorlage**

Erneuter lesender Serverabruf am 15.09.2026, Kanalstatus mit Heartbeat um 08:56 MESZ. Die Vermutung einer gewachsenen Konfiguration ist berechtigt. Ein veralteter Pipelineaufbau gegenüber der aktuellen Vorlage wurde jedoch nicht festgestellt:

- Der Kanal verwendet weiterhin v41 vom 03.09.2026, 13:03 MESZ. Diese Version ist zugleich die neueste und die veröffentlichte Version seiner eigenen Definition.
- Die zugehörige Vorlage `Stammausrichtung – geometrische Eindrehoptimierung` steht auf v4 vom 03.09.2026, 13:01 MESZ. Deren Nodes, Verträge, Verbindungen, Konfigurationen und Modellplatzhalter entsprechen der heute aus dem Repository erzeugten Vorlage.
- Aktive Kanalpipeline und Vorlage besitzen dieselben **14 Nodes und 25 Verbindungen**, ohne zusätzliche, fehlende oder anders typisierte Nodes. Die Unterschiede liegen in Konfiguration und standortspezifischer Modellbindung.
- Die andere Vorlage `Stammausrichtung – Krümmungsminimum` steht auf v13. Die davon abgeleitete `Stammkrümmungserkennung Pipeline` hat einen neuesten Entwurf v118 und eine veröffentlichte v117. Das sind andere Definitionen und ein anderer Auswertungspfad; ihre höheren Versionsnummern bezeichnen kein Upgrade der geometrischen Eindrehoptimierung.

Runtime-Code, Vorlage und Kanalversion werden getrennt verwaltet. `ensure_builtin_pipeline_templates()` aktualisiert die eingebauten Vorlagen; es überschreibt dabei keine bestehende Kanalinstanz. `instantiate_pipeline_template()` kopiert einen Vorlagengraphen in eine eigene Definition. Die Runtimekorrekturen können deshalb bei weiterhin angezeigter Kanal-v41 aktiv sein. Ein expliziter Konfigurationswert in der Kanalversion bleibt dabei erhalten, auch wenn sich Vorlagen- oder Registrydefaults ändern.

| Einstellung | Aktive v41 | Aktuelle geometrische Vorlage | Bewertung |
| --- | ---: | ---: | --- |
| Beobachtungen je Station, mindestens | 7 | 7 | Die Anforderung an den Messdurchsatz ist bereits in der Vorlage vorhanden. |
| Unterschiedliche Phasen, mindestens | 3 | 6 | Aktive Pipeline ist großzügiger. |
| Phasenspanne, mindestens | 60° | 90° | Vorlagenübernahme würde die aktuelle Abdeckungslücke vergrößern. |
| Stationsabstand | 100 mm | 50 mm | Aktive Pipeline berechnet weniger Stationen; kein offensichtlicher zusätzlicher Lasttreiber. |
| Zentrum-/Breitenresiduum, maximal | 8 mm | 5 mm | Aktive Pipeline lässt größere Fitabweichungen zu. |
| Seedalter, maximal | 500 ms | 300 ms | Rückkehr zur Vorlage würde Seeds früher veralten lassen. |
| Gefundene Features, maximal | 160 | 240 | Niedrigerer Deckel; in den gesehenen Phasenzuständen maximal 101 erkannt, daher dort kein nachgewiesener Engpass. |
| Gematchte Features, mindestens | 8 | 12 | Aktive Pipeline ist großzügiger. |
| Inlieranteil, mindestens | 0,55 | 0,70 | Aktive Pipeline ist großzügiger. |
| Maskenerosion | 0,18 | 0,15 | Aktive Pipeline verkleinert den nutzbaren Oberflächenbereich stärker. |
| Querbewegungsempfindlichkeit | 0,60 | 0,727273 | Aktive Pipeline verlangt einen höheren Y-Anteil für die Klasse `transfer_y`. |
| Krümmungs-Debugoverlay | aus | an | Spart Ausgabeaufwand, verdeckt im bisherigen Probeformat aber aktuelle Krümmungsablehnungen. |

`HIGH_RATE_SEEDED`, `phase_tracking_fast`, 25 Phasen-FPS, Trackingbreite 960 Pixel, drei Queueplätze, 500 ms Replayfenster, 16 Replayframes und 50 ms Mindestintervall der Krümmungsbeobachtung stimmen bereits mit der geometrischen Vorlage überein. Es gibt hier keinen gefundenen alten Synchronmodus oder zusätzlichen Frame-Sampler, den ein Vorlagenwechsel beseitigen würde.

**Konkrete Konfigurationspunkte für die weitere Prüfung**

1. **Modellbindung:** `segment_stem` verwendet die Modellfamilie `stammsegmentierung` mit `selection_policy=staged_test` und ohne fest eingetragene `deployment_release_id`. Die Vorlage verwendet den Platzhalter `required-segmentation-model` mit `active_production`. Die reale Modellfamilie ist eine notwendige Standortanpassung; die Testauswahl sollte dagegen auf Absicht geprüft werden. Aus der Auswahlregel allein lässt sich nicht schließen, welcher konkrete Release aktuell aufgelöst wurde oder dass dessen Masken schlechter sind. Vor einem Wechsel tatsächlichen Release und Maskenqualität vergleichen.
2. **Trackingmaske:** 0,18 statt 0,15 Erosion verkleinert den Bereich für Features stärker. Wegen der gesehenen Feature-/Relockausfälle ist ein Vergleich auf derselben Rollaufnahme sinnvoll. Zunächst nur diesen Parameter ändern und Featurebestand, Fehlzuordnungen und zusammenhängende Spur prüfen. Mehr Randfläche kann zugleich mehr unsichere Randfeatures aufnehmen; eine Verbesserung ist nicht vorab bewiesen.
3. **Prozesszonen und Übergangserkennung:** Die aktiven Transfer- und Längsförderer-Polygone ersetzen die Vollbildplatzhalter der Vorlage und dürfen nicht pauschal zurückgesetzt werden. Bei Empfindlichkeit 0,60 benötigt `transfer_y` einen Y-Anteil von 70 % an `abs(X)+abs(Y)`, gegenüber etwa 63,6 % in der Vorlage. Das kann Mischbewegung beim Überrollen der Erhöhung häufiger als `ambiguous` klassifizieren. Die X-Dominanzprüfung erfolgt separat davor; eine Änderung dieses Wertes allein verhindert deshalb keine fälschlich erkannte Längsbewegung. Zonen, Schwerpunktbewegung und Zeitpunkt von `longitudinal_transition_pending` müssen gemeinsam am Video geprüft werden.
4. **CPU-Einstellung:** Die gemeldeten 32 OpenCV-Threads sind keine Einstellung dieser Pipelinevorlage. Sie werden über die Executor-Startkonfiguration beeinflusst. Ein Neuanlegen der Pipeline behebt diese mögliche Konkurrenz nicht. Der kontrollierte Vergleich mit einem/zwei nativen Threads bleibt der erste Leistungsversuch.

**Nachgewiesene Entwicklungsreste ohne aktuelle Wirkung**

In `build_slice_model.config` stehen noch `maximum_axial_delta_mm=2`, `maximum_cumulative_axial_motion_mm=5` und `maximum_transverse_delta_mm=3`. Diese drei Schlüssel werden im aktuellen `cv_runtime` weder im Handler noch in der Registry verwendet. Sie erklären daher insbesondere keine Ablehnung bei zwei Millimetern Bewegung. Die heute wirksamen Materialgrenzen sind `maximum_material_x_step_mm=75` und `maximum_material_x_excursion_mm=150`; diese entsprechen der Vorlage. Die alten Schlüssel können bei einer späteren Konfigurationsbereinigung entfernt werden, wodurch sich das aktuelle Messverhalten nicht ändern sollte.

Auch `track_surface_phase.maximum_frame_gap=3` und `maximum_time_gap_seconds=2` gehören zur synchronen Verarbeitung. Der aktive High-Rate-Zweig kehrt vor dieser Prüfung zurück und verwendet seine eigene History-, Alters- und Deadlineverwaltung. Eine Erhöhung dieser beiden alten Lückengrenzen wäre deshalb kein Mittel gegen die jetzigen High-Rate-Ausfälle. `build_slice_model.maximum_frame_sequence_gap=2` beschreibt wiederum die Differenz zwischen Krümmungs- und Phasensequenz desselben Paares, nicht die Lücke zwischen zwei aufeinanderfolgenden Pipelinebildern; im High-Rate-Modus ist ohnehin exakte Sequenzgleichheit vorgeschaltet.

**Folgerung:** Einstellungen können das Problem mitverursachen. Es fehlt aber keine aktuellere geometrische Vorlagenstruktur, und zahlreiche aktive Qualitätsgrenzen sind bereits weniger streng als deren Defaults. Sinnvoll ist eine gezielte, vergleichbare Bereinigung und Erprobung auf Basis der vorhandenen Standortkonfiguration. Eine vollständige Übernahme der Vorlage würde strengere Abdeckungsgates und ungebundene Kalibrierungs-/Modellplatzhalter zurückbringen.

Vergleichsartefakte: `.test-tmp/stem-template-comparison-20260915/`, insbesondere `comparison.json`, erneut gelesene Definitionsdetails und aus den aktuellen Factoryfunktionen erzeugte Vorlagengraphen. Hilfsskripte: `.test-tmp/stem_template_compare.py` und `.test-tmp/stem_template_diff.py`. Es wurden auch für diesen Nachtrag keine Server-, Modell- oder Pipelineeinstellungen geändert.
