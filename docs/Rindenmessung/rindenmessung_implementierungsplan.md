# Rindenmessung: Implementierungsplan

## Ziel und Umfang

Eigene Runtime-Vorlage **Rindenmessung – zwei Stirnflächen** im bestehenden Inferenz
Server. Manueller gemeinsamer Upload eines zusammengesetzten Fotos und der CSV;
separate metrische Registrierung jeder Stirnfläche aus Foto und CSV, radiale
Rindenmessung und Mittelwerte je Seite und für den Stamm. Vorwiegend veränderlicher
Aufnahmeabstand bei ausreichend konstanter Blickrichtung. Echte rindenfreie Stellen
zählen als 0 mm. Ziel: höchstens 0,5 mm absolute Abweichung des Seitenmittelwerts;
Nachweis durch unabhängige Referenzmessungen auf dem Zielserver erforderlich.

## Verbindliche Seitenzuordnung

Erforderliche Auswahl im Bildaufteilungs-Node:

| Auswahl | Links | Rechts |
|---|---|---|
| Links = erste Scheibe | erste CSV-Scheibe | letzte CSV-Scheibe |
| Links = letzte Scheibe | letzte CSV-Scheibe | erste CSV-Scheibe |

Keine automatische Ableitung aus Größe oder Stock/Zopf. Neue Vorlage ohne
bestätigte Zuordnung; Auswahl vor Ausführung erforderlich. Versionierte Vorgabe
und explizite Auftragsüberschreibung; die Überschreibung verändert die Vorlage
nicht. Links/rechts beziehen sich auf das unveränderte Eingabebild. Bildbereiche
und Trennbereich grafisch einstellbar; Drehung und Spiegelung separat je Seite.
Erste/letzte Scheibe sind erste/letzte Datenzeile; streng aufsteigende Positionen,
kein stilles Sortieren. Ergebnis enthält Bildseite, Scheibenrolle, CSV-Zeile und
Position; Stock/Zopf ist zusätzliche Information.

## Runtime und Messverfahren

Neue zustandslose Nodes gemäß bestehender Handler-/Payload-/Registry-Konvention:

- `source.stem_csv_measurements`: CSV lesen und validieren.
- `preprocessing.endface_pair_split`: Bildbereiche und Scheiben zuordnen.
- `preprocessing.endface_metric_rectify`: Bild-/CSV-Konturen registrieren.
- `analysis.bark_radial_thickness`: radiale Einzelmessungen und Statistik.
- `analysis.bark_pair_result`: beide Seiten zusammenführen.

Zwei explizite Zweige: Bildbereich und Scheibe → bestehende
`inference.segmentation` (gesamte Schnittfläche einschließlich Rinde, ohne
Mantelfläche) → metrische Registrierung → bestehende semantische Segmentierung
→ radiale Messung → gemeinsames Ergebnis.

CSV gemäß `Messdatenexport csv - Datei.pdf`: `HFA_KRUEMMUNG_V1`, sechs Kopfzeilen,
Semikolon, Dezimalpunkt, maximal 70 Scheiben, 73 Werte je Datenzeile (Position und
36 X/Y-Paare). Position cm → mm; X/Y bereits mm. Kopfmerkmale übernehmen.
Feldzahlen, endliche Werte, Reihenfolge und Polygongeometrie prüfen. Fehlerhafte
Endscheiben nicht durch innere ersetzen.

Registrierung: vollständige Konturen, mehrere Rotations-/Konturanfangskandidaten,
affine Initialisierung und projektive Verfeinerung; beidseitiger Konturabstand,
Abdeckung, Stabilität, alternative Lösungen und Konturperturbationen prüfen.
Mehrdeutige Kreis-/Ellipsenfälle ungültig; kein Rückfall auf frühere Kalibrierung.
Gleiches X/Y-Raster, standardmäßig 0,25 mm/Pixel; Hochskalierung erhöht nicht die
Messauflösung. CSV liefert Maßbezug, Bildmaske liefert feine Außenkontur.

