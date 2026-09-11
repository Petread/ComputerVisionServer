# Stammkrümmung und Vor-Einzug-Ausrichtung einrichten

Stand: 2026-08-04

Diese Anleitung beschreibt den aktuell im Inferenz Server vorhandenen Weg von
der Kamera- und Modellvorbereitung bis zur Inbetriebnahme im Shadow Mode. Sie
bezieht sich auf die Bedienung der bestehenden Admin-Web-App und benennt
technische Schritte, die gegenwärtig noch nicht vollständig im Frontend
abgebildet sind.

Die Funktion hat zwei klar getrennte Ergebnisse:

- `analysis.stem_mask_geometry` misst die **projizierte** Geometrie einer
  segmentierten Stamminstanz. Dazu gehören Referenzachse, Mittellinie,
  Residualprofil, Breitenprofil und normierte Krümmungskennwerte. Diese Stufe
  liefert weder Millimeter noch eine 3D-Bogenrichtung.
- `analysis.stem_temporal_orientation` beobachtet die projizierte Krümmung
  während einer **kalibrierten relativen Oberflächendrehung** und friert vor
  dem ersten Vorzentrierungs-Walzenpaar eine signierte Empfehlung in Grad ein.
  Das Ergebnis ist derzeit `projected_baseline` und `shadow`.

> **Sicherheitsgrenze:** Diese Anleitung nimmt keine SPS-Schreibfunktion in
> Betrieb. Der Node `output.siemens_sps_write` gehört nicht in die hier
> beschriebene Pipeline. `PlcWritesAllowed=false` bleibt gesetzt. Eine
> Gradangabe ist eine Shadow-Empfehlung für Prüfung und Datensammlung, kein
> Maschinenbefehl.

## 1. Was heute in Betrieb genommen werden kann

| Stufe | Ergebnis | Voraussetzung | Status |
|---|---|---|---|
| Geometrie | Normierte projizierte Stammkrümmung und Debugoverlay | Instanzsegmentierung | Vollständig im Pipeline-Designer bedienbar |
| Deterministische Vor-Einzug-Empfehlung | Signierter Korrekturwinkel in `[-90°, +90°)`, Richtung, Konfidenz und Reason Codes | Intrinsische Kamerakalibrierung, validierte visuelle Rotationskalibrierung und Prozesszonen | Als Shadow-Pfad vorhanden |
| Automatische Sequenzaufzeichnung | Primärclip bis zum Decision Frame, Features, Rotationsspur und Manifest | Temporal-Recorder im Node aktiviert; Dataset/Projekt für Autoimport | Vorhanden |
| Label Studio und Snapshot | Timeline-Label und daraus abgeleitetes Winkelziel | `temporal_sequence`-Dataset und `stem_orientation_angle.v1` | Vorhanden |
| TCN/GRU | Optionaler kausaler Modellvergleich | Gültiger Temporal-Snapshot und freigegebenes Modellartefakt | Backend vorhanden; aktueller Frontend-Trainingsdialog ist noch nicht sicher verdrahtet |
| SPS-Drehauftrag | Maschinenbewegung | Separate ausdrückliche Freigabe und Maschinen-/Interlock-Abnahme | **Nicht Bestandteil dieser Inbetriebnahme** |

### Derzeitiger technischer Sperrpunkt für Gradwerte

Die intrinsische Kamerakalibrierung besitzt einen vollständigen Assistenten im
Pipeline-Designer. Für die visuelle Rotationskalibrierung gibt es dagegen noch
keinen Aufnahme- und Berechnungsassistenten in der Admin-Web-App. Der
Runtime-Node kann ein validiertes Profil verarbeiten, das Profil muss aber derzeit durch
den technischen Kalibrierungsprozess außerhalb der Bedienoberfläche mit
`cv_runtime.stem_rotation.calibrate_stem_rotation` erstellt werden.

Ohne ein solches Profil darf die Inbetriebnahme nur bis zur Geometrie- und
Trackingprüfung gehen. Die Software muss dann `manual_required` liefern. Eine
Pixel-zu-Grad-Konstante darf weder geraten noch von Hand passend gemacht werden.

## 2. Voraussetzungen

Vor der Konfiguration müssen folgende Punkte erfüllt sein:

- Das Update ist installiert und die vorhandenen Plattformdienste sind laut
  **Advanced → Settings & Health** gesund.
- Kamera, Objektiv, Fokus, Zoom, Bildauflösung und Montage sind mechanisch
  festgelegt. Eine spätere Änderung macht Intrinsik, Rotationskalibrierung und
  Prozesszonen ungültig.
- Die schräge Kamerapose ist bewusst festgelegt. Sie wird durch Undistort nicht
  in eine Draufsicht umgerechnet.
- Ein zur Kameraauflösung passendes Checkerboard oder ChArUco-Board ist
  vorhanden.
- Eine freigegebene **Instanzsegmentierung** mit einer eindeutigen Stammklasse
  ist bereits als deploybare Runtime-Capability vorhanden. Grundgewichte für
  Transfer Learning allein reichen dafür nicht aus.
- Für die Winkelstufe liegt ein validiertes Rotationskalibrierungsprofil mit
  bekanntem Betrachtungsende und bekannter positiver Drehrichtung vor.
- Es existieren repräsentative Aufnahmen mit geradem Stamm, C-Bögen beider
  Vorzeichen, S-Bogen, lokalen Astmerkmalen, mehreren Stämmen, Verdeckung und
  einem tatsächlichen Durchlauf bis zur Entscheidungslinie.
- Genügend persistenter Speicher für Result-History und gegebenenfalls
  Temporal-Samples ist eingeplant.

### Modelle: Was benötigt wird

Für die erste deterministische Inbetriebnahme wird genau ein deploytes
Segmentierungsmodell benötigt. Im Node `inference.segmentation` zeigt das Feld
**Deployed model** nur Modelle an, die bereits im bestehenden Modell- und
Deploymentpfad verfügbar sind.

- Ist ein geeignetes deploytes Segmentierungsmodell vorhanden, müssen keine
  zusätzlichen Pretrained-Gewichte heruntergeladen werden.
- Pretrained-Gewichte unter
  `/srv/cv-platform/control/pretrained-checkpoints` werden nur benötigt, wenn
  ein neues Modell auf der Anlage trainiert werden soll.
- Ein TCN-/GRU-Modell ist für die deterministische Baseline und für die erste
  Shadow-Datensammlung nicht erforderlich.

## 3. Empfohlene Reihenfolge

1. Optional, aber für Datensammlung empfohlen: Temporal-Dataset und
   Label-Studio-Projekt anlegen.
2. Kanal anlegen und Result-History aktivieren.
3. Kanalspezifische Realtime-Pipeline aus einer vorhandenen Vorlage erzeugen.
4. Stammgraph aufbauen.
5. Intrinsische Kamera kalibrieren.
6. Segmentierung, aktiven Stamm und Geometrie prüfen.
7. Validiertes Rotationsprofil eintragen.
8. Betrachtungskonvention und Prozesszonen festlegen.
9. Pipeline validieren, als Version speichern und publizieren.
10. Vollständige Sequenzen im Datei- und anschließend im Live-Shadow-Test
    prüfen.
