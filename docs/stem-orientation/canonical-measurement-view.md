# Kanonischer Messkorridor für Runtime, Datenerfassung und Label Studio

## Zweck und fachliche Grenze

Die Stammausrichtung verwendet für Segmentierungsinferenz, Capture und Label Studio
dieselbe kanonische Bildansicht. Im empfohlenen Standardmodus bleibt die Perspektive
des intrinsisch entzerrten Kamerabildes erhalten: Der Messkorridor wird ausgeschnitten,
außerhalb des Quadrilaterals geschwärzt und nur proportional mit Rand auf die feste
Ausgabegröße skaliert. Die vollständige Kameraansicht bleibt für Bedienung und Diagnose
erhalten, gehört aber nicht zum neuen Trainingsinput.

> Wartende Stämme außerhalb der kanonischen Messansicht müssen nicht gelabelt werden.
> Sichtbare, aber ungelabelte Stämme dürfen nicht im tatsächlichen
> Segmentierungstrainingsinput verbleiben.

Die Transformation ist allgemein als `RuntimeImageViewProfile` beziehungsweise
`image.view-profile.v1` implementiert. Stammbezogene Gates und Eligibility-Regeln sind
davon getrennt und bleiben in den Stamm-Nodes. Es gibt keine zweite Anwendung, keine
zweite Datenbank und keine duplizierte OpenCV-Implementierung im Frontend oder
Dataset-Service.

## Wiederverwendete Erweiterungspunkte

- `preprocessing.camera_undistort` liefert den intrinsisch entzerrten Eingangsraum.
- Der vorhandene Node `preprocessing.process_coordinate_rectify` wurde zur allgemeinen
  kanonischen Bildansicht erweitert. Ein optionales
  `StemCurvatureProcessCalibration` hängt Anlagen-X/Y und Prozessgeometrie an.
- `FramePayload`, `RuntimeImageContext` und `RuntimeImageTransform` transportieren
  Bildpixel, Homographien, Quellkontext und Provenienz durch Preview, Live-Runtime und
  Capture.
- `capture.full_frame_labelstudio` erfasst seinen Pipeline-Frame unverändert. Mit
  `require_eligibility=true` werden nur angenommene Resultate des Nodes
  `analysis.stem_measurement_eligibility` importiert.
- Die bestehende idempotente Label-Studio-Provisionierung und der vorhandene
  Projekt-/Dataset-Mappingpfad bleiben zuständig. Für den neuen Datenstand ist ein neues
  Dataset und ein eigenes Projekt, zum Beispiel „Stammsegmentierung – Messkorridor“, zu
  provisionieren.
- Dataset-Materialisierung, Snapshotmanifest, Trainingsexport und Deploymentmanifest
  übernehmen den View-Vertrag ohne einen parallelen Modellkatalog.

## View-Profil einrichten

1. Im Pipeline Designer `source.channel_frame` mit
   `preprocessing.camera_undistort` verbinden und eine gültige intrinsische
   Kalibrierung auswählen.
2. Den bestehenden Node `preprocessing.process_coordinate_rectify` ergänzen.
3. Im Property-Editor `Canonical image view` auf dem entzerrten Standbild die vier
   Ecken in der Reihenfolge oben links, oben rechts, unten rechts, unten links anklicken.
4. `Preserve camera perspective (recommended)` auswählen. Eine fachlich feste
   Outputgröße, zum Beispiel 1600 × 640, wählen und das Profil berechnen und anwenden.
5. Das gültige Profil in die neue unveränderliche Pipelineversion übernehmen. Die
   Profil-ID allein ist keine Version: Der `profile_checksum` bindet Geometrie,
   Quellauflösung, intrinsische Kalibrierung, Outputgröße und Interpolation.
6. Die Draft Preview durch den Rectify-Node ausführen. Der Editor zeigt die vollständige
   Kameraansicht und die tatsächliche kanonische Modell-/Labelansicht nebeneinander.

Das Profil speichert normalisierte Quellpunkte im `UNDISTORTED_IMAGE`, beide
Homographien, exakte Quell- und Outputauflösung, Intrinsic-Hash, Erstellzeit,
Provenienz und Validierungsstatus. Falsche Auflösung, anderer Kanal oder Intrinsic-Hash,
Selbstüberschneidung, zu kleine Fläche, instabile Homographie und nicht unterstützte
Outputgröße führen fail closed zum Abbruch.

### Perspektive und metrische Krümmung