Segmentierungsvertrag rückwärtskompatibel um optional angeforderte verlustfreie
Klassenmasken einschließlich Löchern und Komponenten erweitern. Modellskalierung
und Padding korrekt zurückführen; Klassen Holz, Rinde, Hintergrund und
unklar/verdeckt konfigurieren. Training und Runtime verwenden dieselben
ausgerichteten Ansichten über vorhandene Capture-/Label-Studio-Pfade.

Radiale Messung: Schwerpunkt der metrischen CSV-Kontur muss im Holz liegen;
standardmäßig 360 gleichwinklige Strahlen. Abstand äußerer Holzrand zu äußerem
Schnittflächenrand bei bestätigter Rinde; sicher rindenfrei = 0 mm. Verdeckung,
unklare Klassen und mehrdeutige Durchtritte ungültig, Innenrisse kein Rindenbeginn.
Mindestens 90 % gültige Strahlen, keine ungültige Winkellücke > 10°. Mittelwert
gültiger Strahlen einschließlich Nullwerte. Gesamtwert gleich gewichtetes Mittel
beider gültiger Seiten; sonst null. Einzelwerte speichern; Min/Max zuschaltbar,
standardmäßig Mittelwert. Ungültig ist null, nicht 0 mm.

## Oberfläche, Verträge und Speicherung

Gemeinsamer Upload, Zuordnung, Ausführung und Diagnose in bestehender Admin-Web-App.
Bestehender Triggervertrag für Bild und versionierte CSV-Zusatzdaten; Preview
erhält optionale Auftragsdaten. Typisierte Scheiben-, Registrierungs- und
Mess-Payloads; Ergebnis `stem.bark-measurement.v1` mit Zuordnung, Einzelwerten,
Statistik, Qualität und Modell-/Transformationsprovenienz.

Original, Freistellung, CSV-Overlay, metrische Ansicht, Klassenmasken, Strahlen und
Umfangsdiagramm anzeigen. Seitenfehler unabhängig darstellen. Bestehende
Artefakt-/Ergebnishistorie-/PostgreSQL-Pfade verwenden; keine Auftragsdateien in
Pipelineversionen. OpenAPI und Frontend-Schemas regenerieren. Keine neue Anwendung,
separate Datenbank oder SPS-Ausgabe; auftragsfremde Änderungen unangetastet lassen.

## Umsetzung und Verifikation

Reihenfolge: Plan → CSV/Zuordnung → Registrierung → Maskentransport/Messung →
Runtime-Vorlage → Oberfläche/Ergebnishistorie → Prüfungen.

Tests: beide Zuordnungen und Overrides, fehlende Auswahl, Spiegelung/Provenienz,
CSV-Einheiten/Reihenfolge/Fehler, bekannte Skalierung und Projektivität,
Mehrdeutigkeit, Abstand/Falschzuordnung/Anschnitt, variable/fehlende Rinde,
Risse/Verdeckung, Maskenlöcher/Padding, Mittelwerte/Nullwerte/Teilergebnisse,
bestehende Segmentierungsverträge und Vorlagen. Backend-Unit-Tests,
Frontend-Tests, Typprüfung, Build und Schema-Prüfungen lokal.

Live- und messtechnische Tests auf Zielserver: unabhängige radiale Referenzen über
den vorgesehenen Abstandbereich, ≤ 0,5 mm Seitenmittelwertabweichung,
Wiederholbarkeit und Ablehnungsquote dokumentieren. Bis zum Nachweis Kennzeichnung
**noch nicht messtechnisch freigegeben**.

## Umsetzungsstand

Stand: **15.09.2026 – Software implementiert und lokal geprüft.** Die reale
Messgenauigkeit ist noch nicht abgenommen; es wurde kein Deployment durchgeführt.

### Implementierte Bestandteile