11. Result-History, automatische Samples und Label-Studio-Aufgaben kontrollieren.
12. Erst nach dokumentierter Shadow-Abnahme eine spätere Maschinenfreigabe
    separat planen.

## 4. Temporal-Dataset und Label-Studio vorbereiten

Dieser Schritt ist nicht für die reine Geometrieanzeige erforderlich. Er sollte
aber vor dem Pipelinebau erfolgen, wenn geeignete Durchläufe automatisch für
das spätere Training gesammelt werden sollen. Dann kann der Dataset-Slug direkt
im Temporal-Node eingetragen werden.

### 4.1 Dataset anlegen

1. **Advanced → Snapshot Operations** öffnen.
2. Im Bereich **Datasets And Snapshot Request** folgende Werte setzen:
   - **Dataset Name:** sprechender Name, zum Beispiel
     `Stammausrichtung Linie 1`.
   - **Slug:** stabiler technischer Name, zum Beispiel
     `stammausrichtung-linie-1`.
   - **Task Type:** `temporal_sequence`.
   - **Schema Version:** `Stem orientation angle v1
     (stem_orientation_angle.v1)`.
3. **Create Dataset** wählen.
4. Den Slug notieren. Der Dataset-ID-Wert wird in der Tabelle nicht angezeigt;
   der Recorder akzeptiert im Feld `Temporal dataset ID` auch den eindeutigen
   Slug.

Der Browser-Upload ist für `temporal_sequence` absichtlich deaktiviert. Die
Sequenzen werden durch den Realtime-Recorder erzeugt und nicht als lose Bilder
hochgeladen.

### 4.2 Label-Studio-Projekt idempotent anlegen

1. **Advanced → Manual Project Mapping** öffnen.
2. Im oberen Bereich **Provision Label Studio Project** arbeiten, nicht im
   darunterliegenden Legacy-Mapping.
3. Das eben angelegte Dataset wählen.
4. Als **Registered annotation schema**
   `stem_orientation_angle.v1` wählen.
5. Projekttitel und einen stabilen **Training pipeline slug / recipe
   reference** vergeben.
6. **Automatically synchronize project metadata** aktivieren.
7. **Automatically request snapshots after qualifying annotation events** nur
   dann aktivieren, wenn nach der eingestellten Mindestzahl automatisch ein
   Snapshot angefordert werden soll.
8. Train-/Validation-/Test-Anteile so setzen, dass die Summe 100 Prozent ist.
   Die Aufteilung erfolgt später gruppenweise nach Session/`group_id`.
9. Optional einen stabilen Idempotency-Key vergeben. Ohne Eingabe verwendet der
   vorhandene Job Dataset plus Schema.
10. **Create Label Studio Project** wählen und warten, bis das Projekt im
    Bereich **Known Label Studio Projects** erscheint.
11. Mit **Open Project** prüfen, dass genau das erwartete Projekt angelegt
    wurde. Videoansicht und Timeline-Label `target_orientation` werden nach dem
    ersten Aufgabenimport an einer realen Aufgabe kontrolliert.

Die Pipeline darf erst mit automatischem Import publiziert werden, wenn genau
ein passendes Projekt dem Dataset und Schema zugeordnet ist. Andernfalls wird
das Sample zwar aufgezeichnet, der Import endet aber nachvollziehbar als
`needs_attention`.

## 5. Kanal anlegen

### 5.1 Datei-Testkanal

Für reproduzierbare Vorprüfungen wird ein normaler Channel mit Quelltyp
`file_test` verwendet. Der versteckte Graph-Node `source.file_test` ist kein
Operatorweg.

1. **Channels → Kanal hinzufügen** wählen.
2. Name und **Technical place** eindeutig benennen.
3. **Source type:** `file_test`.
4. Unter **Test file** ein repräsentatives Bild oder für Temporaltests ein
   Video hochladen.
5. **Max processing FPS** passend zur beabsichtigten Auswertung setzen.
6. **Sample every N:** für die Rotationsspur zunächst `1`. Eine Ausdünnung
   verändert die Winkelschritte und kann Featuretracking oder
   `maximum_frame_gap` verletzen.
7. **Pipeline max. parallel:** `1`. Die Runtime erzwingt für die registrierten
   stateful Stamm-Nodes ohnehin Serialisierung; die explizite Einstellung
   macht die Absicht sichtbar.
8. **Execution target:** `Zentraler Server`.
9. **Result history** aktivieren und Aufbewahrungszeit sowie maximale Anzahl
   ausreichend groß wählen.
10. **Create channel** wählen.

Der Datei-Quellpfad besitzt keinen expliziten Beschleunigungsfaktor und keinen
Vertrag für originale Aufnahmetimestamps. Ein Datei-Test ist daher für
deterministische Graph- und Decision-Frame-Prüfungen geeignet, aber kein Ersatz
für die abschließende Echtzeitlatenzprüfung.

### 5.2 Realkamerakanal

Für die reale Inbetriebnahme einen eigenen Channel mit dem passenden
vorhandenen Kameratyp anlegen, beispielsweise `basler_pylon`, `aravis_gige`,
`flir_blackfly` oder `rtsp`.

1. Kameraadresse beziehungsweise Geräte-ID und die zum Adapter gehörenden
   Parameter eintragen.
2. Die produktive Auflösung bereits jetzt festlegen.
3. **Sample every N** zunächst auf `1` und **Pipeline max. parallel** auf `1`
   setzen.
4. **Execution target:** für diese erste Ausbaustufe `Zentraler Server`.
5. **Result history** aktivieren.
6. Kanal speichern.

Intrinsik und Prozesszonen einer anderen Kamera oder Auflösung dürfen nicht
blind kopiert werden. Auch ein technisch identisches Ersatzobjektiv erfordert
eine neue Kalibrierungs- und Pipelineversion.

## 6. Kanalspezifische Pipeline aufbauen

Es gibt derzeit keine eingebaute Stammvorlage. Der vorhandene Frontendweg ist,
eine kanalspezifische Pipeline aus einer vorhandenen Realtime-Vorlage zu
erzeugen und anschließend umzubauen.

1. Den Kanal öffnen.
2. **Pipeline-Konfiguration** aufklappen.
3. Unter **Template basis** die Vorlage **Realtime detection with overlay**
   wählen.
4. **Create channel pipeline** wählen.
5. Als Modus `realtime` beibehalten.
6. Im Graph die Vorlagen-Nodes `preprocessing.frame_sampler`,
   `inference.detection` und `analysis.object_counting` sowie ihre Kanten
   entfernen. Im Dialog **Node Properties** geschieht das mit **Remove**.
7. `source.channel_frame`, `output.result`, `output.overlay` und
   `output.display_output` können als Gerüst bestehen bleiben.
