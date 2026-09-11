Verbindliche Specification für den neuen Runtime-Teil


## 1. Ziel und Einordnung

Die Plattform wird um einen Runtime Vision Execution Layer erweitert.

Bestehende Plattform bleibt verantwortlich für:

Label Studio Integration
Dataset-Management
Training
Model Registry
Deployment
Inference Endpoints

Neuer Runtime-Teil wird verantwortlich für:

Kanalverwaltung
Quellenverwaltung
Live- und Triggerbetrieb
Pipeline-Ausführung
Live-Overlay
Ergebnisverarbeitung
Output-Weitergabe
Datenerfassung zurück in Label Studio

Der neue Runtime-Teil ist kein unabhängiges Fremdsystem, sondern ein zweiter operativer Anwendungsteil innerhalb derselben Plattform.


## 2. Architekturprinzipien

Der Runtime-Teil folgt diesen Prinzipien:

gleiche Plattform, aber getrennte operative Services
keine Vermischung von Training/Deployment mit Live-Ausführung
Realtime-Pfade getrennt von Trigger- und Hintergrundpfaden
Pipeline-Ausführung als modulare Node-basierte Verarbeitung
No-Code-Konfiguration als Standard
niedrige Latenz und kontrollierter Ressourceneinsatz
keine manuellen internen IDs
Erweiterbarkeit über klar definierte Node- und Connector-Typen
Operator denkt in Kanal, Quelle, Pipeline, Ergebnis, Output
interne technische Details bleiben verborgen


## 3. Hauptdomänenobjekte
Kanal

Ein Kanal ist die Source-/Kamera-Abstraktion. Er beschreibt eine
Quellverbindung, nicht einen Trigger-Endpunkt.

Ein Kanal hat:

Kanalname
technische Platzbezeichnung
Beschreibung
Quellentyp
Quellenkonfiguration
Source-Health und Source-Capabilities
Live-View-Konfiguration, falls anwendbar
optionale Default-/Live-Pipeline-Metadaten, falls fachlich gerechtfertigt
Datenerfassungsregeln
Output-Konfiguration
Status
Performance-/Health-Status

Ein Kanal ist nicht dasselbe wie ein Modell und nicht dasselbe wie ein
Trigger. Derselbe Kanal kann fuer Live-Visualisierung, manuelle
Snapshot-Erfassung, Datensammlung und mehrere triggerbare Pipelines verwendet
werden.

Quelle

Eine Quelle liefert Bilddaten fuer einen Kanal.

Unterstützte Quellentypen zum Start:

RTSP Stream
Snapshot-/Einzelbildquelle
Test-/Dateiquelle für Entwicklung
Trigger

Ein Trigger ist kein eigenes Primaerobjekt der Operator-Konfiguration. Ein
HTTP Trigger ist ein Pipeline-Ingress-Node. Endpoint-Metadaten duerfen intern
persistiert werden, werden aber aus einer publizierten/deployten Pipeline mit
HTTP Trigger Input abgeleitet.

Pipeline

Eine Pipeline ist eine gerichtete Verarbeitungskette aus Nodes.

Beispiel:
HTTP Trigger Input -> Channel Frame Input -> Vorverarbeitung -> Inferenz 1 -> ROI-Extraktion -> Inferenz 2 -> Berechnung -> Response Output

Node

Ein modularer Pipeline-Baustein.

Node-Kategorien:

Trigger/Input Nodes
Source Nodes
Preprocessing Nodes
Inference Nodes
Analysis/Computation Nodes
Capture Nodes
Output Nodes
Runtime Execution

Eine laufende Ausführung einer Pipeline auf einem Kanal.

Capture Rule

Eine Regel, wann und wie Bilder oder ROIs in Label-Studio-Projekte gespeichert werden.

Output Connector

Ein Ziel für Ergebnisse, zum Beispiel:

HTTP Response
Overlay
Speicherung
internes Event
Steuerungsantwort


## 4. Service-Schnitt des neuen Runtime-Teils
A. runtime-admin-api

Verantwortung:

Kanalverwaltung
Pipeline-Definitionen
Runtime-Konfiguration
pipeline-abgeleitete HTTP-Trigger-Endpunkt-Metadaten
Capture-Regeln
Ergebnis-/Statusabfragen
Live-Session-Metadaten
B. channel-service

