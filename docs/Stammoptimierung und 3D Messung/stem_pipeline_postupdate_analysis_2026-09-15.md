# Serveranalyse nach dem Update – 15.09.2026

Die verbesserte Bildübernahme ist im Betrieb sichtbar. Der Hauptengpass liegt jetzt eindeutig hinter der Übernahme: Zu wenige vollständige Geometrieergebnisse treffen auf eine gültige Phasenspur. Zusätzlich wurde eine Lücke in der implementierten Wiederanlaufsteuerung reproduziert, durch die eine einmal veraltete Spur trotz frischer Seeds weiter ungültig bleiben kann. Das Update ist deshalb noch keine erfolgreiche Leistungsabnahme der Stammausrichtung.

**Messgrundlage und Grenzen**

Lesende HTTPS-Abfragen am Server `172.23.27.242`, ohne Bildabruf, Kanaländerung, Neustart oder SPS-Zugriff. Messfenster der angezeigten Zustände: **15.09.2026, 07:08:23–07:11:23 MESZ**. 719 Antworten enthalten 427 unterschiedliche Frame-/Zeitstempelzustände, darunter einen Zustand ohne reguläre Frame-Metriken. Polling der letzten Anzeige ist kein vollständiger Mitschnitt aller Ergebnisse oder Rollvorgänge.

Aktiv ist weiterhin `Stammeindrehung inkl. Modell`, Kanal `chn_8db0e33874024d15b65f370efbbcc12c`, Pipeline **v41**, Version `plv_9de3adfeb71440cdb13804d68240b65c`. Die gespeicherte zugewiesene Version wurde erneut gelesen. `Stammkrümmungserkennung` bleibt gestoppt. Gleichzeitig läuft `Basler Beleimung` mit etwa 8,9 verarbeiteten FPS; damit liegt keine isolierte Lastmessung vor.

Die neuen Ring- und Paarzähler sind vorhanden, ebenso die neuen historischen Ablehnungsgründe. Das bestätigt die Ausführung des erweiterten Diagnose-/Trackingpfads. Ein serverseitiger Hash aller ausgelieferten Dateien sowie effektive native Threadzahlen wurden nicht ausgelesen. Die Systemstatus-API verweigert diesem Zugang den Zugriff mit HTTP 401. Der Vergleich zum 14.09. ist ein Vergleich zweier Betriebsstichproben, kein kontrollierter A/B-Versuch derselben Bildfolge.

**1. Bildübernahme funktioniert weitgehend, Messdurchsatz bleibt unzureichend**

| Kennzahl | Vorher, 14.09. | Nach Update, 15.09. |
| --- | ---: | ---: |
| Gemeldete Quellrate, Median | ca. 19,6 FPS | **30,0 FPS** |
| Vollständige Pipeline, Median | ca. 5,2 FPS | **4,9 FPS** |
| Angezeigte Einzellatenz, Median | ca. 374 ms | **349 ms** |
| Einzellatenz, P95 | hier nicht erneut berechnet | **606 ms** |
| Angezeigte Verwerfungsrate, Median | ca. 71,6 % | **83,4 %** |

Die höhere Verwerfungsrate ist nicht gleichbedeutend mit schlechterem Kameraempfang: Der Executor übernimmt jetzt deutlich mehr Quellframes, verarbeitet aber weiterhin nur einen kleinen Teil vollständig. Der vollständige Durchsatz hat sich in dieser Stichprobe nicht verbessert. Eine kausale Verschlechterung durch einzelne Optimierungen lässt sich daraus wegen anderer Bildinhalte und Hintergrundlast nicht ableiten.

Neue kumulative Zähler, jeweils Differenz zwischen erstem und letztem verfügbaren Messwert:

| Zähler | Differenz in etwa 180 s |
| --- | ---: |
| Übernommene High-Rate-Quellframes | **5.399** |
| Überschriebene Ringsequenzpositionen | **2** |
| Beim Ringlesen übersprungene Sequenzpositionen | **0** |
| Als verarbeitet gezählte Phasenframes | **3.959** |
| Durch Ziel-FPS-Begrenzung ausgelassen | **930** |
| Aus der Phasenqueue verworfen | **533** |
| Wegen Generation verworfene Frames | **17** |
| Wegen Altersgrenze verworfene Queueframes | **0** |
| Doppelte Frames | **0** |
| Zusätzliche Replay-Schritte | **139** |
| Exakte Geometrie-/Rohphasenpaare mit verriegelter Phase | **82** |
| Vom Slice-Builder akzeptierte Profil-/Phasenpaare | **35** |
| Abgelaufene Paarwartezeiten | **148** |
| `high_rate_phase_processing_failures` | **128** |