8. Über den Guided Builder beziehungsweise die Node-Palette folgende Nodes
   hinzufügen:
   - `preprocessing.camera_undistort`
   - `inference.segmentation`
   - `analysis.active_stem_tracker`
   - `analysis.stem_mask_geometry`
   - `analysis.stem_surface_rotation`
   - `analysis.stem_temporal_orientation`
9. Automatisch erzeugte Kanten kontrollieren. Sie sind nur eine Bedienhilfe;
   verbindlich ist die folgende Portbelegung.

### 6.1 Verbindlicher Graph

| Von Node und Port | Zu Node und Port | Zweck |
|---|---|---|
| `source.channel_frame.frame` | `preprocessing.camera_undistort.frame` | Rohframe intrinsisch entzerren |
| `preprocessing.camera_undistort.frame` | `inference.segmentation.frame` | Segmentierung auf dem entzerrten Bild |
| `inference.segmentation.segments` | `analysis.active_stem_tracker.segments` | Kandidaten für aktiven Stamm |
| `preprocessing.camera_undistort.frame` | `analysis.active_stem_tracker.frame` | Frameprovenienz und Overlay |
| `analysis.active_stem_tracker.segments` | `analysis.stem_mask_geometry.segments` | Genau eine getrackte Stamminstanz |
| `preprocessing.camera_undistort.frame` | `analysis.stem_mask_geometry.frame` | Bildkontext für Overlay |
| `preprocessing.camera_undistort.frame` | `analysis.stem_surface_rotation.frame` | Entzerrtes Bild für Featuretracking |
| `analysis.active_stem_tracker.segments` | `analysis.stem_surface_rotation.segments` | Aktive Maske mit Track-ID |
| `analysis.stem_mask_geometry.stem_geometry` | `analysis.stem_surface_rotation.stem_geometry` | Lokale Stammachse und Breite |
| `analysis.stem_mask_geometry.stem_geometry` | `analysis.stem_temporal_orientation.stem_geometry` | Frameweise projizierte Krümmung |
| `analysis.stem_surface_rotation.stem_rotation` | `analysis.stem_temporal_orientation.stem_rotation` | Kalibrierte relative Rotationsspur |
| `analysis.active_stem_tracker.segments` | `analysis.stem_temporal_orientation.active_stem` | Track-/Sessionidentität |
| `analysis.stem_temporal_orientation.result` | `output.result.result` | Result-History und Live-Resultpfad |
| `analysis.stem_temporal_orientation.overlay` | `output.overlay.result` | Explizites Entscheidungs-/Zonenoverlay |
| `preprocessing.camera_undistort.frame` | `output.display_output.frame` | LiveView auf dem entzerrten Bildkontext |

Die Debugoverlays von Tracker, Geometrie und Rotationsspur werden von der Engine
als `OverlayPayload` mitgeführt. Der eine vorhandene `output.overlay`-Node wird
für das zentrale Temporal-/Entscheidungsoverlay verwendet.

### 6.2 Noch nicht hinzufügen

- Kein `output.siemens_sps_write`.
- Kein `analysis.stem_orientation_model_decision`, solange kein separat
  validiertes Temporalmodell vorliegt.
- Kein `inference.temporal_sequence` für die deterministische Baseline.
- Kein `preprocessing.resize` vor `camera_undistort`. Das Kalibrierungsprofil
  verlangt die exakte Aufnahmeauflösung.
- Kein `preprocessing.frame_sampler` in der anfänglichen Rotationsspur.

### 6.3 Geometrie-only, solange das Rotationsprofil fehlt

Solange noch kein extern validiertes Rotationsprofil vorliegt, darf der
vollständige Winkelgraph nicht mit Ersatzwerten publiziert werden. Für die
frühe Kamera-, Segmentierungs-, Tracking- und Krümmungsprüfung kann stattdessen
eine eigene Geometrie-Version der kanalspezifischen Pipeline verwendet werden:

1. Den Graph bei `analysis.stem_mask_geometry` beenden.
2. `analysis.stem_mask_geometry.overlay` mit `output.overlay.result` verbinden.
3. `preprocessing.camera_undistort.frame` mit
   `output.display_output.frame` verbinden.
4. `analysis.stem_surface_rotation`,
   `analysis.stem_temporal_orientation` und den unverbundenen
   `output.result` aus dieser Version entfernen.
5. In der Change Note eindeutig `geometry-only / no angle` vermerken.

Damit sind Masken, Track-ID, Referenzachse, Mittellinie, Querschnitte und
projizierte normierte Krümmung prüfbar. Es entsteht bewusst noch kein
`stem.orientation.v2`-History-Result und keine Gradempfehlung. Nach Freigabe des
Rotationsprofils wird eine neue Pipelineversion mit dem vollständigen Graphen
aus Abschnitt 6.1 erstellt.

## 7. Nodes im Frontend konfigurieren

### 7.1 Channel frame input

Im Node **Channel frame input** als **Calibration camera** den aktuellen Kanal
verwenden. In der eingebetteten Kanalansicht ist der Kanal normalerweise bereits
der Authoring-Kontext. Diese Zuordnung ist Voraussetzung für den intrinsischen
Kalibrierungsassistenten.

### 7.2 Camera undistort und intrinsische Kalibrierung

1. Runtime und für exklusiven Kamerazugriff bei Bedarf auch Live-Stream des
   Kanals stoppen.
2. Node **Camera undistort** öffnen.
3. Boardtyp wählen:
   - `Checkerboard`: **Inner columns** und **Inner rows** korrekt angeben.
   - `ChArUco`: **Square columns**, **Square rows**, relative Kanten- und
     Markerlänge sowie das passende ArUco-Dictionary angeben.
4. **Minimum valid observations** festlegen; der UI-Ausgangswert ist 5.
5. **Maximum RMS reprojection error [px]** festlegen; der UI-Ausgangswert ist
   2 Pixel.
6. Das Board nacheinander an deutlich verschiedenen Positionen, Abständen und
   Neigungen im gesamten genutzten Bildbereich halten. Die schräge
   Produktionspose der Kamera bleibt unverändert.
7. Für jede Stellung **Capture board observation** wählen. Die Kamera wird pro
   Aufnahme exklusiv geöffnet und danach wieder freigegeben.
8. Unscharfe, abgeschnittene oder falsch erkannte Beobachtungen mit **Use
   observation N** abwählen.
9. **Calculate calibration** wählen.
10. Einzelne Reprojektionfehler, Gesamt-RMS und Status prüfen. Bei Ablehnung
    bessere Beobachtungen aufnehmen; nicht einfach die Fehlergrenze erhöhen.
11. In **Raw** und **Undistorted (pose preserved)** prüfen, dass gekrümmte
    Referenzlinien begradigt werden, die beabsichtigte schräge Perspektive aber
    sichtbar bleibt.
12. Erst bei Status `valid` **Apply valid profile to this pipeline version**
    wählen.
13. **Calibration resolution policy** auf `Require exact resolution` lassen.
14. **Remap interpolation** zunächst auf `Linear` lassen.