Verantwortung:

Kanalstammdaten
Kanalstatus
technische Platzzuordnung
Kanal-zu-Pipeline-Zuordnung
Kanal-Lifecycle
C. pipeline-definition-service

Verantwortung:

Node-Typen
Pipeline-DSL
Pipeline-Validierung
Versionierung von Pipeline-Definitionen
Template-Pipelines
D. runtime-orchestrator

Verantwortung:

Start/Stopp von Kanal-Laufzeiten
Zuweisung an Realtime- oder Trigger-Executors
Worker-/Prozessverwaltung
Recovery
E. realtime-executor

Verantwortung:

RTSP / Live-Verarbeitung
Framefluss
Inferenzaufrufe
Overlay-Daten
Latenzoptimierter Pfad
F. trigger-executor

Verantwortung:

HTTP PUT Trigger-Annahme
Snapshot-Capture
Pipeline-Ausführung
Ergebnis im Response Body
deterministische Request/Response-Abarbeitung
G. overlay-service

Verantwortung:

Overlay-Datenmodell
Render-Vorbereitung
Stream-/Bilddarstellung
optionale Debug-Layer
H. capture-service

Verantwortung:

Bild-/ROI-Erfassung
Routing in ein oder mehrere Label-Studio-Projekte
Metadaten
Duplikat-/Rate-Control
Projektzuordnung
I. result-service

Verantwortung:

letzte Ergebnisse
Ergebnis-Historie
Roh-/Normalisierungsdaten
Weiterverarbeitungsresultate
J. connector-service

Verantwortung:

Output-Connectoren
Antwortformate
zukünftige Erweiterungen


## 5. Beziehung zur bestehenden Plattform

Der Runtime-Teil nutzt bestehende Plattformdienste, ersetzt sie aber nicht.

Er verwendet:

bestehende Benutzer- und Key-Verwaltung
bestehende Modell-/Deployment-Verwaltung
bestehende Inference-Gateway-/Triton-Schicht
bestehende Label-Studio-Integration als Ziel für Datenerfassung

Label Studio kann Bilder per Upload, URL oder Storage-Sync in Projekte bekommen; der Runtime-Teil soll diese Mechanismen nutzen oder passend abstrahieren. Webhooks aus Label Studio bleiben für eure Trainings-/Dataset-Seite relevant, sind aber nicht der Ausführungsmechanismus des Runtime-Teils.


## 6. Betriebsmodi
Realtime-Modus

Für Live-Stream-Betrieb.

Eigenschaften:

kontinuierliche Frame-Verarbeitung
Overlay im Livestream
Drop-Strategien statt Rückstau
niedrige Latenz
keine Queue im kritischen Pfad
pro Kanal/Quelle kontrollierbare Sampling-Strategie

Livestream-Publikation ist Teil der Runtime-Plane, nicht der Admin-UI.
Ein Channel-Runtime erzeugt pro aktivierter Variante hoechstens einen
kodierten Stream; mehrere Zuschauer werden ueber einen Media-Fanout bedient und
duerfen keine zusaetzliche Pipeline-, Inferenz-, Overlay- oder Encoding-Arbeit
ausloesen. Produktionspfad ist genau ein NVENC-H.264-Stream ueber internen
RTSP-Ingest, MediaMTX und WHEP/WebRTC mit TURN/TCP. HLS, MJPEG,
Snapshot-Player, WebRTC/UDP und CPU-Encoder sind keine Fallback-Pfade.

Stream-Autorisierung ist fuer LAN-Installationen standardmaessig deaktiviert
und muss in API/UI als `unauthenticated_lan` sichtbar sein, bis sie explizit
aktiviert wird. Overlay-Metadaten muessen `frame_id`, Zeitstempel,
`display_context_id`, Koordinatensystem und Aufloesung enthalten. Schwere
Masken/Heatmaps muessen kompakt kodiert werden. Der Browser zeichnet sie als
Canvas ueber dem einzigen Videostream; ein zweiter Overlay-Videostream ist
nicht zulaessig. Details:
`docs/runtime/livestream-streaming-architecture.md`.

Trigger-Modus

Für SPS-/Steuerungsauslösung. Trigger-Modus ist eine
Pipeline-Ausfuehrungsform, kein Kanaltyp.

