# Edge-Leistung und persistente TensorRT-Engines

Stand: 21. September 2026. Die Erweiterung nutzt den bestehenden Edge-Agent,
dessen Health-/Runtime-Verträge, den Geräteabgleich und die vorhandene Admin-Web-App.

Seit 22. September 2026 gilt zusätzlich das
[begrenzte Edge-Speicherverhalten](edge-result-storage.md): Vorschauen bleiben im
RAM, und die Ergebnispersistierung erfolgt nur für aktivierte Historienprojektionen
im eingestellten Sampling-Abstand. FPS-/Node-Messungen benötigen keine Historie.

## Anzeige und Messung

Unter **Edge-Gerät → Betrieb → Edge-Leistung** erscheinen Statistiken je laufendem
Kanal. Derselbe Bereich ist unter **Kanal → Betrieb → Edge-Leistung und TensorRT-Cache**
erreichbar. Die Vorschau übernimmt die gemessenen FPS und die vorhandenen Node-Zeiten.

| Wert | Bedeutung |
|---|---|
| Verarbeitet / FPS | Erfolgreich abgeschlossene Pipeline-Ausführungen pro realer Sekunde; keine Ableitung aus der Inferenzlatenz |
| Kamera / FPS | Vom Kameraadapter gelesene Frames, nicht die konfigurierte Sensor-Framerate |
| Sampling | Bewusst durch `frame_sample_every_n` ausgelassene Frames; keine Behauptung über SDK-interne Frameverluste |
| Pipeline-P95 | 95. Perzentil der gemessenen vollständigen Graph-Ausführung |
| Verarbeitungszeiten | Mittelwert, P95, Maximum und letzter Wert für Kameraabruf, Pipeline, Ergebnispersistierung, angeforderte Vorschau und einzelne Nodes |
| Ausführungsfehler | Fehlgeschlagene Pipeline-Ausführungen und Exceptions des Graph-Aufrufs |

FPS verwenden ein gleitendes Zehn-Sekunden-Fenster mit 100-ms-Zählintervallen.
Laufzeiten verwenden höchstens 256 Messungen pro Schritt innerhalb derselben zehn
Sekunden, für maximal 256 Nodes. Die kumulierten Zähler gelten seit Kanalstart.
Bei Triggerkanälen zählt jede erfolgreiche Auslösung als Ausführung; das Lesen eines
Kamerabildes allein erhöht den Verarbeitungszähler nicht. Sowohl der gemeinsame
`cv_runtime`-Executor als auch bestehende E1-Graphen werden unterstützt.

Die Messung läuft auch ohne Viewer. Daten bleiben im Speicher des laufenden Workers
und werden beim Health-Abruf zusammengefasst. Es gibt keine neue Datenbank oder
zusätzlichen Ergebnisstrom. Der bestehende Worker-Heartbeat wird höchstens einmal
pro Sekunde in den bestehenden Store geschrieben, statt bei jedem Kameraabruf.
Kamerazeit enthält Wartezeit; parallel ausgeführte Node-Zeiten dürfen nicht einfach
zur Gesamtlatenz addiert werden. Übersprungene und fehlgeschlagene Nodes sind markiert.

Der vorhandene Geräteabgleich holt den Status standardmäßig alle 15 Sekunden.
Ohne neue Frames fallen die Raten innerhalb des Messfensters auf null. Bei einem
offline gemeldeten Gerät, abweichender Boot-ID oder einem mehr als 45 Sekunden alten
Gerätekontakt zeigt die UI keine FPS/Laufzeiten als aktuelle Werte an. Ältere Agents
bleiben lesbar und zeigen einen Hinweis auf fehlende Leistungsmessung.

## Engine-Wiederverwendung

Der bestehende Cache konnte bereits gleiche Builds aus anderen Workload-Releases
kopieren. Zusätzlich bleibt nun eine gemeinsame Kopie unter
`$YBB_EDGE_DATA_ROOT/engines/cache/<build-key>.plan` erhalten. Die bisherigen
releasebezogenen Engine-Pfade bleiben bestehen. Der Key enthält ONNX-Inhalt,
Eingabeformen, Präzision, Kalibrierungs-Digest, Builder-Modus und den bestehenden
Plattform-Namespace (Plattform, Architektur, TensorRT-Version, GPU-Modell).
Kameraeinstellungen, Kanal-/Release-IDs und Agent-Neustarts ändern diesen Key nicht.

Vor dem Laden des Graphen werden passende persistente Engines aufgelöst. Fehlende
Release-Kopien werden aus dem gemeinsamen Cache ergänzt. SHA-256-Sidecars erkennen
beschädigte oder leere Cache-Kopien; ein gültiges Duplikat wird bevorzugt, andernfalls
wird gebaut. Alte Cache-Dateien ohne Sidecar werden weiterverwendet und erhalten
einen Digest; die vorhandene TensorRT-Deserialisierung prüft ihre Verwendbarkeit.
Gleichzeitige Build-Anforderungen werden innerhalb des Agents serialisiert.

Die Geräteansicht zeigt je Modellschritt Cache-Nutzung, Vorbereitungsdauer und
Engine-Größe. Das Laden und Anlegen des Inferenzkontexts benötigt weiterhin Zeit,
auch wenn keine Kompilierung stattfindet. Der private Engine-Cache gehört ins
persistente Datenverzeichnis, nicht in einen austauschbaren Software-Slot.

## Einführung und Verifikation

Zuerst die Zentrale, danach die Edge-Runtime über die bestehenden Offlinepakete aktualisieren.
Die Oberfläche allein ergänzt keine Metriken auf einem alten Agent. Dafür ist ein
neues Edge-Softwarepaket erforderlich, auch wenn der `cv_runtime`-Digest gleich bleibt.
Es ist keine Datenbankmigration erforderlich.

Lokal geprüft: echte Supervisor-Ausführung mit Testquelle in Realtime-/Triggerbetrieb,
Sampling und zeitliches Verfallen, beide Timing-Formate, Vorschauübertragung mit
korrekten Nullzählern, Agent-Neustart ohne erneuten Builder-Aufruf, parallele
Build-Anforderungen, Release-Kopien, Cache-Beschädigung und notwendige Neubauten bei
geänderten Modellparametern. Frontend-Tests prüfen aktuelle/veraltete Werte,
Kanalzuordnung und ältere Agent-Antworten; TypeScript und Produktionsbuild laufen.
Die ältere M4-Testsuite benötigt das lokal fehlende Alembic; der geänderte
Vorschaupfad ist separat geprüft.

Auf dem Jetson noch abnehmen: denselben Kanal stoppen/starten und den Agent neu
starten; dabei Cache-Nutzung und fehlenden erneuten `trtexec`-Aufruf prüfen.
FPS und Laufzeiten mit realen Bildern beobachten, danach Sampling und Triggerbetrieb
vergleichen. Ohne Zielzugriff lässt sich die Ursache eines auf einem bereits
installierten Gerät beobachteten Neubaus nicht abschließend feststellen.