`POLYGON_MASKED_CROP` ist der Standard für neue Stammprofile. Seine Source-to-View-
Matrix enthält nur Translation und einheitliche Skalierung. Winkel und Form des
Kamerabildes werden daher nicht durch ein Strecken der vier Ecken verändert. Die
schwarzen Bereiche außerhalb des Quadrilaterals gehören weiterhin zum kanonischen
Bild, enthalten aber keine wartenden Stämme.

`PERSPECTIVE_RECTIFY` bleibt ausschließlich für vorhandene Profile und bewusst
gewählte planare Anwendungen rückwärtskompatibel. Dieser Modus zieht das Quellpolygon
auf ein Rechteck und verändert damit die sichtbare Form eines Objekts, das oberhalb der
Referenzebene liegt.

Die Anlagenkalibrierung ist davon getrennt. Der Koordinatensystem-Assistent erzeugt aus
Ursprung O, bekanntem X-Endpunkt, bekanntem Y-Endpunkt und der XY-Rechteckecke vier
eindeutige Referenzpunkte für die Homographie vom Kamerabild auf die X/Y-Referenzebene.
Z dokumentiert nur die vertikale Ebenennormale. Für die
Krümmung wird die Segmentierungsmaske intern in ein metrisch ausgerichtetes Messraster
projiziert. Modellbild, Capture und Label-Studio-Bild bleiben dabei unverändert. Die
berechnete Mittellinie und Diagnose werden anschließend in die kanonische Ansicht und
optional in das Vollbild zurückprojiziert.

## Bedienfolge im Pipeline Designer

1. Im Node `Canonical image view + X/Y calibration` unter **1. Model and Label Studio
   corridor** den perspektiverhaltenden Modus wählen und die vier Korridorecken setzen.
2. Im selben Node unter **2. Metric X/Y and reference plane** den Ursprung O anklicken,
   danach den Endpunkt der bekannten X-Strecke entlang des Längsförderers, den Endpunkt
   der bekannten horizontalen Y-Strecke quer zum Förderer und zuletzt die zugehörige
   XY-Rechteckecke. Nur die bekannten X- und Y-Längen werden in Millimetern eingegeben;
   die vier X/Y-Koordinaten erzeugt die UI automatisch.
3. Die positive Z-Richtung als vertikale/normal zur Fördererebene verlaufende Richtung
   einzeichnen und „nach oben“ oder „zur Maschinenbasis“ wählen. Z ist niemals die
   Förderrichtung. Eine Z-Referenzlänge ist optional und in V1 nur Dokumentation. Sie
   kann die zwingende Y-Länge nicht ersetzen, da horizontaler Y-Bogen gemessen wird.
4. Weitere Mess-ROIs und Prozessgates sind im Standardmodus nicht zu zeichnen. Das
   versionierte Canonical-View-Quadrilateral ist gleichzeitig Kandidatenbereich,
   Krümmungsbereich und Bereich für das Mantelflächentracking. Beim Eintritt beginnt
   die Session; am gegenüberliegenden Rand werden Krümmung und Ergebnis eingefroren.
   Das Prozessprofil speichert Profil-ID und Checksum der verwendeten Canonical View.
   Nach einer Änderung der View muss die Prozesskalibrierung neu berechnet werden.
5. Die Preview einmal bis einschließlich dieses Nodes ausführen. Erst dieser Output ist
   das tatsächliche Modell- und Labelbild.
6. `Stem measurement corridor eligibility` verwendet in der Pipelinevorlage bereits
   den gesamten Canonical View als `measurement_corridor`. Im Standardeditor wird daher
   kein zweites ROI angeboten; vorhandene Legacy-Konfigurationen bleiben wirksam. Die
   `crowd_boundary` bleibt eine optionale
   zusätzliche Ablehnungsgrenze innerhalb der Ansicht, falls sich das Stammpaket trotz
   Crop noch bis in den kanonischen View erstreckt.
7. Auf der bestehenden Channel-/Capture-Seite das dedizierte
   Stammsegmentierungsprojekt auswählen. Sobald ein kanonisches Profil verfügbar ist,
   wird `Canonical model/Label Studio view` automatisch als Capture-Quelle gewählt.
   Der Benutzer kann für ältere Projekte weiterhin bewusst auf den Vollbildmodus
   zurückschalten.

## Pipeline und Bildräume

### Automatisch abgeleiteter Messkorridor