Eigenschaften:

externer HTTP PUT auf einen aus der Pipeline abgeleiteten Endpunkt
HTTP Trigger Input startet die Pipeline
Channel Frame Input erhaelt ein Bild vom Kanal, auf den diese Pipeline angewandt wurde
Pipeline-Ausführung
Response Output erstellt den HTTP Response Body
definierte Timeouts
definierte Antwortstruktur
Intervall-Modus

Optionaler periodischer Snapshot-Betrieb.
Ein Interval Trigger Node startet die Pipeline periodisch und verwendet wie
HTTP Trigger den Channel Frame Input als kanalgebundene Bildquelle.


## 7. Pipeline-Node-Typen
Trigger/Input Nodes
HTTP Trigger Input

Source Nodes
Channel Frame Input
Channel Snapshot nur als Legacy-/Migrationsadapter
RTSP Input nur als Legacy-/Entwicklungsadapter
File/Test Input
Preprocessing Nodes
Resize
Crop
ROI Extract
Frame Sampler
Zone Filter / Zone Mask
image normalization if required by backend
Inference Nodes
Detection
Classification
Segmentation
Anomaly Inference
später weitere Modelltypen erweiterbar

Inference Nodes adressieren logische deployte Modelle, nicht interne Triton-Namen. Triton bleibt dahinter die Serving-Schicht mit modellweiser Konfiguration, Scheduling und ggf. Dynamic Batching. Dynamic Batching ist eher für Durchsatz als für harte Low-Latency-Pfade geeignet und soll deshalb nur dort aktiviert werden, wo es zur Pipeline passt.

`inference.anomaly` ist ein eigener Inference Node und verlangt ein deploytes
Modell mit `required_task_type=anomaly`. Er konsumiert einen Frame/Image
Context und liefert:

- `anomaly_score`
- Entscheidung `normal` oder `anomalous` anhand des Modell- oder Node-Thresholds
- Heatmap als echtes Modellresultat
- optionale Maske nur, wenn das deployte Modell Masken-Support deklariert

Anomaly darf nicht als Classification Node mit zwei Labels modelliert werden.
Die Runtime ruft den Inference-Gateway-Anomaly-Pfad auf und behandelt Heatmaps
als eigenes Overlay-Primitive.

Analysis / Computation Nodes

Zum Start:

Positionsbestimmung
Zonen-Eintritt / -Austritt
Optical Flow / Bewegungswert
Größenbestimmung
Objektzählung
Bewegungsrichtung
regelbasierte Ergebnisbildung
Capture Nodes
Full Image to Label Studio Project
ROI Image to Label Studio Project
Conditional Capture
Multi-Project Capture

Wichtig:
Ein Kanal kann Bilder für mehrere Label-Studio-Projekte sammeln.
Ein Kanal kann auch per ROI-Capture Daten für nachgelagerte Aufgaben erzeugen.

Output Nodes
Overlay Output
HTTP Trigger Response Output
Persisted Result Output
Internal Event Output
Write to Siemens SPS
später weitere Connectoren

`Write to Siemens SPS` (`output.siemens_sps_write`) ist ein terminaler
Runtime-Output-Node mit industriellem Side Effect. Er konsumiert erreichbare
Result-Payloads aus vorherigen Pipeline-Nodes und schreibt explizit
konfigurierte Werte in Siemens S7/SPS-DB-Adressen. Der Node wird im Output-
Bereich gruppiert als Industrial / PLC angezeigt und darf nur im Editor
erscheinen, wenn ein echter Runtime-Handler und Publish-Validierung vorhanden
sind.

Unterstuetzte v1-Ziele sind DB-Adressen und die Siemens-Datentypen `BOOL`,
`BYTE`, `WORD`, `DWORD`, `INT`, `DINT` und `REAL`. `STRING`, M/I/Q-Bereiche
und andere Feldbusse bleiben ausgeblendet, bis sie vollstaendig implementiert
und validiert sind. Adressbeispiele:

- `DB101.DBW0` fuer `INT`
- `DB101.DBD10` fuer `REAL`, `DINT` oder `DWORD`
- `DB101.DBX8.0` fuer `BOOL`
- `DB101.DBB12` fuer `BYTE`