Das Profil wird mit der Pipelineversion gespeichert. Eine Änderung von
Auflösung, Zoom, Fokus, Objektiv oder Kameramontage erfordert eine neue
Kalibrierung und neue Pipelineversion.

### 7.3 Segmentation

1. Node **Segmentation** öffnen.
2. Unter **Selection policy** für die Inbetriebnahme **Pin deployment release**
   verwenden. Dadurch bleibt der Test reproduzierbar.
3. Unter **Deployed model** das freigegebene Instanzsegmentierungsmodell
   wählen. Der Task muss `segmentation` sein.
4. **Score threshold** zunächst beim Registry-Ausgangswert `0.35` belassen.
5. **Max segments** so begrenzen, dass alle realistischen Kandidaten
   enthalten sind; Ausgangswert ist 50.
6. Mit **Preview draft** kontrollieren, dass jeder Stamm als eigene Maske und
   mit der richtigen Klasse ausgegeben wird. Boxen ohne brauchbare Maske reichen
   für die Geometrie nicht aus.

Wenn kein Modell im Auswahlfeld erscheint, fehlt eine deployte
Segmentation-Capability. Dann zuerst den bestehenden Weg **Projekte &
KI-Modelle** beziehungsweise **Advanced → Deployment Control** abschließen.
Ein Download von Grundgewichten allein behebt diese Lücke nicht.

### 7.4 Active stem tracker

1. Node **Active stem tracker** öffnen.
2. **Transfer ROIs** im visuellen ROI-Editor auf den tatsächlichen
   Übergabebereich legen.
3. **Longitudinal conveyor ROIs** auf den anschließenden relevanten
   Längsförderbereich legen.
4. **Stem classes** exakt wie die Modellklasse eintragen. Mehrere Klassen
   werden kommasepariert angegeben. Ein leeres Feld akzeptiert alle Klassen und
   ist für die Abnahme nicht empfohlen.
5. **Minimum segmentation score** passend zum Segmentation-Node zunächst auf
   `0.35` setzen.
6. Den erwarteten Übergabepunkt mit **Expected handover X/Y** in normierten
   Bildkoordinaten eintragen.
7. **Expected motion direction** entsprechend der sichtbaren Förderrichtung
   setzen.
8. Die folgenden Ausgangswerte zunächst beibehalten und erst anhand
   aufgezeichneter Fehlerfälle ändern:
   - Mindest-ROI-Überdeckung `0.05`
   - erwartete sichtbare Gesamtlänge `0.55`
   - minimale sichtbare Länge `0.25`
   - maximale Zentroiddistanz `0.25`
   - minimale Akquisitionskonfidenz `0.35`
   - minimale Kontinuitätskonfidenz `0.5`
   - Ambiguitätsabstand `0.08`
   - Verdeckungsfrist `3` Frames
9. **Show tracking overlay** aktivieren.

Bei zwei annähernd gleich plausiblen Kandidaten muss der Tracker fail-closed
enden. Eine falsche sichere Auswahl darf nicht durch Absenken der
Ambiguitätsschwelle erzwungen werden.

### 7.5 Stem mask geometry

1. **Stem classes** und **Minimum segmentation score** identisch zum Tracker
   setzen.
2. **Instance selection** auf `Require exactly one match` setzen. Der
   vorgeschaltete Tracker soll genau eine aktive Instanz liefern;
   `Largest matching segment` ist nur die einfache stateless Entwicklungsregel.
3. **Centerline / residual samples:** 32.
4. **Width samples:** 16.
5. **Low-frequency smoothing passes:** zunächst 2.
6. **Robust end trim [%]:** zunächst 0,2.
7. **Clipped-end uncertainty window [%]:** zunächst 8.
8. Mindestfläche und minimale sichtbare projizierte Länge auf die reale
   Auflösung abstimmen; Ausgangswerte sind 300 Pixel und 60 Pixel.
9. **Show stem geometry overlay** aktivieren.

Im Overlay müssen Kontur, gerade Referenzachse, robuste Mittellinie,
Stützstellen sowie akzeptierte und unsichere Querschnitte nachvollziehbar sein.
Die Werte sind Pixel- beziehungsweise normierte Projektionswerte, keine
Millimeterwerte.

### 7.6 Visuelle Rotationskalibrierung

Vor diesem Schritt müssen Kamera, Intrinsik und Blickkonvention feststehen.

1. Das physische Stammende als **End A** oder **End B** eindeutig an Anlage und
   Dokumentation kennzeichnen.
2. Festlegen, ob positive Rotation von diesem Ende aus gesehen im oder gegen
   den Uhrzeigersinn verläuft.
3. Mit derselben Kamera, Auflösung und Intrinsik mehrere bekannte
   Winkelstellungen oder temporäre Umfangsmarkierungen aufnehmen.
4. Die Beobachtungen müssen mindestens enthalten:
   `observed_normalized_displacement`, `known_rotation_deg`,
   `stem_width_fraction`, `image_center_x` und `image_center_y`.
5. Das technische Kalibrierungswerkzeug muss
   `cv_runtime.stem_rotation.calibrate_stem_rotation` verwenden. Der Kern
   verlangt standardmäßig mindestens vier gültige Beobachtungen, mindestens
   30 Grad Winkelspanne und validiert mittleren beziehungsweise maximalen Fehler.
6. Nur ein Profil mit `profile.valid == true`, leerer `reason_codes`-Liste und
   unverändertem `calibration_hash` freigeben.
7. Als Übergabe an den Bediener die vollständige Ausgabe von
   `StemRotationCalibrationProfile.runtime_config()` verwenden.

> **Offen:** Aufnahme, Berechnung, Versionierung und Freigabe dieses Profils
> besitzen aktuell keine eigene Frontendseite und keinen Repository-CLI-Befehl.
> Bis dieser Bedienweg ergänzt ist, ist dies ein Engineering- und
> Freigabeschritt, kein reiner Operator-Schritt.

Im Node **Stem surface rotation** die Profilwerte unverändert in die
gleichnamigen Felder übertragen:

- Calibration ID, Version und Method
- vollständiger Intrinsic calibration hash
- Rotation viewpoint und Positive rotation direction
- Degrees per normalized displacement
- Validated calibration samples
- Mean und Maximum calibration error sowie deren erlaubte Grenzen
- gültiger Stammbreitenbereich
- gültiger X-/Y-Bildmittelpunktbereich
- vollständiger Rotation calibration hash

Die Werte **Rotation viewpoint** und **Positive rotation direction** müssen im
nachfolgenden Temporal-Node exakt gleich sein. Der Profilhash darf nicht manuell
neu berechnet oder nach einer Feldänderung weiterverwendet werden.

Die Featuretracking- und Quality-Gate-Werte zunächst auf Registry-Default
lassen. Insbesondere sind mindestens 8 passende Features, ein Inlieranteil von
0,55, maximale Trackingabweichung von 20 Pixeln und minimale
Rotationskonfidenz 0,35 vorgesehen. Abweichungen erst anhand gespeicherter
Traces und bekannter Winkel begründet versionieren.