Im Standardablauf werden in der Prozesskalibrierung keine weiteren Candidate-,
Curvature- oder Surface-ROIs und keine separaten Session-, Freeze- oder Decision-Gates
gezeichnet. Das Quadrilateral der Canonical Image View ist zugleich der vollständige
Messkorridor: Eintritt startet Tracking und Krümmungsbeobachtung, am
gegenüberliegenden Rand wird die Krümmung eingefroren und die Entscheidung fällig.

Der Stamm tritt quer vom rechts wartenden Stammpaket über die rechte Kante der
Canonical Image View ein. Dieser Eintritt startet die Session. Nach der Übergabe auf
den Längsförderer bestimmt die im Koordinatensystem-Assistenten gesetzte positive
X-Richtung den Austritt; die Korridorkante mit dem größten X ist die rote
Austrittskante. Die vier metrischen O/X/Y/XY-Referenzen dürfen innerhalb
des Canonical-Korridors liegen; dessen vollständige projizierte Grenzen erweitern den
gültigen Prozessbereich, ohne die kalibrierte Homographie zu verändern.

Die daraus abgeleiteten internen Geometriefelder bleiben für die vorhandenen
Runtime-Verträge erhalten. Das Prozessprofil speichert View-Profil-ID und Checksumme;
nach einer Änderung der Canonical Image View muss es neu berechnet werden. Manuell
getrennte ROIs und Gates bleiben ausschließlich als eingeklappter Legacy-Sonderfall
für bestehende Installationen verfügbar.

Die neue Stammkette lautet:

```text
source.channel_frame
→ preprocessing.camera_undistort
→ preprocessing.process_coordinate_rectify
→ inference.segmentation
→ analysis.active_stem_tracker
→ analysis.stem_measurement_eligibility
→ weitere Geometrie-, Phasen- und Solver-Nodes
```

Diese Kette muss nicht von Hand aufgebaut werden. Die bestehende Runtime-Pipeline-
Vorlagenliste enthält `Stammausrichtung – Krümmungsminimum`
(`template-realtime-stem-curvature-minimum`). Sie verdrahtet die kanonische
Modellansicht bis zum V3-Solver, aktiviert die Modell-/View-Kompatibilitätsprüfung und
startet ausschließlich im Shadow Mode ohne SPS-Node. Kamera-, View-/Prozess-, Modell-
und Phasenkalibrierung bleiben bewusst standortgebundene Publish-Voraussetzungen.

Das Segmentierungsmodell, der Active-Stem-Tracker und die Stammgeometrie arbeiten im
kanonischen View. `source_to_view_homography` und `view_to_source_homography` liegen im
Bildkontext und in den Capture-Metadaten. `apply_image_view` ist der gemeinsame
Transformationskern. `restore_view_mask`, `source_points_to_view`,
`view_points_to_source` und `view_overlay_geometry_to_source` bilden Masken, Polygone
und Vektoroverlays zurück in das entzerrte Vollbild ab.

Die Channel-LiveView trennt vier Ansichten: verzerrtes Kamera-Rohbild ohne fachfremde
Pixeloverlays, intrinsisch entzerrtes Vollbild, kanonischer Pipeline-Modellframe sowie
entzerrtes Vollbild mit serverseitig rückprojizierten Vektoroverlays. Masken werden mit
`restore_view_mask` rückprojiziert. Dadurch wird kein kanonisches Overlay irrtümlich
über den verzerrten WebRTC-Rohstream gelegt.

## Messkorridor und Eligibility V1

`analysis.stem_measurement_eligibility` konfiguriert im kanonischen Bild:

- `view_profile_id`
- `measurement_corridor`
- `crowd_boundary`
- `min_visible_axis_ratio`
- `min_stable_frames`
- `reject_if_touching_crowd_boundary`
- `max_blur_score`

Die frühere Konfiguration `reject_if_multiple_stems_in_view` bleibt nur intern für
alte Tracker-Konfigurationen lesbar. Bei der aktuellen bereichsbasierten Auswahl muss
der Bediener keinen Mehrfachstamm-Schalter setzen.

Session Start, Curvature Freeze, Decision Gate, Conveyor Direction und die
Drehrichtungskonvention bleiben Teil der bestehenden versionierten
Prozesskalibrierung. Der Rectify-Node leitet diese Geometrien reproduzierbar in den
kanonischen View ab.

Ein positiver Frame ist nur zulässig, wenn der Tracker genau eine aktive Stamminstanz
auflöst, deren Maske die Crowd-Grenze nicht berührt, deren sichtbare Achse lang genug
und deren Track stabil ist. Curvature Freeze darf noch nicht erreicht und das Bild muss
ausreichend scharf sein. Weitere getrennte Masken des vorherigen oder nachfolgenden
Stamms blockieren den Frame im bereichsbasierten Auswahlmodus nicht. Unaufgelöste,
nahezu gleich große Kandidaten und miteinander verschmolzene Masken bleiben abgelehnt.