Der Node verwendet eine strukturierte Zuordnungstabelle: Source Result Field,
DB-Nummer, Byte-Offset, optionaler Bit-Offset fuer `BOOL`, Datentyp,
Conversion-Modus, Fallback und Failure Behavior. Normale Bedienung verwendet
den upstream Result Field Picker; Advanced Paths sind nur Fallback fuer
dynamische Plugin-Felder. Beispiele:

- `classification.top1.label`: `gut -> 1`, `schlecht -> 0`, Ziel `DB101.DBW0 INT`
- `classification.top1.score`: Skalierung `0.0..1.0` auf `0..100`, Ziel
  `DB101.DBW10 INT`
- `measurement.distance_mm`: direkte oder skalierte Messwert-Ausgabe als `REAL`

Sicherheitsregeln:

- Node und einzelne Mapping-Zeilen haben explizite Enabled-Flags.
- Default ist `enabled=false` und `dry_run=true`.
- Validation, Publish-Pruefung und Editor Preview schreiben nie zur SPS.
- Reale Writes brauchen gueltige Config, gueltige Conversion, explizites
  Failure Behavior, `dry_run=false` und die Runtime-Umgebungsfreigabe
  `CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=true` oder
  `YBB_ALLOW_PLC_WRITES=true`.
- Unbekannte Enum-Werte, fehlende Felder und Typ-/Range-Fehler schreiben nicht
  stillschweigend `0`.
- Realtime-Pipelines duerfen aktive Writes nur mit explizitem Throttle
  verwenden; Trigger/Intervall-Pipelines schreiben synchron mit begrenzten
  Timeouts.

Mehrere Writes laufen in v1 geordnet sequenziell. Es gibt keine
Transaktionsgarantie ueber mehrere DB-Adressen; Teilfehler werden im
Runtime-Ergebnis als per-Mapping-Status sichtbar.


## 8. No-Code-Konfiguration

Die Verarbeitungspipeline wird als Drag-and-Drop-Konfigurator umgesetzt.

UI-Prinzip:

Node Palette links
Pipeline Canvas Mitte
Eigenschaften des gewählten Nodes rechts
wenige, kontextabhängige Eingabefelder
sinnvolle Defaults
Vorlagen für Standardpipelines
keine Formularwüste
keine unnötigen globalen Freitextfelder


## 9. Bedienphilosophie
Kanalübersicht

Darstellung:

kompakte Kanalkarten
Filter
Suchfeld
Statusicons/Farben
Quelle
technischer Platz
Live-Runtime-Status
zugewiesene Live-/Default-Pipeline, falls gesetzt
letzte Aktivität
Schnellaktionen
Kanal-Detail

Abschnitte:

Stammdaten
Quelle
Live-/Default-Pipeline, falls gesetzt
Ergebnisse
Outputs
Datenerfassung
Live-Ansicht
triggerbare Pipelines, die auf diesen Kanal angewandt sind
letzte Pipeline-Trigger / letzte Frames
Pipeline-Konfigurator
Node-basiert
gut lesbar
keine rohen JSON-Editoren im Standardpfad
Debug-/Advanced-Bereich nur bei Bedarf
Live-Ansicht
Streambild
Overlay
Ergebniswerte
Latenz/FPS kompakt
Debug-Layer optional einblendbar
Trigger-Konfiguration
im Pipeline-Konfigurator, nicht als eigene Top-Level-Verwaltung
HTTP Trigger Input fuer Methode, Auth, Timeout, Endpoint-Sichtbarkeit
Channel Frame Input als kanalgebundene Quelle
Response Output mit Response-Feld-Mapping
Pipeline-Detail/Deployment-Detail zeigt Endpoint-URL, Methode, Auth, Timeout,
Beispiel-Request/-Response und Bereitschaft


## 10. Performance- und Latenz-Anforderungen

Diese Punkte sind verpflichtend:

Realtime-Pfad ohne Queue vor der Inferenz
kontrollierte Frame-Drop-Strategie statt wachsender Backlogs
minimierte Serialisierung im kritischen Pfad
getrennte Realtime- und Trigger-Executors
kein UI-Thread oder Admin-API im Live-Datenpfad
modellbezogene Routing-/Warmup-Strategien
admission control / overload behavior definieren
Overlay darf Live-Inferenzpfad nicht dominieren
Ressourcenkonflikte zwischen Livebetrieb und Training müssen begrenzt sein