### 7.7 Prozesszonen und Entscheidungslinie

Im Node **Stem temporal orientation** vier ROIs zeichnen. Der visuelle Editor
verwendet normierte Bildkoordinaten. Ein Rechteck wird durch Ziehen im Bild
angelegt; ein vorhandenes Rechteck kann in der Liste ausgewählt beziehungsweise
entfernt und numerisch nachbearbeitet werden.

Falls kein Bild im ROI-Editor erscheint, einen upstream Node wie **Camera
undistort** oder **Segmentation** auswählen und **Preview draft** ausführen oder
kurz einen bereits publizierten Kamera-Livepfad verwenden. Der Preview dient nur
als Zeichenunterlage, nicht als Temporaltest.

Die Zonen in Förderrichtung anlegen:

1. **Observation entry ROI:** Bereich, in dem eine qualitativ brauchbare
   Beobachtungssession beginnen darf.
2. **Throw-in ROI:** sichtbarer Einwurf-/Rollbereich, in dem Krümmung und
   relative Rotation gesammelt werden.
3. **Decision deadline ROI:** schmaler Bereich vor dem ersten Walzenkontakt.
   Beim ersten Eintritt wird die Entscheidung aus den **vorherigen** gültigen
   Beobachtungen eingefroren.
4. **First roller diagnostic ROI:** Bereich des ersten Walzenpaares, nur für
   Diagnose und eine spätere getrennte Nachkontrolle.

Wichtig: Die Zonenstufe wird anhand des mittleren Punkts der ermittelten
Stammmittellinie bestimmt, nicht anhand der vorauslaufenden Stammspitze. Die
Decision ROI muss deshalb so weit vor dem Walzenkontakt liegen, dass beim
Eintritt des **Stammzentrums** noch die gesamte reale Verarbeitungs- und
Reaktionsreserve vorhanden ist.

Überlappende Zonen werden nach der höchsten Stufe aufgelöst; eine Überlappung
mit Decision oder First roller kann somit vorzeitig entscheiden. Zonen für die
erste Abnahme nicht überlappen lassen.

Danach setzen:

- **Process-zone calibration ID:** stabiler Anlagen-/Kamera-Bezug.
- **Process-zone calibration version:** bei jeder geometrischen Änderung
  erhöhen.
- **Process-zone intrinsic hash:** vollständiger Hash des angewendeten
  intrinsischen Profils.
- **Rotation viewpoint** und **Positive rotation direction:** exakt wie im
  Rotationsprofil.

Die Baseline-Werte zunächst beibehalten:

- mindestens 4 Beobachtungen
- mindestens 20 Grad beobachtete Rotation
- minimale Rotationskonfidenz 0,45
- minimale Geometriequalität 0,5
- minimal sichtbarer Anteil 0,3
- Mindest-Bogenrelevanz 0,12
- minimale Ergebnis-Konfidenz 0,5
- No-Rotation-Deadband 5 Grad

Diese Schwellwerte sind Ausgangswerte, keine bereits an der Anlage validierten
Produktionsgrenzen.

### 7.8 Temporal-Sample-Recorder

Im selben Node unter **Temporal recording/import**:

- **Record temporal samples:** aktivieren, wenn Datensammlung gewünscht ist.
- **Temporal sample FPS:** Ausgangswert 15. Dieser Wert beschreibt die
  konstante FPS des erzeugten Clips; die Frameindex- und Timestamp-Zuordnung
  bleibt im Manifest erhalten.
- **Record diagnostic video:** für die Inbetriebnahme aktivieren.
- **Verification frames:** zunächst 0. Ein Wert größer 0 erzeugt ein getrennt
  gekennzeichnetes Nachkontrollasset und erweitert nicht den Hauptmodelleingang.
- **Maximum primary frames:** ausreichend für Eintritt bis Entscheidung; der
  Ausgangswert ist 900.
- Aufnahmequalität, Sichtbarkeit, Deduplication und Retention festlegen.
- **Temporal annotation schema:** `stem_orientation_angle.v1`.
- **Temporal dataset ID:** den notierten Dataset-Slug oder die interne
  Dataset-ID eintragen.
- **Automatically import recorded samples:** nur mit vorhandenem eindeutigem
  Projektmapping aktivieren.
- **Import every Nth sample** und **Maximum imports per hour** passend zur
  gewünschten Datenrate setzen.

Der Recorder ist Bestandteil des bestehenden Realtime-/Capturepfads. Es ist
kein separater Recorder-Node und kein eigener Dienst zu konfigurieren. Er
schreibt Frames nicht inline in die Datenbank, sondern übergibt die
Materialisierung asynchron an den vorhandenen Capture-Service.

### 7.9 Result und LiveView

Im **Result output** für den ersten vollständigen Stammvertrag:

1. `analysis.stem_temporal_orientation.result` mit dem Port `result` verbinden.
2. **Result projections** leer lassen.
3. **Wrap result under key** leer lassen.
4. Runtime-Metadaten aktiviert lassen.

Bei leerer Projection-Tabelle persistiert die Engine den vollständigen,
begrenzten `stem.orientation.v2`-ResultPayload. Sobald später eine Projection
angelegt wird, werden nur noch die explizit dem Ziel **History** zugeordneten
Felder gespeichert. Eine unvollständige Projection kann deshalb die spezielle
Stammansicht in der Result-History unbrauchbar machen.

Im **Display output** den entzerrten Frame anschließen. **Show enriched labels**
kann aktiviert bleiben. Im **Overlay output** das Temporal-Overlay anschließen.

## 8. Validieren, speichern und publizieren

1. **Validate** wählen.
2. Alle Fehler beseitigen. Typische Publish-Blocker sind:
   - kein deploytes Segmentation-Modell,
   - ungültige oder fehlende Intrinsik,
   - leere Pflicht-ROIs,
   - fehlende Kalibrierungs-IDs/Versionen/Hashes,
   - inkompatible oder doppelt belegte Ports,
   - fehlender Terminal-/Output-Node.
3. Einen upstream Node auswählen und **Preview draft** verwenden, um
   Entzerrung, Segmentmaske und Geometrie an einem Frame zu kontrollieren.
4. **Save version** wählen und eine aussagekräftige Change Note verwenden, die
   mindestens Modellrelease, Intrinsik-, Rotations- und Prozesszonenversion
   nennt.
5. Nach jeder Änderung erneut validieren und eine neue Version speichern.
6. Erst bei fehlerfreiem Stand **Publish** wählen.

Das Publizieren einer kanalspezifischen Pipeline bindet genau diese Version an
den owning Channel. Ein bereits laufender Kanal wird nach Bestätigung neu
gestartet. Der gespeicherte Pipelinegraph enthält auch das intrinsische Profil
und bleibt damit versioniert und rollbackfähig auf fachlicher Pipelineebene.

### Grenze von Preview draft