| Bereich | Umsetzung |
|---|---|
| Verträge | `cv_contracts.bark`, optionale verlustfreie PNG-Klassenmasken im bestehenden Segmentierungsvertrag, optionale Auftragsdaten im Preview. |
| CSV und Geometrie | `cv_runtime.bark_measurement`: strikter HFA-Parser, Polygonprüfung, Konturregistrierung, radiale Messung und Statistik. |
| Runtime | Alle fünf Nodes in `cv_runtime.nodes.bark`, typisierte Payloads und reguläre Registry-Einträge. Keine auftragsübergreifende Kalibrierung. |
| Vorlage | `template-trigger-bark-measurement`, zwei explizite Zweige, zwei gemeinsame Modellbindungen, vorhandene Ergebnis- und Bildausgabe. |
| Editor | CSV als zusätzliche Datenquelle neben genau einer Bildquelle; Ergebnisfelder im vorhandenen Ausgabekatalog. Die vollständig gebundene Vorlage besteht die Veröffentlichungsvalidierung. |
| Bedienung | Gemeinsamer Upload, ausdrückliche Zuordnung und Auftragsüberschreibung, grafische Rechtecke, separate Drehung/Spiegelung, Entwurfsprüfung und gespeicherter Auftrag. |
| Auswertung | Seitenmittelwerte, gleich gewichteter Stammwert, echte Nullwerte, optional Min/Max, Einzelstrahlen, Umfangsdiagramm und verständliche Ablehnungsgründe. |
| Historie | Original, CSV, Klassenmasken, Freistellung, CSV-Overlay, metrische Bilder, Messdiagnose und vollständige Ausführungsprovenienz unter dem bestehenden Runtime-Ergebnisspeicher. |
| Zugriff und Aufbewahrung | Artefaktzugriff mit bestehender Ergebnis-Leseberechtigung; SHA-256-Dateimanifest und Dateibereinigung über den vorhandenen Maintenance-Worker. |
| Schnittstellen | OpenAPI aus den bestehenden Apps exportiert, Frontend-Schemata und API-Client synchron erweitert. Keine neue Datenbank oder Migration erforderlich. |

Die neue Vorlage verwendet **120 Sekunden Auftragslimit**, da die mehrfach
initialisierte Registrierung mit zwei Seiten das bisherige Maximum von 30 Sekunden
überschreiten kann. Der bestehende Triggervertrag und dessen Konfigurationsgrenze
wurden dafür rückwärtskompatibel erweitert; bestehende Timeout-Vorgaben bleiben
erhalten. Das Limit einzelner Inferenzaufrufe bleibt separat konfiguriert.

Die Registrierung prüft den beidseitigen mittleren Konturfehler und begrenzt
zusätzlich den maximalen Abstand entlang der Konturen. Sie prüft konkurrierende
Abbildungen sowie zwölf deterministische Konturperturbationen mit 0,5 Pixel
Standardabweichung. Kreis-/Ellipsenkonturen werden bereits vor der Lösungsauswahl
abgelehnt. Die mittlere Rindenstärke wird zusätzlich unter den alternativen
Abbildungen auf Stabilität geprüft. Diese Prüfungen sind **keine messtechnische
Zertifizierung**.

### Bedienung nach dem Update

1. Im bestehenden Kanal die Vorlage **Rindenmessung – zwei Stirnflächen** instanziieren.
2. Das Modell für die gesamte Schnittfläche und das semantische Holz-/Rindenmodell
   binden. Die Bindungsgruppen verwenden dasselbe jeweilige Modell für beide Seiten.
   Standardklassen: `endface` sowie `wood`, `bark`, `background`, `unknown`;
   abweichende Modellklassen in den Ausrichtungs-/Mess-Nodes einstellen.
3. **Rindenmessung · Foto und CSV hochladen** öffnen. PNG/JPEG und passende HFA-CSV
   gemeinsam auswählen; maximale Uploadgrößen in der Oberfläche: 5 MB / 1 MB.
4. Die Pipeline-Zuordnung ausdrücklich setzen. Im Originalbild beide Rechtecke
   zeichnen; der Abstand zwischen den Rechtecken bildet den ausgeschlossenen
   Trennbereich. Anschließend Drehung und Spiegelung je Bereich einstellen.
5. Den Entwurf prüfen. Zum Speichern von Aufträgen die Pipeline speichern und
   veröffentlichen; verwendet wird der aktive Endpunkt genau dieser Version.