## 11. Trigger-HTTP-Schnittstelle

HTTP Trigger werden aus publizierten/deployten Pipelines abgeleitet, die einen
HTTP Trigger Input Node enthalten. Der Trigger ist ein Pipeline-Eingang, kein
Kanalmodus und kein separates Operator-Objekt.

Anforderungen:

stabiler, aus Pipeline-Identitaet, Version und HTTP Trigger Input abgeleiteter Endpoint
Kanalbezug ueber die Channel-zu-Pipeline-Zuordnung
Snapshot-Ausloesung durch den Trigger Executor und Uebergabe an Channel Frame Input
Pipeline-Ausfuehrung
Response Output erzeugt den strukturierten Response Body
definierte Fehlercodes
definierte Timeouts
deterministische Antwortstruktur

Eine triggerbare Pipeline muss publish-blockierend validieren:

genau ein HTTP Trigger Input
genau ein Channel Frame Input
gueltige Kanalbindung ueber den Channel, dem die Pipeline zugeordnet ist
ein erreichbarer Response Output
keine mehrdeutige oder halbe Triggerbarkeit

Die Antwort kann z. B. enthalten:

result
count
direction
segment_area
custom calculated fields


## 12. Ergebnisverarbeitung

Ergebnisse dürfen nicht auf rohe Modelloutputs beschränkt bleiben.

Die Plattform muss unterstützten:

Kombination mehrerer Inferenzschritte
ROI-basierte Nachverarbeitung
Regeln und Berechnungen
finale Business-Entscheidungen
Ausgabe normalisierter Resultate


### 12.1 Kontextbewusstes Bild-, Objekt- und Overlay-Modell

Der Runtime-Teil verwendet ein kontextbewusstes Visual-/Object-Modell. Die
Live-Ansicht darf nicht mehr implizit annehmen, dass jedes Overlay auf dem
urspruenglichen Eingangsbild liegt.

Verpflichtende Runtime-Konzepte:

Image Context

Ein Image Context beschreibt einen konkreten visuellen Zustand:

- Originalbild des Kanals
- Resize-/Transformationsbild
- fester Crop
- ROI-Bild aus einem Objekt
- Masken-/Segmentation-/Processed-Bild

Jeder visuell arbeitende Node muss deklarieren, ob er Image Contexts
konsumiert, bewahrt, transformiert oder neu erzeugt.

Object Reference

Objektproduzierende Nodes, insbesondere Detection und Segmentation, erzeugen
stabile Objekt-Referenzen pro Pipeline-Ausfuehrung. Eine Objekt-Referenz
enthaelt mindestens:

- object id / detection id
- source image context id
- Geometrie und Koordinatensystem
- Klasse/Label und Score, falls vorhanden
- erzeugender Node und Provenance-Metadaten

Diese IDs sind zunaechst pro Frame bzw. pro Runtime-Ausfuehrung stabil. Ein
spaeteres Tracking kann zusaetzliche track ids ergaenzen, ersetzt aber nicht
die Objekt-Referenz.

Provenance und Parent-Child Linking

ROI-, Crop- und object-abgeleitete Pfade muessen speichern:

- source image context
- source object reference
- Transformationsbeziehung
- Projektionseigenschaften, soweit geometrisch sicher bekannt
- Moeglichkeit, downstream Resultate wieder an das Quellobjekt anzuhaengen

Enrichment / Merge

Downstream Resultate wie ROI-Klassifikation, Segmentmessungen,
objektbezogene Scores oder Labels werden als Enrichment an eine source object
reference gebunden. Ein Merge-/Enrichment-Schritt darf nur publizierbar sein,
wenn die source-object-Verknuepfung erhalten ist.

Display Context und Overlay Context

Overlays sind an Image Contexts und/oder Object References gebunden. Die
Live-Ansicht rendert auf einem Display Context:

- Bei einer eindeutigen visuellen Kette wird der letzte visuelle Image Context
  dieser Kette als Default genutzt.
- Overlays werden auf dem Image Context gerendert, zu dem sie gehoeren.
- Bei mehreren visuellen Branches ist ein expliziter Display Output Node
  erforderlich.
- Projektion auf einen anderen Display Context ist nur erlaubt, wenn die
  Transformationslinie eine sichere Projektion beschreibt.