`Preview draft` verarbeitet einen Einzelrequest mit neuem
`RuntimeExecutionContext.state`. Damit lassen sich Ports, Masken,
Referenzachse, Mittellinie und ein einzelner Overlaystand prüfen. Eine stabile
Track-ID, kumulative Rotation oder Decision-Line-Entscheidung kann damit nicht
abgenommen werden. Dafür muss der publizierte Graph als laufender Channel eine
vollständige Sequenz verarbeiten.

## 9. Datei-Shadow-Test durchführen

1. Im Datei-Testkanal prüfen, dass die publizierte Pipeline ausgewählt ist.
2. **Runtime → Starten** wählen. Der **Live-Stream** ist separat und für die
   Inferenz nicht dasselbe wie die Runtime.
3. Im **LiveView** die verfügbaren Layer einschalten.
4. Den Ablauf in dieser Reihenfolge beobachten:
   - Segmentmaske umfasst genau den sichtbaren Stamm.
   - Tracker wählt genau eine Instanz und hält dieselbe Track-ID.
   - Bei zwei gleich plausiblen Stämmen wird keine sichere Instanz behauptet.
   - Referenzachse bleibt stabil; Mittellinie folgt dem niederfrequenten Bogen.
   - Lokale Astwülste dominieren den Bogenwert nicht.
   - Abgeschnittene Enden und unsichere Querschnitte werden markiert.
   - Translation des Stamms erzeugt keine relevante kumulative Rotation.
   - Die Rotationsspur zeigt mehrere Inlier und keine unerklärte
     Diskontinuität.
   - Beim Eintritt des Stammzentrums in die Decision ROI wird der letzte
     gültige Vor-Einzug-Frame eingefroren.
   - Nach diesem Zeitpunkt bleibt die Hauptempfehlung unverändert.
5. Nach Ende des Testvideos die Runtime manuell stoppen, falls der Kanal nicht
   bereits in einen Endzustand wechselt.

Ein positiver Test zeigt entweder eine begründete Gradempfehlung oder bewusst
`Keine sichere Winkelentscheidung – Bediener erforderlich`. Ein scheinpräziser
Winkel trotz ungültiger Kalibrierung, Diskontinuität oder Mehrdeutigkeit ist ein
Abnahmefehler.

## 10. Result-History und Schnellreview

Das entscheidungsbereite Result wird nur gespeichert, wenn am Kanal **Result
history** aktiviert und der Temporal-Result mit **Result output** verbunden ist.
Der erste eingefrorene Decision-Stand wird als Ereignis persistiert und nicht
auf jedem Folgeframe dupliziert.

1. **Results** öffnen.
2. Nach Kanal und Pipeline filtern.
3. Unter **Type** `Stem orientation` wählen. Alternativ Track- oder Session-ID
   im Suchfeld verwenden.
4. In **Vor-Einzug-Empfehlung** prüfen:
   - Track und Session,
   - Beobachtungsbeginn,
   - Decision Frame und Zeitpunkt,
   - Zielkandidat,
   - signierter beziehungsweise absoluter Winkel und Richtung,
   - Blickkonvention und positive Drehrichtung,
   - Konfidenz, Methode und Reason Codes,
   - Pipeline-, Modell-, Intrinsik-, Rotation-, Prozesszonen- und
     Featureversion,
   - kompakte Krümmungs- und Rotationsspur,
   - Roh-/Diagnosevideo und ausgewählte Frames, sobald materialisiert.
5. Kontrollieren, dass eine optionale **Getrennte Nachkontrolle** die
   Vor-Einzug-Empfehlung nicht ersetzt.

Im integrierten Schnellreview erfassen:

- **Vorschlag plausibel:** Ja / Nein / Unklar
- **Richtung:** Korrekt / Falsch / Unklar
- **Winkel:** Grob zu klein / Passend / Zu groß / Nicht beurteilbar
- **Bogen:** Relevant / Nahezu gerade / Unklar
- **Finale Lage:** Good / Acceptable / Bad
- optional Ziel-Frame von/bis, Sonderfall und Kommentar

Mit **Neue Review-Revision speichern** wird eine neue append-only Revision
angelegt. Dieses Review ist Diagnose und Vorannotation, aber nicht das
verbindliche Winkel-Label.

## 11. Automatische Samples und Label-Studio-Bedienung

Bei aktivem Recorder beginnt ein Sample, sobald ein aktiver Track in der
Observation entry/Throw-in-Stufe die Aufnahmequalität erfüllt. Der Primärclip
endet am gespeicherten Decision Frame. Spätere Walzenframes gelangen nicht
unbemerkt in den Hauptclip.

Der bestehende Capturepfad materialisiert:

- `sequence.mp4`
- optional `diagnostic.mp4`
- `features.jsonl`
- `rotation-trace.jsonl`
- `sequence-manifest.json`
- optional ein klar getrenntes Verification-Asset

Bei aktivem Autoimport und genau einem passenden Mapping wird idempotent genau
eine Videoaufgabe in Label Studio angelegt. Fehlende Medien, ein fehlendes oder
mehrdeutiges Mapping und Schemaabweichungen führen zu `needs_attention`, nicht
zu einem geratenen Importziel.

### Verbindliches Labeln in Label Studio

1. In **Advanced → Manual Project Mapping → Known Label Studio Projects**
   beim Stammprojekt **Open Project** wählen.
2. Video vollständig bis zum Clipende ansehen.
3. `sequence_valid` als `usable` oder `unusable` bewerten.
4. `bow_relevance` als `relevant`, `nearly_straight` oder `unclear` bewerten.
5. `target_visible` als `yes`, `no` oder `unclear` bewerten.
6. Nur bei `usable + relevant + target_visible=yes` genau einen kurzen
   Timeline-Bereich `target_orientation` markieren, in dem der projizierte
   Bogen am besten vertikal beziehungsweise minimal erschien.
7. `final_orientation_quality` für das Entscheidungsfenster als `good`,
   `acceptable` oder `bad` setzen.
8. Sonderfall und Kommentar bei Bedarf ergänzen.
9. Annotation speichern.

Der Labeler gibt **keinen Gradwert** ein. Der versionierte Target Builder leitet
den signierten Winkel später aus dem Zielintervall und der gespeicherten
Rotationsspur ab. Bei `nearly_straight`, nicht sichtbarem Ziel oder ungültiger
Rotationsspur entsteht kein erzwungenes Winkelziel.

### Snapshot anfordern

- Bei aktivem Auto-Snapshot wird nach den konfigurierten qualifizierenden
  Annotationen automatisch ein Snapshotjob angelegt.
- Manuell im Bereich **Known Label Studio Projects** die Aktion **Create
  Snapshot** verwenden.
- Der allgemeine **Create Snapshot**-Button auf **Snapshot Operations** ist für
  `temporal_sequence` derzeit absichtlich deaktiviert; für diesen Typ den
  Projekt-Mapping-Pfad benutzen.

## 12. Optionales TCN-/GRU-Modell