Gezielte negative Aufgaben dürfen ausschließlich die konfigurierten Klassen
`EMPTY_CORRIDOR`, `MACHINE_MOTION`, `LIGHT_CHANGE` oder `WOOD_DEBRIS` verwenden. Auch
negative Aufgaben enthalten nur den kanonischen View.

## Mehrere betriebsbedingt sichtbare Stämme

Wenn auf dem Längsförderer regelmäßig noch ein kurzer Rest des vorherigen Stamms und
im Transferbereich bereits der Nachfolgestamm sichtbar ist, gilt nicht die rohe Anzahl
aller Segmentmasken als aktive Stammzahl. `analysis.active_stem_tracker` hält zuerst
den bestehenden Track. Bei einer Neuaufnahme wird pro Prozessbereich die größte
zusammenhängende Maske verwendet; nahezu gleich große Masken im selben Bereich bleiben
mehrdeutig und werden nicht willkürlich ausgewählt. Nur die ausgewählte aktive Maske
wird an Geometrie, Krümmung, Phase und normalen positiven Capture weitergegeben.

Der Transferbereich umfasst den tatsächlichen Querbewegungsweg ab der Freigabekante
der Warteposition bis leicht in den Längsförderer hinein. Er soll das ruhende
Stammpaket nicht vollständig umfassen. Transfer- und Längsfördererpolygon dürfen und
sollen sich in einem schmalen Übergabeband überlappen; zwischen beiden darf keine
ungetrackte Lücke entstehen.

## Label Studio und Labelregeln

Für die kanonische Ansicht wird ein neues Dataset beziehungsweise eine neue
Datasetversion angelegt; bestehende Vollbild-Datasets und Label-Studio-Projekte werden
nicht umgedeutet. Die vorhandene schemaabhängige, idempotente Projektanlage wird mit
dem normalen Segmentierungsschema und einem stabilen Idempotency-Key verwendet.

Der vorhandene manuelle Channel-Capture verwendet den kanonischen Pipeline-Frame und
das Eligibility-Result. Er speichert den Pipeline-Frame bytegleich und übergibt ihn als
Taskbild. Quellframe-Referenz, Kamera- und Profilbindung, Homographien, Outputgröße,
Timestamp, Pipeline-/Node-Version, Eligibility-Status und Reason Codes bleiben
Metadaten; das Vollbild wird nicht als Taskbild importiert. Die Analysevorlage enthält
bewusst keinen automatisch auslösenden Capture-Node.

Im kanonischen Taskbild werden alle tatsächlich sichtbaren Stämme gelabelt. Die
bereichsbasierte Runtime-Auswahl bedeutet nicht, dass sichtbare Nebenstämme im
Segmentierungstraining ungelabelter Hintergrund werden dürfen. Der Crop soll das
ruhende Stammpaket ausschließen; ein unvermeidbarer kurzer Vorgängerrest oder bereits
eingetretener Nachfolgestamm wird mitgelabelt. Ein leerer, bewusst zugelassener
negativer Frame bleibt ohne Stammlabel. Abgelehnte Frames bleiben als
Runtime-/Capture-Diagnose zählbar, werden aber nicht an normale Trainingstasks gesendet.

Der vorhandene manuelle Live-Capture bleibt für ältere Projekte verfügbar. Sobald die
aktuelle Pipeline ein kanonisches Profil liefert, wählt die UI automatisch
`PIPELINE_IMAGE_OUTPUT`; ein bewusster manueller Wechsel bleibt möglich. Die
Capture-Seite zeigt dann entzerrtes Bedienerbild und tatsächliches Label-/Modellbild
nebeneinander. Profil-ID und Eligibility werden vor dem Senden geprüft; bei fehlendem
Profil, Profilwechsel oder abgelehntem Frame stoppt der Capture fail closed und
verwendet niemals ersatzweise das Vollbild.

## Modell-/Profilkompatibilität

Der Dataset-Snapshot schreibt `image_view` in `snapshot_manifest.json`. Der
Trainingsexport übernimmt diesen Block als `preprocessing.image_view` in das bestehende
Modell-/Deploymentmanifest. Gebunden werden:

- `view_profile_id` und `profile_checksum`
- kanonische Inputbreite und -höhe vor modellinternem Resize
- Farbraum
- Undistort- und Image-View-Preprocessingversion