Heatmap Overlay

Anomaly Heatmaps sind ein eigener Overlay-Primitive-Typ `heatmap`. Sie werden
nicht als Boxen, Polygone oder Labels kodiert. Der Primitive enthaelt Format,
Bilddaten, Heatmap-Dimensionen, Wertebereich, Colormap, Threshold und
Layer/Opacity-Stil. Die Default-Darstellung nutzt eine transparente Heatmap
ueber dem zugehoerigen Image Context; Operators koennen den Layer ein- oder
ausblenden. Masken sind ein eigener Primitive-Typ `mask` und bleiben verborgen,
solange das Modell keinen echten Maskenpfad bereitstellt.

Object Spatial Signal

`analysis.object_spatial_signal` erzeugt ein boolesches Signal aus Objekt-
Geometrie. Der Node konsumiert Detection-, Segmentation- oder Region-Set-
Outputs und prueft Annaeherung, Ueberlappung, Abstand oder Belegung eines
operatorgezeichneten Feldes. Felder werden in der Live-Ansicht per Drag im
Stream-Bild festgelegt und normalisiert gespeichert; Operatoren geben keine
Pixelkoordinaten ein. Abstand und Annaeherung werden als prozentuale
Frame-Diagonalwerte konfiguriert, Ueberlappung als Prozent der kleineren
Objektflaeche. Das Ergebnis ist ein normales `result`-Payload mit `signal`,
`decision`, Regelstatus und Match-Details und kann dadurch in Response Output,
Persisted Result, Conditional Capture, interne Events und Connectoren fliessen.

Display Output Node

`output.display_output` ist ein Runtime-/Live-View-Output-Node. Er waehlt den
Image Context fuer die Live-Ansicht explizit und deklariert, welche Objekt-,
Overlay- und Enrichment-Layer auf diesem Display Context angezeigt werden
sollen. Er ist kein externer Connector und ersetzt nicht Persisted Result,
HTTP Trigger Response oder interne Events.

Vorbereitung fuer ROI Fan-Out

Das Modell muss spaeter einen ROI Fan-Out Node aufnehmen koennen. Dieser Node
wird ein Object Set, zum Beispiel Detection-Ergebnisse, konsumieren und pro
Objekt einen ROI Image Context erzeugen. Jeder ROI Image Context muss die
source object reference und source image context reference behalten, damit
Klassifikations- oder Segmentierungsergebnisse anschliessend wieder an das
urspruengliche Objekt gemerged und auf einem passenden Display Context
gerendert werden koennen.


### 12.2 Computer Vision and board/free-region measurement pipeline

The board measurement workflow is implemented as runtime analysis over the
existing Feature Detection node output and over deterministic classical
Computer Vision edge output. Feature Detection performs ML inference. Computer
Vision nodes run in the runtime plane with open-source OpenCV algorithms and do
not call the inference plane. The measurement nodes below consume detections,
CV edges, object references, image context, and geometry; they do not create a
second feature detection concept.

Supported typed runtime/result objects:

- `CvEdgesPayload`: source image context, processed edge-mask frame context,
  edge pixel count, optional contours, optional candidate line metadata,
  timings, and provenance.
- `ReferenceEdge`: `edge_id`, `image_context_id`, pixel line geometry,
  confidence, role (`start_edge`, `side_edge`, `left_edge`, `right_edge`,
  `custom`), source, and provenance.
- `InspectionCoordinateFrame`: frame id, image context id, origin,
  longitudinal/transverse axis vectors, confidence, source/provenance, and
  optional inspected object, camera, and view ids.
- `FeatureMeasurement`: feature id/type, measurement name, pixel value,
  optional millimeter value, basis (`bbox_width`, `bbox_height`,
  `approximate_diameter`, `polygon_length`, `mask_area`,
  `axis_projected_width`, `axis_projected_length`), method metadata, and
  confidence.
- `ClearanceZone`: blocked longitudinal interval in pixels, optional metric
  interval, clearance amount, rule metadata, severity, and category.
- `FreeRegion`: found flag, selected start/end positions, distance from
  origin, optional metric values, and not-found reason.
- `MeasurementDecision`: pass/fail/warning status, label, evaluated rules,
  messages, and selected output values.