Die deterministische Baseline muss zuerst abgenommen und bleibt auch bei einem
späteren Modell parallel erhalten. Der vorhandene Backendpfad kennt
`temporal_sequence`, TCN und GRU, Temporal-Snapshots, Modellkatalog,
Deploymentpakete und den Runtime-Node `inference.temporal_sequence`.

> **Offene Frontendgrenze:**
> `frontend/apps/admin-web/src/features/training/training-page.tsx` besitzt in
> `defaultForm` und `recipeForArchitecture` derzeit keinen eigenen
> `temporal_sequence`-Zweig. Ein Temporal-Dataset kann deshalb im aktuellen
> Formular auf Bildklassifikations-/Anomaly-Defaults fallen. Solange diese
> Verdrahtung nicht korrigiert und separat abgenommen ist, darf kein
> TCN-/GRU-Job über **Advanced → Training Jobs** als regulärer Operatorablauf gestartet
> werden.

Liegt später ein separat freigegebenes und deploytes Temporalmodell vor, wird
der Graph allgemein so erweitert:

| Von | Zu |
|---|---|
| `analysis.stem_temporal_orientation.sequence` | `inference.temporal_sequence.sequence` |
| `analysis.stem_temporal_orientation.result` | `analysis.stem_orientation_model_decision.baseline` |
| `inference.temporal_sequence.prediction` | `analysis.stem_orientation_model_decision.prediction` |
| `analysis.stem_orientation_model_decision.result` | `output.result.result` |
| `analysis.stem_orientation_model_decision.overlay` | `output.overlay.result` |

Das Modell wird im Temporal-Inference-Node wie jedes andere Modell über eine
deployte Capability gebunden. Schema-, Mindestsequenz-, Confidence- und
Baseline-Abweichungsgates müssen fail-closed bleiben. Es erfolgt keine
automatische Produktions-Promotion.

## 13. Live-Shadow-Inbetriebnahme an der Kamera

Erst nach bestandenem Datei-Test auf den realen Kamerakanal wechseln.

1. Kamerabefestigung, Auflösung, Belichtung, Fokus und Beleuchtung nochmals
   protokollieren.
2. Intrinsische Kalibrierung auf genau diesem Kanal durchführen.
3. Rotationskalibrierung mit genau diesem Intrinsik-Hash freigeben und
   eintragen.
4. Tracker- und Prozess-ROIs auf dem realen entzerrten Livebild neu zeichnen.
5. Decision ROI mit dokumentierter Zeit-/Wegreserve vor dem ersten
   Walzenkontakt positionieren.
6. Pipeline validieren, als neue Version speichern und publizieren.
7. Runtime starten; den Live-Stream nur zusätzlich für die Bedieneranzeige
   starten.
8. Zunächst ohne Maschinenkopplung mehrere reale Stämme im Shadow Mode
   beobachten.
9. Für jeden Fall Overlay, eingefrorenes Result, Reason Codes, Trackstabilität
   und Decision-Zeitpunkt prüfen.
10. Mit bekannten Referenzdrehungen kontrollieren, dass Richtung und Winkel
    innerhalb der freigegebenen Kalibrierungsfehler liegen.
11. Reine Translation, kurzzeitige Verdeckung, zwei plausible Kandidaten,
    Featureverlust und Runtime-Neustart gezielt prüfen.
12. Result-Reviews und Label-Studio-Labels sammeln; Schwellenänderungen nur als
    neue Pipeline-/Kalibrierungsversion vornehmen.

### Abnahmekriterien für den Shadow-Pfad

- Die Intrinsik ist `valid`; Produktionsframe und Kalibrierung haben exakt
  dieselbe Auflösung.
- Gerade Referenzlinien sind nach Undistort gerade, die schräge Pose bleibt
  erhalten.
- Das Segmentierungsmodell liefert geschlossene, brauchbare Instanzmasken.
- Eine normale Bewegung behält dieselbe Track-ID; ein Stammwechsel erzeugt eine
  neue ID.
- Ambiguität endet ohne sichere aktive Instanz.
- Gerade Stämme liegen bei der normierten Krümmung nahe null; C-Bögen beider
  Richtung haben entgegengesetzte Vorzeichen.
- Lokale Astmerkmale verändern den niederfrequenten Bogen nicht
  unkontrolliert.
- Reine Bildtranslation erzeugt keine relevante kumulative Rotation.
- Bekannte Kalibrierungsdrehungen werden innerhalb der freigegebenen Fehler
  rekonstruiert.
- Der Decision Frame liegt nachweislich vor dem ersten Walzenkontakt.
- Die Empfehlung wird am Decision Frame eingefroren und danach nicht
  überschrieben.
- Ungültige Kalibrierung, Spurunterbrechung, zu wenig Rotation, Mehrdeutigkeit
  oder geringe Qualität ergeben `manual_required` ohne Gradzahl.
- Das Result erscheint in **Results → Stem orientation** mit vollständiger
  Richtungs- und Versionsprovenienz.
- Der Primärclip endet am Decision Frame; ein Verification-Asset ist getrennt.
- Es existiert kein SPS-Write-Node im Graph und keine Maschinenaktion wird
  ausgelöst.

Erst wenn diese Punkte über eine ausreichend repräsentative Shadow-Evaluation
dokumentiert sind, kann ein eigener, ausdrücklich freigegebener Auftrag für die
Maschinenkopplung beginnen.

## 14. Häufige Reason Codes und Reaktion

| Reason Code beziehungsweise Zustand | Bedeutung | Bedienerreaktion |
|---|---|---|
| `waiting_for_observation_entry` | Stammzentrum hat die Startzone noch nicht erreicht | ROI-Lage und Förderrichtung prüfen |
| `rotation_calibration_invalid` | Profil unvollständig, fehlerhaft oder nicht freigegeben | Kein Winkelbetrieb; validiertes Originalprofil eintragen |
| `rotation_calibration_hash_mismatch` | Profilfelder und Hash gehören nicht zusammen | Profil nicht manuell reparieren; neu aus Originalartefakt übernehmen |
| `rotation_calibration_intrinsic_mismatch` | Rotationsprofil wurde mit anderer Intrinsik erzeugt | Kamera-/Intrinsikversion korrigieren und Rotation neu kalibrieren |
| `rotation_calibration_outside_width_range` | Stamm liegt außerhalb des kalibrierten Breitenbereichs | Kalibrierungsbereich erweitern und neu validieren, nicht Gate abschalten |
| `rotation_calibration_outside_image_region` | Stamm liegt außerhalb des kalibrierten Bildbereichs | Kamera-/ROI-Lage oder Kalibrierungsdatensatz korrigieren |
| `rotation_feature_count_insufficient` | Zu wenige Rindenmerkmale | Fokus, Belichtung, Bewegungsunschärfe, Maskenerosion und Oberflächentextur prüfen |
| `rotation_feature_inlier_ratio_low` | Merkmale bewegen sich nicht konsistent | Verdeckung, Schlupf, falsche Maske oder lokale Astdominanz prüfen |
| `rotation_trace_discontinuous` | Track-/Frame-/Featurekontinuität wurde unterbrochen | Fall als manuell behandeln; Ursache in Trace und Overlay prüfen |
| `geometry_quality_insufficient` | Maske oder Querschnitte reichen nicht aus | Segmentierung, Sichtbarkeit und Geometrieschwellen prüfen |
| `observation_count_insufficient` | Vor Decision zu wenige gültige Frames | Observation ROI früher setzen oder reale FPS/Latenz prüfen |
| `insufficient_observed_rotation` | Beobachtete Winkelspanne ist zu klein | Beobachtungsfenster und Einwurfbewegung prüfen; keine Gradzahl erzwingen |
| `target_not_observed` | Kein belastbarer Projektionsminimum-Kandidat | `manual_required`; mehr geeignete Beobachtung erforderlich |
| `target_candidate_ambiguous` | Mehrere Zielstellungen sind gleich plausibel | `manual_required`; Fall für Review/Labeling markieren |
| `ambiguous_s_bow` | S-förmige/komplexe Projektion ist nicht eindeutig | `manual_required`; als Sonderfall labeln |
| `decision_line_missed_first_roller_reached` | Erst die Walzenzone wurde erreicht | Decision ROI/Latenz sofort korrigieren; keine Freigabe |
| `bow_not_relevant` / `no_rotation_deadband` | Stamm nahezu gerade oder Korrektur im Deadband | Erwartetes Ergebnis `no_rotation` |
| `needs_attention` beim Sample | Medien, Dataset, Mapping oder Label-Studio-Import nicht eindeutig | Dataset-Slug, Schema, Auto-Sync und genau ein Projektmapping prüfen |