6. **Auftrag ausführen und speichern** starten. Eine ausdrücklich gewählte
   Auftragsüberschreibung wirkt nur auf diesen Auftrag. Ergebnis und Artefakte
   stehen anschließend auch in der bestehenden Ergebnishistorie bereit.

Ungültige Geometrie oder unzureichende Messqualität einer Seite lässt die andere
Seite sichtbar. Ein gemeinsamer Stammwert entsteht nur bei zwei gültigen Seiten.
Technische Modell-/Eingabefehler werden über die bestehenden Runtime-Fehlerpfade
ausgegeben. Eingaben mit gültigem Auftragsvertrag werden auch bei einer Ablehnung
für die spätere Prüfung aufbewahrt.

### Training und tatsächliche Messauflösung

Die Modelle sind über den vorhandenen Modellkatalog bereitzustellen. Diese Änderung
enthält keine neu trainierten Gewichte. Für die zweite Segmentierungsstufe die
metrischen `frame`-Ausgänge der beiden Ausrichtungs-Nodes als Trainingsbilder
verwenden: über vorhandene Capture-/Label-Studio-Nodes oder die gespeicherten
metrischen Artefakte. Nur erfolgreich registrierte Ansichten übernehmen. Bei
Änderungen von Raster, Freistellung oder Modellvorverarbeitung passende Trainings-
und Validierungsdaten erneut erzeugen.

Modellmasken bleiben auch mit Innenlöchern und mehreren Komponenten verlustfrei.
Die native Maskenauflösung wird mitgeführt; eine hochskalierte, ursprünglich grobe
Maske wird deshalb nicht als fein aufgelöste Messung akzeptiert. Standardmäßig
wird eine ursprüngliche Pixelgröße über 0,5 mm zurückgewiesen. Modellauflösung,
Segmentierungsfehler, Scannerfehler und optische Verzerrungen müssen dennoch in
die reale Genauigkeitsbewertung eingehen.

### Erweiterung: Trainingsaufnahmen an mehreren Pipeline-Stufen

**Implementiert am 15.09.2026.** Zwischenbilder lassen sich in der bestehenden
Admin-Web-App gezielt für Label Studio aufnehmen. Die zwei Stufen erhalten
unterschiedliche Trainingsprojekte; links und rechts können jeweils in dasselbe
Projekt ihrer Stufe aufgenommen werden.

| Trainingsdaten | Bildausgang | Label-Studio-Projekt |
|---|---|---|
| Linke, noch nicht metrisch ausgerichtete Stirnfläche | `split_faces.left_frame` | Schnittfläche einschließlich Rinde – Modell 1 |
| Rechte, noch nicht metrisch ausgerichtete Stirnfläche | `split_faces.right_frame` | Schnittfläche einschließlich Rinde – Modell 1 |
| Metrisch ausgerichtete linke Stirnfläche | `left_metric.frame` | Holz / Rinde / Hintergrund / unklar – Modell 2 |
| Metrisch ausgerichtete rechte Stirnfläche | `right_metric.frame` | Holz / Rinde / Hintergrund / unklar – Modell 2 |

**Bedienung in der Rindenvorlage:**

1. Vorlage in einem Kanal öffnen; Foto und CSV laden, Seitenzuordnung und
   Bildbereiche konfigurieren.
2. „Trainingsbilder vor einem nachfolgenden Modell vorbereiten“ öffnen.
   „Beide Stirnflächen aufteilen · Training Modell 1“ wählen und
   „Bis zur Trainingsstufe vorbereiten“ ausführen. Dafür werden keine
   Segmentierungsmodelle benötigt.
3. Unter „Trainingsbild für Label Studio aufnehmen“ die linke beziehungsweise
   rechte aufgeteilte Ansicht wählen, das Trainingsprojekt auswählen und
   „Dieses Bild für Label Studio aufnehmen“ betätigen. Jede Aufnahme ist ein
   eigener, ausdrücklich ausgelöster Vorgang.