Die Zähler sind keine disjunkte Mengenbilanz: Seedverarbeitung, Generationwechsel, Replay und unterschiedliche Veröffentlichungszeitpunkte beeinflussen ihre Zuordnung. Insbesondere zählt `processed_frames` auch Bearbeitungen ohne gültige Phase; daraus dürfen keine gültigen Winkelbeobachtungen abgeleitet werden. Der gleitende Phasen-FPS-Wert liegt im Median bei 24,5 FPS, die Zählerdifferenz entspricht ungefähr 22 Bearbeitungen/s. Beides ist deutlich von den nur 35 akzeptierten Profilpaaren im ganzen Fenster zu unterscheiden.

Der Fehlerzähler bedeutet außerdem nicht 128 Python-Ausnahmen: `HighRateStemPhaseBridge._process()` zählt auch `PHASE_SEED_HISTORY_EXPIRED` über `record_processing_failure()`. Ohne weitere Aufschlüsselung lässt sich die Zahl nicht auf technische Ausnahmen reduzieren.

**2. Höchste Korrekturpriorität: Wiederanlauf der Phasenspur**

In 424 angezeigten Zuständen mit Phasenmetadaten waren **387 `LOST`** und **37 verriegelt sowie kalibriert gültig**. Die beobachteten Hauptgründe sind:

| Phasengrund | Angezeigte unterschiedliche Zustände |
| --- | ---: |
| `PHASE_SEED_STALE` | **214** |
| `PHASE_SEED_HISTORY_MISSING` | **96** |
| `PHASE_PAIR_DEADLINE_EXPIRED` | **77** |

Die aktive Konfiguration verwendet 500 ms maximales Seedalter, 500 ms Replayfenster, 16 Replayframes, drei Queueplätze und 25 Ziel-FPS. Das gemeldete Alter des zuletzt angewandten Seeds beträgt im Median **2,31 s**, maximal **8,36 s**. Das ist kein Nachweis einer entsprechend langen Kamerawarteschlange: Dieser Wert kann weiterwachsen, weil neu eintreffende Seeds nicht mehr angewandt werden.

Im aktuellen Code wurde folgender Ablauf mit echten synthetischen Bildarrays und dem produktiven Tracker lokal reproduziert:

1. Seed 1 initialisiert eine gültige Spur.
2. Frame 2 liegt 600 ms später. `_advance()` überschreitet die 500-ms-Grenze und erzeugt `PHASE_SEED_STALE`.
3. Ein frischer Seed für genau Frame 2 trifft ein. `_apply_seed()` verlangt einen verriegelten historischen Zustand; der vorhandene Zustand ist bereits ungültig. Ergebnis: `PHASE_SEED_HISTORY_MISSING`.
4. Weitere frische Seeds für Frame 3 und 4 scheitern genauso. Intern bleibt Seed 1 erhalten; die Generation wechselt nicht.

Damit ist eine echte Wiederanlauflücke belegt, unabhängig von Bewegungsunschärfe oder einer zu kleinen Featurezahl. Die neue strikte historische Prüfung verhindert korrekt eine erfundene Fortsetzung des alten Winkels. Es fehlt aber ein sicherer Übergang zu einer neuen Spur, wenn diese Kontinuität nicht mehr hergestellt werden kann. Die lokale Reproduktion beweist diesen Mechanismus, nicht die Ursache jedes einzelnen Live-Ausfalls.

Relevante Stellen: `high_rate_phase.py`, `_PhaseTracker.process_frame()` ab Zeile 610, `_apply_seed()` ab Zeile 662, Prüfung um Zeile 698, `_advance()` ab Zeile 772 und `_hold()` ab Zeile 904.