Implemented nodes:

- `computer_vision.canny_edge_detection`: consumes an image context and
  produces a real OpenCV Canny edge mask, optional contours, a processed image
  context for the mask, timing metadata, and debug overlay layers. It validates
  grayscale conversion mode, threshold ordering, blur kernel size, aperture
  size, and optional mask dimensions.
- `computer_vision.color_object_segmentation`: consumes an image context and
  detects visually distinct objects without AI inference. It applies configured
  HSV color profiles, optional operator-drawn fields, mask cleanup, contour
  extraction, optional distance-transform watershed splitting for compact
  touching same-color blobs, and oriented rectangles, then emits a
  `region_set`, per-label count measurements, mask frame, and overlay with
  normalized contour/object geometry. Each emitted region remains individually
  addressable through a per-execution object reference and profile/split
  provenance metadata.
- `computer_vision.reference_edge_from_cv_edges`: consumes `cv_edges`, extracts
  Hough line segment candidates, ranks/selects lines by role, angle, length,
  region preference, nearest side, and confidence, then emits
  `ReferenceEdge` objects.
- `analysis.reference_edge_from_features`: maps detected feature classes such
  as `board_start_edge` and `board_side_edge` to edge roles. Lines can be
  derived from bbox sides, polygon lines, or mask contour metadata when
  available. Required roles fail validation/runtime clearly.
- `analysis.coordinate_frame_from_edges`: builds an
  `InspectionCoordinateFrame` from a start edge and side/direction edge. It
  supports center/intersection/configured-endpoint origin rules and
  along-side-edge or perpendicular-to-start-edge longitudinal axes.
- `analysis.feature_measurement`: measures existing Feature Detection objects
  from bbox, polygon/mask metadata, and optional axis projection.
- `analysis.clearance_zone_generator`: turns per-feature measurements into
  blocked board-axis intervals using structured rule tables with multipliers,
  min/max clearance, before/after flags, confidence threshold, and optional
  interval merging.
- `analysis.first_free_region_finder`: sorts/merges blocked intervals, starts
  at the configured offset, and returns the first gap satisfying minimum free
  length, optionally bounded by a board end limit.
- `analysis.pixel_to_metric`: applies simple positive `mm_per_pixel`
  calibration to measurements, clearance zones, free region, and selected
  response/result fields. Homography/perspective correction is out of scope and
  must be modeled as a future separate node.
- `analysis.quality_rule_evaluation`: evaluates simple structured rules for
  feature count, feature size, total length/area, and free-region distance.
- `analysis.object_orientation_signal`: evaluates elongated object orientation
  and shape only inside configured inspection fields. The reference direction
  can be drawn on the live frame, configured as an angle, or derived from a
  coordinate-frame axis. Output is a normal signal/result payload plus overlay.

Feature Detection based and CV based edge extraction are both supported:

```text
Channel Frame Input
+-> Canny Edge Detection
|   -> Reference Edge From CV Edges
|   -> Coordinate Frame From Two Edges
+-> Feature Detection
    -> Feature Measurement

Coordinate Frame + Feature Measurements
-> Clearance Zone Generator
-> First Free Region Finder
-> Pixel To Metric Conversion
-> Overlay Output / Response Output / Persisted Result

Channel Frame Input
-> Color Object Segmentation
-> Object Orientation Signal
-> Overlay Output / Response Output / Persisted Result
```

The graph executor supports true fan-out and fan-in. One image-context output
can feed multiple downstream branches, and a downstream node can consume
different typed inputs from different branches. Publishing validation must
reject missing required fan-in inputs and incompatible port pairings rather
than requiring operators to duplicate input nodes manually.

Overlay output must render these real runtime results when present:
CV edge masks, candidate CV line segments, selected reference-edge lines,
coordinate-frame axes, feature boxes and measurement labels, clearance
polygons, first-free-region polygons, distance arrows, and decision labels.
Response Output and Persisted Result may map fields such as `distance_mm`,
`distance_px`, `free_region_found`, and `status`.

Validation blocks publishing when required Feature Detection or CV edge inputs
are not connected, Canny thresholds/kernel/aperture settings are invalid, CV
line-selection rules lack required roles, edge role mappings lack `start_edge`
or `side_edge`, coordinate-frame inputs are missing, clearance rules reference
measurements not produced upstream, `mm_per_pixel` is not positive, a
free-region finder has no frame/zones, or a response field requests a value
that no reachable upstream node produces.