4. Modell 1 mit diesen Bildern trainieren und über den vorhandenen Modell- und
   Deploymentpfad an die beiden Kontursegmentierungs-Nodes binden.
5. Als Vorbereitungsstufe `left_metric` beziehungsweise `right_metric` wählen.
   Es werden nur die ausgewählte Stufe und deren Vorgänger ausgeführt. Das
   semantische Modell 2 muss dafür noch nicht vorhanden sein. Die andere
   Stirnseite bei Bedarf in einem weiteren Vorbereitungslauf ausrichten.
6. Jeweils die erfolgreich ausgerichtete Ansicht für das zweite Trainingsprojekt
   aufnehmen. Modell 2 anschließend mit diesen Ansichten trainieren und binden.

Nach vollständiger Vorschau oder Auftragsausführung stehen ebenfalls die
enthaltenen Zwischenbilder zur Auswahl. Im allgemeinen Pipeline-Editor liefert
ein manueller „Schritt testen“-Lauf die Bilder unter
„Trainingsbilder aus getesteten Pipeline-Stufen“. Die Projektauswahl wird während
der geöffneten Ansicht getrennt je Bildausgang gehalten. Sie legt keine
automatische Aufnahmeregel an.

**Automatische Aufnahme über vorhandene Nodes:**

- Pro gewünschtem Bildausgang einen `capture.full_frame_labelstudio`-Node als
  zusätzlichen Zweig verbinden; das passende synchronisierte Trainingsprojekt
  im vorhandenen Projektauswahlfeld konfigurieren.
- Nach dem Split die beiden Frame-Ausgänge jeweils direkt anschließen.
- Nach jeder metrischen Ausrichtung deren `frame` und neuen `eligibility`-Ausgang
  mit den gleichnamigen Capture-Eingängen verbinden; `require_eligibility=true`
  setzen. Eine ungültige Ausrichtung erzeugt keine Aufnahme. Eine bildbezogene
  Freigabe eines anderen Zweigs wird ebenfalls zurückgewiesen.
- Vorschau und Trainingsvorbereitung lösen keine automatischen Label-Studio-
  Importe aus. Automatische Capture-Nodes arbeiten bei regulären gespeicherten
  Aufträgen über die vorhandene Capture-Warteschlange.

**Verträge und Herkunft:** Der bestehende manuelle Einzelaufnahmevertrag wurde
optional um `pipeline_image` erweitert: Node, Ausgang, gegebenenfalls gespeicherte
Pipelineversion, exaktes Bild, Bildkoordinaten und Freigabe. Der Server prüft
Dateigröße, Bildformat, Dimensionen, Dekodierbarkeit und die Zuordnung der
Freigabe zum Bild. Der Capture-Worker verarbeitet diesen gespeicherten Snapshot
ohne Rückgriff auf ein späteres Livebild. SHA-256, Herkunft der ausgewählten
Ansicht, Transformation und Kalibrierungsmetadaten werden über die bestehenden
Capture-/Label-Studio-Metadaten transportiert. Entwurfsvorschauen behaupten keine
gespeicherte Pipelineversion. Dies ist eine vom Bediener ausgewählte Ansicht,
keine kryptografische Bestätigung der Ausführung.

Die Preview kann optional mit `include_execution_result` ihre Zwischenbilder
zurückgeben. Wiederholte automatische Editor-Vorschauen fordern diese zusätzliche
Antwort nicht an. Intervallaufnahmen bleiben für fortlaufende Quellen vorgesehen;
ein ausgewählter Snapshot wird als Intervallquelle abgewiesen. Bestehende
Live-Aufnahmen, Capture-Nodes, Aufnahmestatus und Wiederholungsversuche bleiben
über ihre bisherigen Pfade erreichbar. Keine zusätzliche Datenbank oder Migration.

Ungültige metrische Ansichten bleiben für Trainingsaufnahmen gesperrt;
Konturüberlagerungen, Freistellungsdiagnosen und Messstrahlen werden für diese
Stufe nicht als Modell-2-Eingabe angeboten. Die geometrische Capture-Freigabe
ändert nichts an der noch offenen messtechnischen 0,5-mm-Abnahme.