Diese Angaben sind Trainingsprovenienz und keine Runtime-Sperre. Dasselbe
Segmentierungsmodell darf mit unterschiedlichen Kameras, Auflösungen, Farbräumen,
Undistort-Profilen und kanonischen Blickfeldern verwendet werden. Der normale
Inference-Preprocessingpfad bereitet das jeweilige Eingangsbild weiterhin für das
Modell auf. Der Segmentation-Node kann Unterschiede optional als
`model_image_view_difference` melden und schreibt den Vergleich als nicht blockierende
`image_view_compatibility`-Diagnose in seine Ausgabemetadaten. Kein Unterschied verwirft
einen Frame. Die Segmentierungsqualität muss bei einer neuen Kamera oder stark
veränderten Ansicht fachlich geprüft werden; ein Retraining ist aber keine technische
Voraussetzung.

Die lockere Modellwiederverwendung ändert nicht die Bindung der geometrischen
Messkette: View-, Prozess-, Krümmungs- und Phasenkalibrierungen müssen weiterhin zur
jeweils aktuellen Kameraansicht passen.

Canonical- und Legacy-/Vollbildaufnahmen dürfen nicht in demselben neuen Snapshot
gemischt werden. Die Materialisierung meldet dies als
`image_view_provenance_incomplete` beziehungsweise `image_view_profile_mixed`.

## Migration bestehender Daten

- Originalbilder, bestehende Datasets, Snapshots, Projekte, Modelle und
  Pipelineversionen bleiben unverändert.
- Eine optionale Offline-Materialisierung darf aus einem vorhandenen Original mit
  exakt demselben gespeicherten View-Profil einen neuen kanonischen Asset erzeugen. Das
  Original wird niemals überschrieben.
- Der neue Asset benötigt dieselbe vollständige View-/Kalibrierungsprovenienz wie ein
  Live-Capture und gehört in eine neue Datasetversion.
- Es ist keine Alembic-Migration erforderlich: Profile leben versioniert in der
  bestehenden Pipelinekonfiguration; Capture-, Task-, Dataset- und Modellmetadaten
  nutzen vorhandene JSON-Verträge und persistente Plattformpfade.

## Diagnose und Reason Codes

Wesentliche Capture-Ablehnungen sind:

- `STEM_CAPTURE_CANONICAL_VIEW_REQUIRED`
- `STEM_CAPTURE_VIEW_PROFILE_MISMATCH`
- `STEM_CAPTURE_PIPELINE_IMAGE_OUTPUT_REQUIRED`
- `STEM_CAPTURE_SINGLE_ACTIVE_STEM_REQUIRED`
- `STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW`
- `STEM_CAPTURE_ACTIVE_TRACK_UNCLEAR`
- `STEM_CAPTURE_TRACK_NOT_STABLE`
- `STEM_CAPTURE_VISIBLE_AXIS_TOO_SHORT`
- `STEM_CAPTURE_CROWD_BOUNDARY_TOUCHED`
- `STEM_CAPTURE_MOTION_BLUR_EXCEEDED`
- `STEM_CAPTURE_AFTER_CURVATURE_FREEZE`

Die Eligibility-Ausgabe enthält Status, Taskrolle, Track-ID, sichtbaren Achsanteil,
Segmentanzahl, Blur-/Focuswert, Profilbindung und Reason Codes. Der Debug-Overlay zeigt
Messkorridor, Crowd Boundary und Annahme/Ablehnung. Capture-Metadaten und die vorhandene
Result-/Diagnoseansicht machen die Ablehnungen auswertbar.

## Updatepfad

Es gibt keine neue Python-Abhängigkeit: NumPy und `opencv-python-headless` gehören
bereits zu `cv-runtime` und werden vom vorhandenen Offline-Wheelbundle geprüft. Die
Windows-Installer- und Update-Exporte prüfen zusätzlich View-Vertrag,
Transformationskern, Overlay-Rückprojektion, Registry-Node, Eligibility,
Dataset-Provenienz, Capture-Gate und Frontend-Editor als Release-Tokens. Der
WSL2-Preflight importiert den Kern und verlangt den neuen Node in der bestehenden
Registry. Profile und neue Datasetversionen liegen außerhalb des
Release-Symlinks und überstehen Update und Rollback.

Live-, Kamera-, Label-Studio- und End-to-End-Abnahme erfolgen auf dem Zielserver. Auf
dem Entwicklungsrechner sind statische Prüfungen, Unit-Tests und Frontendbuilds
maßgeblich.