## 13. Modularität und Erweiterbarkeit

Neue Anforderungen sollen über Plugins/Nodetypen integrierbar sein.

Erweiterbar sein müssen:

neue Source-Typen
neue Inference-Node-Typen
neue Calculation-Nodes
neue Capture-Nodes
neue Output-Connectoren

Architekturregel:
Erweiterungen dürfen nicht zu Änderungen quer durch alle Services zwingen.


## 14. Sicherheit

Der Runtime-Teil nutzt die bestehende Benutzer- und Key-Verwaltung.

Anforderungen:

UI nur für authentifizierte Benutzer
App-zu-App-Zugriffe per API-Key/Bearer-Mechanismus
pipeline-abgeleitete Trigger-Endpunkte nicht ungeschuetzt offen
Audit für sensible Konfigurationsänderungen
keine Secrets in Klartext
Upload-/Payload-Grenzen
sinnvolle Timeouts


## 15. Persistenz

Zu speichern sind mindestens:

Kanäle
Quellenkonfigurationen
Pipeline-Definitionen und Versionen
Kanal-zu-Live-Pipeline-Zuweisungen, falls verwendet
pipeline-abgeleitete Trigger-Endpunkt-Metadaten
Outputdefinitionen
Capture-Regeln
letzte Ergebnisse / Ergebnis-Historie
Runtime-Status
Audit-Einträge
Live-/Trigger-Performance-Metadaten

Migration aus altem Modell:

Falls bestehende Kanal-Datensaetze `mode=trigger` oder triggerbezogene
Konfiguration am Kanal enthalten, migriert die Schema-/Datenmigration diese
Konfiguration in interne Trigger-Endpunkt-Metadaten, setzt den Kanal wieder auf
Source-/Kamera-Semantik zurueck und markiert den Endpunkt als
Legacy-Migrationsartefakt. Neue Konfiguration muss ueber HTTP Trigger Input,
Channel Frame Input und Response Output in der Pipeline erfolgen. Wenn ein
Legacy-Endpunkt nicht vollautomatisch in eine gueltige Pipeline ueberfuehrt
werden kann, bleibt er sichtbar als zu pruefende Runtime-Metadaten und wird
nicht stillschweigend als neuer Operator-Workflow dargestellt.


## 16. Observability

Pflicht:

Health/Ready-Endpunkte
Kanalstatus
letzte Fehlerursachen
Realtime-Latenzmetriken
Trigger-Latenzmetriken
FPS / Drop-Rate
Queue-/Executor-Status für nichtkritische Jobs
Korrelation-IDs
kompakte, operatorfreundliche Statusdarstellung


## 17. Label-Studio-Datenerfassung

Ein Kanal kann:

Vollbilder erfassen
ROI-Bilder erfassen
Bilder in mehrere Label-Studio-Projekte einspeisen
Capture abhängig von Regelereignissen auslösen

Capture-Regeln können z. B. sein:

jedes frame
jedes n-te Bild
nur bei Trigger
nur bei bestimmtem Ergebnis
nur ROI statt Vollbild
mehrere Zielprojekte


## 18. Abgrenzung

Der Runtime-Teil ist nicht zuständig für:

Modelltraining
Dataset-Snapshot-Management für Training
Model Registry
Deployment-Erstellung

Er nutzt diese Fähigkeiten der bestehenden Plattform.

## 19. Abnahmekriterien

Die Arbeit ist nur vollständig, wenn:

Kanalverwaltung vollständig nutzbar ist
RTSP und pipeline-basierter Triggerbetrieb real funktionieren
Pipeline-Konfigurator produktiv nutzbar ist
Overlay im Livebild angezeigt wird
Ergebnisse weiterverarbeitet werden können
HTTP-Trigger ueber HTTP Trigger Input, Channel Frame Input und Response Output echte Response-Ergebnisse liefern
Bilder/ROIs in Label-Studio-Projekte erfasst werden können
mehrere Projekte pro Kanal möglich sind
Architektur erweiterbar bleibt
Performance-/Latenzanforderungen im Design berücksichtigt sind
UI operativ einfach und ohne Technikballast bedienbar ist