**Prüfungen dieser Erweiterung:**

- 63 Backendtests bestanden: 14 neue Tests für Snapshot-Validierung, exakte
  Bildübernahme, Freigabezuordnung, Capture-API/Worker/Label-Studio-Testdouble und
  Modell-1-Datenerzeugung über die Preview-API; zusätzlich 17 bestehende manuelle
  Capture-Tests, 30 Rindenmessungstests und zwei bestehende Preview-Tests.
- Der erste Capture-Regressionslauf wurde auf diesem Rechner durch die
  bestehende Speicherplatzreserve blockiert. Die Zustellungstests liefen danach
  mit simuliertem ausreichendem freien Speicherplatz erfolgreich. Der
  Anwendungscode für Speicherreserve und Quoten wurde nicht verändert.
- 50 gezielte Frontendtests bestanden, einschließlich fünf neuer Tests für
  getrennte Trainingsstufen, Bild-/Kontexterhalt und gesperrte Ausrichtungen.
- TypeScript-Typprüfung und Produktionsbuild bestanden; die bestehende Warnung
  über große JavaScript-Bundles bleibt bestehen.
- Sieben bestehende Vertragstests und Ruff für die beiden neuen Python-Dateien
  bestanden. Der exportierte Runtime-Admin-OpenAPI-Snapshot stimmt mit der
  bestehenden App überein; die drei erweiterten Request-Schemas wurden geprüft.

**Zielserverprüfung für Trainingsaufnahmen:** Die vier Ansichten mit echten
Foto-/CSV-Aufträgen erzeugen, die zwei Trainingsprojekte auswählen, Lieferung
und Aufnahmestatus prüfen. In Label Studio Bildorientierung, Ausschnitt,
metrisches Raster, Quell-Node und Kalibrierungsmetadaten kontrollieren. Zusätzlich
ungültige Ausrichtung, eine Seite ohne Freigabe, automatische Capture-Zweige,
Label-Studio-Unterbrechung und Zustellung nach Wiederanlauf prüfen. Diese Live-
Prüfung wurde auf dem Entwicklungsrechner nicht ausgeführt.

### Ausgeführte lokale Prüfungen

- **29 Rindenmessungs-Backendtests bestanden**, einschließlich vollständiger
  synthetischer Foto-/CSV-Pipeline mit zwei Aufnahmeabständen, Modellmasken,
  Speicherung und Artefaktaufbewahrung. Weitere Fälle: beide Zuordnungen,
  Auftragsüberschreibung, Spiegelung/Drehung, CSV-Fehler, bekannte projektive
  Abbildungen, falsche Kontur, Ellipsenmehrdeutigkeit, Anschnitt, variable Rinde,
  rindenfreie Sektoren, Innenrisse, Verdeckung, zyklische Winkellücken und
  ungültige Teilergebnisse.
- **37 weitere Backendtests** für vorhandene Vorlagen, ausgewählte Preview-Nodes
  und parallele Pipelineausführung bestanden. Die Veröffentlichungsvalidierung
  der gebundenen Rindenvorlage wurde zusätzlich separat erfolgreich geprüft.
- **177 Frontendtests bestanden**; nach der letzten Ergänzung der CSV-Palettenregel
  außerdem alle **45 gezielten Editor-/Rindenmessungstests** erneut bestanden.
- **TypeScript-Typprüfung und Vite-Produktionsbuild bestanden.** Die bestehende
  Warnung über große JavaScript-Bundles bleibt bestehen.
- **Ruff für alle sieben neuen Python-Dateien bestanden.** Bestehende Dateien
  wurden nicht pauschal umformatiert.
- Acht betroffene JSON-Schemas geprüft; fünf betroffene OpenAPI-Snapshots stimmen
  mit der Schemaerzeugung der bestehenden Apps überein. Die Frontend-Verträge
  wurden entsprechend erweitert.