## 15. Bekannte Bediengrenzen

Diese Punkte dürfen bei der Inbetriebnahme nicht als bereits gelöst
angenommen werden:

1. **Kein Frontendassistent für Rotationskalibrierung.** Der testbare Kern ist
   vorhanden, Aufnahme/Persistenz/Freigabe sind aber noch kein durchgängiger
   Operatorworkflow.
2. **Kein eingebautes Stammpipeline-Template.** Die kanalspezifische Pipeline
   muss aus der vorhandenen Realtime-Vorlage umgebaut werden.
3. **Preview ist nur ein Einzelrequest.** Temporal-State wird dort nicht über
   mehrere Aufrufe geteilt.
4. **Kein expliziter Datei-Beschleunigungsfaktor.** `file_test` ist nicht die
   reale Latenzabnahme.
5. **Temporaltraining noch nicht sicher aus dem aktuellen Trainingsformular.**
   Backend, TCN/GRU und Artefaktpfad sind vorhanden, die Task-spezifischen
   Frontenddefaults fehlen.
6. **Kein Produzent für einen numerischen post-rotation Residualwert in der
   ersten Baseline.** Ein separates Verification-Video kann aufgezeichnet
   werden; es überschreibt nie die Hauptempfehlung.
7. **Runtime-Neustart verwirft den ephemeren Node-State.** Ein neuer Lauf startet
   sicher ohne alte Empfehlung. Ein vor der Decision abgebrochener Recorderfall
   wird als unvollständig behandelt, aber es gibt keinen Handler-Finalize-Hook
   für ein eigenes `aborted_runtime_restart`-Hauptresult.
8. **Keine 3D- oder Millimeteraussage.** Die Einzelkamera liefert eine
   projizierte Baseline; metrische Längen und räumliche Bogenrichtung sind nicht
   Bestandteil dieses Vertrags.

## 16. Reale Implementierungsstellen

Die Anleitung stützt sich auf folgende vorhandene Pfade und Symbole:

- Node-Katalog und Defaultkonfigurationen:
  `backend/packages/cv-runtime/src/cv_runtime/registry.py` —
  `BUILTIN_NODE_TYPES`
- Pipeline-Designer, Standardfelder, ROI-Editor und Intrinsikassistent:
  `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`
  — `NodeField`, `VisualBoxEditor`, `CameraIntrinsicCalibrationEditor`
- Kanalbedienung und LiveView:
  `frontend/apps/admin-web/src/features/channels/channel-list-page.tsx` —
  `ChannelListPage`; `frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx`
  — `ChannelDetailPage`, `LiveViewPanel`
- Intrinsischer Kern und Undistort:
  `backend/packages/cv-runtime/src/cv_runtime/camera_calibration.py` —
  `calculate_camera_calibration`;
  `backend/packages/cv-runtime/src/cv_runtime/nodes/camera_undistort.py` —
  `CameraUndistortHandler`
- Stammgeometrie und aktiver Tracker:
  `backend/packages/cv-runtime/src/cv_runtime/stem_geometry.py` —
  `analyze_stem_geometry`;
  `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py` —
  `ActiveStemTrackerHandler`, `StemMaskGeometryHandler`
- Rotationskalibrierung und Temporal-Baseline:
  `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py` —
  `calibrate_stem_rotation`, `StemRotationCalibrationProfile.runtime_config`;
  `backend/packages/cv-runtime/src/cv_runtime/stem_temporal.py` —
  `evaluate_projected_pre_entry_baseline`, `wrap_to_half_turn`;
  `backend/packages/cv-runtime/src/cv_runtime/nodes/stem_temporal.py` —
  `StemSurfaceRotationHandler`, `StemTemporalOrientationHandler`,
  `_process_stage`
- Stateful Realtime-Ausführung und Recorder:
  `backend/services/realtime-executor/src/realtime_executor/live.py` —
  `_STATEFUL_PIPELINE_NODE_TYPES`, `_pipeline_max_inflight`;
  `backend/services/realtime-executor/src/realtime_executor/temporal_samples.py`
  — `RealtimeTemporalSampleRecorder`
- Materialisierung und Label-Studio-Import:
  `backend/services/capture-service/src/capture_service/executor.py` —
  `_materialize_temporal_sequence_capture`, `_resolve_temporal_import_target`;
  `backend/packages/cv-datasets/src/cv_datasets/temporal_import.py` —
  `build_temporal_label_studio_task`
- Dataset-/Projektbedienung:
  `frontend/apps/admin-web/src/features/datasets/datasets-page.tsx` —
  `DatasetsPage`;
  `frontend/apps/admin-web/src/features/datasets/project-mapping-page.tsx` —
  `ProjectMappingPage`
- Result-History und Review:
  `frontend/apps/admin-web/src/features/results/result-history-page.tsx` —
  `StemOrientationResultSummary`, `StemOrientationReviewPanel`
- Temporal-Trainingsgrenze im Frontend:
  `frontend/apps/admin-web/src/features/training/training-page.tsx` —
  `defaultForm`, `recipeForArchitecture`
- Persistenz und Updategrenzen:
  `deployment/README.md` — Abschnitte zur Temporal-Kompatibilität und zu
  persistenten Datenpfaden

Fachliche Grundlage bleiben `Spezifikation.md`,
`docs/stem-orientation/implementation-map.md` und
`docs/stem-orientation/temporal-angle-model-adr.md`.