Erforderliche Korrektur: Historische Fortsetzung nur bei nachgewiesener Kontinuität; bei endgültigem Verlust eine neue, ausdrücklich getrennte Phasengeneration initialisieren. Alte und neue Winkelnullpunkte dürfen nicht in einem Slice-Modell zusammengeführt werden. Ein Reset allein liefert noch keine gültige Endkorrektur. Wiederanlauf, Slice-Invalidierung und erneute Mindestabdeckung müssen gemeinsam getestet werden. Ein bloßes Erhöhen der Altersgrenze oder Rückgriff auf einen neueren Snapshot ist keine belastbare Lösung.

**3. Zweite Priorität: rechtzeitige Seeds und exakte Zuordnung unter Last**

Die Kamera liefert 30 FPS, der Phasenpfad ist auf 25 FPS begrenzt. Ein Pipelineframe kann deshalb zu einer Sequenz gehören, die im Phasenpfad ausgelassen wurde. Der aktuelle Code ergänzt dessen echtes Seedbild, wenn es noch vor dem Trackingfortschritt liegt. Ist der Worker bereits weiter und existiert kein exakter verriegelter historischer Zustand, wird die Zuordnung verworfen. Das ist sicher, reduziert aber die nutzbaren Paare.

Gleichzeitig können Geometrie und nachfolgende Seedverarbeitung das 500-ms-Fenster überschreiten. 148 Paar-Timeouts belegen, dass zeitgerechte Zuordnung weiterhin ein praktisches Problem ist. Nicht alle Timeouts lassen sich mit der API einem einzelnen Subschritt zuordnen.

Für die Korrektur sollten echte Rohframehistorie und ausgewählte Pipeline-Sequenzen gemeinsam betrachtet werden: Ein benötigter Seedframe muss entweder rechtzeitig im geordneten Phasenpfad berücksichtigt oder aus einem vollständig vorhandenen zulässigen Vorgängerzustand mit echten Zwischenbildern replayt werden. Fehlende historische Zustände bleiben ungültig. Die Queuegrenze und Deadline werden dabei nicht pauschal erweitert. Eine Anhebung auf 30 Phasen-FPS wäre wegen der bereits vorhandenen 533 Queueverwerfungen allein noch kein nachgewiesener Gewinn.

**4. Dritte Priorität: Geometrie und CPU-Konkurrenz messen**

Zeiten abgeschlossener Nodes; Median hier als gewöhnlicher statistischer Median der deduplizierten Stichprobe berechnet:

| Node | Median vorher | Median nachher | P95 nachher |
| --- | ---: | ---: | ---: |
| `stem_geometry` | 106 ms | **174 ms** | **370 ms** |
| `measure_curvature` | 152 ms | **137 ms** | **260 ms** |
| `track_surface_phase` | 35 ms | **42 ms** | **129 ms** |
| `segment_stem` | 40 ms | **54 ms** | **178 ms** |
| `build_slice_model` | 11 ms | **7 ms** | **41 ms** |

`stem_geometry` verwendet bereits `phase_tracking_fast`. Diese Stufe ist trotzdem der größte gemessene Einzelposten. Die Krümmungsstufe liegt etwas niedriger als zuvor; ein genereller Beschleunigungsnachweis ist damit nicht erbracht. Die Phasen-Node-Zeit umfasst auch Seedaufbereitung und Warten und ist nicht identisch mit der Optical-Flow-Rechenzeit des Workers. Node-Mediane dürfen wegen Parallelisierung nicht einfach zur Endlatenz addiert werden.

Aktiv sind zwei Pipelineframes gleichzeitig; die Runtime meldet globale Inferenzparallelität vier und Parallelität pro Release zwei. Ob native Threads begrenzt sind, ist über diesen Zugang nicht nachgewiesen. Als nächster kontrollierter Leistungsversuch ist deshalb weiterhin die dokumentierte Kombination aus einem/zwei Pipelineframes und einem/zwei nativen Threads sinnvoll. Messgröße sind rechtzeitige akzeptierte Paare je Rollvorgang, zusätzlich Subschrittzeiten von Geometrie, Seedaufbereitung und Tracking. Ein kontrollierter Replaylauf benötigt eine identische aufgezeichnete Bildfolge; solche Live-Konfigurationsänderungen wurden hier nicht vorgenommen.