- **Sieben vorhandene Vertragstests bestanden**: Runtime-Grenzen und OpenAPI,
  öffentliche Inferenzverträge und Vollständigkeit der OpenAPI-Dokumentation.

Relevante Befehle in der eingerichteten Repository-Testumgebung:

```text
python -m pytest tests/unit/backend/test_bark_measurement.py -q
python -m pytest tests/unit/backend/test_runtime_pipeline_definition_system.py tests/unit/backend/test_runtime_selected_node_preview.py tests/unit/backend/test_runtime_parallel_node_execution.py -q
pnpm --filter @cv-platform/admin-web test
pnpm --filter @cv-platform/admin-web build
python scripts/dev/export_openapi.py inference-realtime.v1.yaml inference-batch.v1.yaml runtime-admin-api.v1.yaml channel-service.v1.yaml pipeline-definition-service.v1.yaml realtime-executor.v1.yaml trigger-executor.v1.yaml result-service.v1.yaml
```

Auf diesem Entwicklungsrechner erfolgten die Backendtests mit isolierten
Python-Abhängigkeiten, ohne laufende Anwendung. Keine Live-, Kamera- oder
Produktionsdatenbankprüfung.

**Vorhandene Regressionstest-Probleme:** Drei Tests in `test_inference_gateway.py`
scheitern auch mit dem unveränderten Gateway-Code aus `HEAD` unter derselben
Testumgebung: `test_realtime_triton_request_uses_realtime_timeout_budget`
(Testdouble ohne `headers`), `test_internal_realtime_timeout_is_retryable` und
`test_internal_segmentation_reports_backend_phase_timings` (jeweils HTTP 503).
Der Vergleich erfolgte im Arbeitsspeicher ohne Austausch von Repository-Dateien.
Das ältere Modul `test_runtime_pipeline_authoring_preview.py` lässt sich wegen
des bereits fehlenden Imports `_should_emit_mjpeg_snapshot` nicht sammeln.
Diese auftragsfremden Fehler wurden nicht durch Änderungen an bestehenden
Tests oder Produktionspfaden verdeckt.

### Noch offene Zielserverprüfungen und Abnahme

- Update über den vorhandenen Installer; Vorlage instanziieren, Modelle binden,
  Kanalveröffentlichung, manuellen Auftrag, Ergebnishistorie und berechtigten
  Artefaktzugriff mit tatsächlichen Diensten prüfen.
- Ausführungsdauer unter Zielserverlast, Speicherverbrauch und Aufbewahrung
  einschließlich Update/Rollback prüfen; Dateien liegen außerhalb der
  Pipelineversionen im persistenten Runtime-Ergebnisspeicher.
- Das Modell für die Schnittfläche muss vorhandene Rinde einschließen und die
  seitliche Mantelfläche zuverlässig ausschließen. Das zweite Modell muss
  Verdeckung/Unklarheit von sicher rindenfreien Stellen unterscheiden.
- Korrekte CSV-Zuordnung und tatsächlich vergleichbare Schnitt-/Messscheiben
  prüfen. Rein aus Konturen sind eine falsche, geometrisch gleichartige CSV
  oder deren gleichförmiger Maßstabsfehler nicht sicher erkennbar.
- Für jeden vorgesehenen Aufnahmeabstand unabhängige radiale Referenzmessungen
  an beiden Stirnseiten erfassen. Auch Blickrichtungsvariation, nicht ebene
  Schnittflächen und verbleibende Objektivverzerrung einbeziehen.
- **0,5-mm-Abnahme:** absolute Abweichung des Seitenmittelwerts zur unabhängigen
  Referenz höchstens 0,5 mm; Wiederholbarkeit, maximale Abweichung, Anzahl der
  Prüfobjekte und Ablehnungsquote dokumentieren. Die synthetischen Tests belegen
  die Softwarefunktionen, nicht diese reale Genauigkeit.

Bis zum erfolgreichen Nachweis tragen alle Ergebnisse weiterhin
**„noch nicht messtechnisch freigegeben“**. Ein erfolgreicher Softwarelauf oder
ein kleiner Konturfehler hebt diese Kennzeichnung nicht auf.