**5. Das fachliche Ziel ist weiterhin nicht erreicht**

Beobachtet wurden **42 Session-IDs**, davon 11 mit einem gesehenen `result_ready`, **keine mit gesehenem `result_valid`**. Session-IDs sind keine nachgewiesene Anzahl physischer Stämme. Pro beobachteter Session lag die höchste gesehene Zahl akzeptierter Beobachtungen an einer Materialstation im Median bei **eins**, maximal bei **vier**. Konfiguriert sind mindestens **sieben** Beobachtungen, drei unterschiedliche Phasen und 60° Phasenspanne.

Bei 4,9 vollständigen Pipelineframes/s stehen rechnerisch im Mittel nur rund **2,5 Frame-Ergebnisse pro 0,5 s** zur Verfügung, noch vor Qualitäts- und Zuordnungsablehnungen. Das ist keine direkt gemessene Burststatistik, zeigt aber die Größenordnung des verbleibenden Durchsatzdefizits. Die 35 akzeptierten Profilpaare des gesamten Fensters dürfen ebenfalls nicht gleichmäßig auf reale Rollvorgänge verteilt werden.

`CURVATURE_INVALID` und `INPUT_BINDING_CHANGED_RESET` stehen in fast allen angezeigten Ergebnisgründen. Der Slice-Builder führt jedoch eine `reason_code_history`; diese Werte belegen daher weder eine in jedem Frame ungültige aktuelle Krümmung noch einen Reset in jedem Frame. Da `measure_curvature.debug_overlay=false` und `include_raw_observations=false` aktiv sind, bleibt die genaue aktuelle Krümmungsablehnung im bildlosen Probeformat unvollständig. Die kanalspezifische Ergebnishistorie ist weiterhin deaktiviert.

Die Erhöhung im realen Rollweg bleibt eine zusätzliche physikalische Modellaufgabe. Die hier reproduzierte Wiederanlauflücke und der zu geringe Messdurchsatz bestehen bereits unabhängig davon. Aus dieser Messung lässt sich noch keine belastbare horizontale oder vertikale Zielkorrektur in Grad freigeben.

**Nächste Umsetzung in Reihenfolge des erwarteten Nutzens**

1. Wiederanlauf mit neuer Generation und sicherer Trennung der Messhistorie korrigieren; den reproduzierten Stale-/Reseed-Zyklus als Regression aufnehmen.
2. Echte Seedsequenzen unter Ziel-FPS-Begrenzung, Queueverlust und verspäteter Geometrie rechtzeitig zuordnen; exakte historische Prüfung erhalten. Deadline-Abläufe getrennt von technischen Ausnahmen zählen.
3. Geometriesubschritte und native Threadkonkurrenz anhand derselben Aufnahme vergleichen; die Einstellung mit den meisten rechtzeitigen gültigen Paaren wählen.
4. Anschließend eine vollständige reale Rollsequenz einschließlich Erhöhung und Winkelreferenz auswerten. Prozessidentität, nutzbarer Materialabschnitt, Winkelabdeckung und rechtzeitige Entscheidung bleiben Abnahmekriterien.

**Nachweise**

Es wurde ausschließlich dieser Bericht ergänzt; Produktionscode und Serverkonfiguration wurden in diesem Analyseauftrag nicht verändert. Lokale Diagnoseartefakte liegen unter `.test-tmp/stem-postupdate-20260915/`: `live.jsonl`, `summary.json`, `analysis.json`, `live-initial.json`, `channel.json`, `status.json`, `channels.json`, `definition.json`, `active-version.json` und `recovery-reproduction.json`. Der API-Schlüssel wurde ausschließlich über die Prozessumgebung übergeben und nicht in diesen Dateien gespeichert.

Die Wiederanlaufreproduktion liegt in `.test-tmp/stem_postupdate_recovery.py`; sie verwendet die vorhandenen lokalen Testabhängigkeiten. Sämtliche Assertions der Reproduktion bestehen: anfänglich verriegelte Phase, anschließend drei trotz frischer Seeds ungültige Zustände bei unverändertem internem Seed und unveränderter Generation. Das ist ein gezielter Fehlernachweis, keine erneute vollständige Regressionstestfreigabe.
