# YOLO OBB und Label Studio Umsetzungsplan

Stand: 2026-06-01

## Implementierungsstand

Die OBB-Erweiterung ist umgesetzt und durch die Anwendung verkabelt:

- Neuer Task-Typ `oriented_detection`.
- `Create Model` bietet bei Detection-Projekten `Detection` und
  `Oriented Detection` an.
- Label Studio bleibt bei `Object Detection with Bounding Boxes` /
  `RectangleLabels`.
- Normale Detection blockiert rotierte Rechtecke mit einem klaren Hinweis.
- Oriented Detection wandelt alle Rechtecke in vier normalisierte Eckpunkte um
  und schreibt YOLO-OBB-Labels.
- Ultralytics nutzt `yolo26{size}-obb` und das Rezept
  `baseline_obb_yolo`.
- Realtime, Batch, Public API, Runtime-Pipelines und Label-Studio-Predictions
  transportieren OBB-Geometrie. Zusaetzlich wird weiterhin eine
  achsparallele Projektionsbox geliefert.

## Ergebnis der Pruefung

Kurzfazit: Oriented Bounding Boxes funktionieren in der aktuellen Anwendung
nicht end-to-end als einfacher Schalter. Label Studio kann Rotation in
`RectangleLabels` erfassen, aber die Plattform behandelt Detection aktuell als
achsparallele Bounding Boxes.

Relevante aktuelle Grenzen:

- `BoundingBox` speichert nur `x`, `y`, `width`, `height` und keine Rotation
  oder vier Eckpunkte.
- Die Label-Studio-Konvertierung liest `rectanglelabels`, ignoriert aber
  `value.rotation`.
- Die Training-ready Detection-Dateien werden im normalen YOLO-Format mit
  fuenf Spalten geschrieben: `class cx cy width height`.
- Das Trainings-Staging erzeugt COCO-Detection mit achsparallelem `bbox` und
  konvertiert danach wieder in normale YOLO-Labels.
- Die Modellmatrix kennt nur `classification`, `detection`, `segmentation` und
  `anomaly`. Es gibt keinen OBB-Task oder Geometrie-Modus.
- Inferenz- und Public-API-Vertraege liefern nur `x_min`, `y_min`, `x_max`,
  `y_max`.
- Der Label-Studio-ML-Adapter schreibt Detection-Predictions immer als
  `rectanglelabels` mit `"rotation": 0`.

Ultralytics erwartet fuer OBB-Datasets dagegen das YOLO-OBB-Format
`class_index x1 y1 x2 y2 x3 y3 x4 y4` mit normalisierten Eckpunkten. OBB-Modelle
verwenden den `-obb` Modell-Suffix, zum Beispiel `yolo26n-obb.pt`. Label Studio
speichert bei rotierten Rechtecken `x`, `y`, `width`, `height` und `rotation`,
wobei die gespeicherte Rotation auf den linken oberen Punkt des Rechtecks
bezogen ist.

## Zielbild

OBB sollte als eigene Geometrie sauber durch die Plattform laufen, ohne die
bestehende achsparallele Detection zu veraendern:

- Annotation: Label Studio `RectangleLabels` mit Rotation bleibt die Quelle.
- Snapshot: Canonical Dataset speichert vier normalisierte Eckpunkte und
  optional die Label-Studio-Rohparameter.
- Training: Ultralytics YOLO-OBB erhaelt 9-spaltige YOLO-OBB-Labels.
- Deployment: ONNX/TensorRT-Metadaten markieren den Output als OBB.
- Inferenz: API und UI liefern neben Label/Score die vier Eckpunkte und
  optional `xywhr`.
- Label Studio Predictions: Vorhersagen erscheinen wieder als rotierte
  Rechtecke, nicht als achsparallele Boxen.

## Empfohlene technische Entscheidung

Eine neue Modellklasse ist sauberer als `detection` zu ueberladen. Das
Label-Studio-Projektschema bleibt dabei gleich: weiterhin
`Object Detection with Bounding Boxes` bzw. `RectangleLabels`.

Empfohlen:

- Neuer Task-Typ: `oriented_detection`
- Alternativ, falls die UI klein bleiben soll: `detection` plus explizites
  Feld `geometry = "axis_aligned" | "oriented"`.

Ich wuerde `oriented_detection` bevorzugen, weil APIs, Metriken,
Modellvorlagen, Label-Studio-Predictions und Runtime-Nodes dann nicht
zweideutig werden. Bestehende Detection-Projekte bleiben unveraendert.

## Automatische Erkennung und Create-Model-UX

Die automatische Erkennung sollte nicht ueber ein anderes Label-Studio-Schema
laufen. Label Studio liefert bei beiden Varianten `RectangleLabels`; der
entscheidende Unterschied ist nur, ob `value.rotation` in den Roh-Annotationen
vorkommt bzw. ungleich 0 ist.

Gewuenschter Bedienablauf:

1. Operator erstellt oder nutzt ein Label-Studio-Projekt mit
   `Object Detection with Bounding Boxes`.
2. In `Create Model` erscheint fuer solche Projekte zusaetzlich zur normalen
   `Detection` die Modellklasse `Oriented Detection`.
3. Wenn `Oriented Detection` gewaehlt wird, materialisiert die Plattform
   dieselben `RectangleLabels` als OBB-Dataset. Rechtecke ohne Rotation werden
   als OBB mit 0 Grad bzw. achsparallelen vier Eckpunkten uebernommen.
4. Wenn normale `Detection` gewaehlt wird und spaeter rotierte Boxen im
   Label-Studio-Roh-Export gefunden werden, soll die Snapshot-Validierung nicht
   stillschweigend flatten. Sie soll mit einem klaren Hinweis blockieren oder
   mindestens warnen: "Rotierte Bounding Boxes gefunden, bitte Oriented
   Detection verwenden."

Damit ist die Erkennung praktisch automatisch: Das Label-Studio-Setup bleibt
identisch, die Plattform scannt die Annotationen auf Rotation und die
ausgewaehlte Modellklasse entscheidet, ob daraus normale YOLO-Labels oder
YOLO-OBB-Labels entstehen.

Implementationsdetails im bestehenden Code:

- Frontend: `modelCreationTaskOptions(...)` muss fuer Detection-Projekte
  `['detection', 'oriented_detection']` anbieten, nicht nur den Projekt-Typ.
- Frontend: `taskTypeLabel(...)` und `defaultTemplateForTask(...)` muessen
  `Oriented Detection` und z.B. `oriented_detection-yolov26-n` kennen.
- Backend: `TaskType` und generierte Schemas muessen
  `oriented_detection` enthalten.
- Backend: Die Retarget-Logik fuer Modellanlage muss
  `detection <-> oriented_detection` als kompatibles Paar erlauben, so wie
  heute `classification <-> anomaly` speziell behandelt wird.
- Backend: Fuer den ersten Schnitt sollte ein Projekt entweder normale
  Detection-Modelle oder Oriented-Detection-Modelle enthalten. Mischbetrieb in
  einem Projekt ist spaeter moeglich, braucht aber eine sauberere Trennung von
  `annotation_task_type` und `model_task_type`.
- Dataset: Die Snapshot-Validierung soll `rotation != 0` zaehlen und im
  Manifest ablegen, z.B. `rotated_rectangle_count`.
- Dataset: Fuer `oriented_detection` werden alle Rechtecke in vier Punkte
  umgerechnet; fuer `detection` fuehrt `rotated_rectangle_count > 0` zu einem
  Validierungsfehler oder expliziten Warnstatus.

## Umsetzungspakete

1. Datenmodell und Label-Studio-Konvertierung
   - `TaskType.ORIENTED_DETECTION` ergaenzen.
   - `OrientedBoundingBox` oder `RotatedBoundingBox` mit vier Punkten
     einfuehren.
   - `RectangleLabels.value.rotation` aus Label Studio lesen.
   - Aus `x`, `y`, `width`, `height`, `rotation` die vier normalisierten
     Eckpunkte berechnen.
   - Bestehende Detection ohne Rotation weiterhin exakt wie bisher behandeln.
   - Tests fuer `rotation=0`, positive/negative Rotation, Clipping und
     kleine Split-Datasets ergaenzen.

2. Snapshot-Materialisierung und Dataset-Staging
   - Training-ready OBB-Labels im Format
     `class x1 y1 x2 y2 x3 y3 x4 y4` schreiben.
   - Eigenen Staging-Pfad fuer Ultralytics OBB bauen, statt COCO-Detection zu
     verwenden.
   - Manifest-Metadaten um `format = "ultralytics_yolo_obb_labels"` ergaenzen.
   - Alte Snapshots koennen nicht nachtraeglich aus `canonical_dataset.json`
     repariert werden, wenn Rotation dort bereits verloren ging. Fuer OBB
     muessen Snapshots aus dem Label-Studio-Roh-Export neu materialisiert
     werden.

3. Training Backend
   - Ultralytics-Trainer fuer `oriented_detection` freischalten.
   - Modellstems auf `yolo26{size}-obb` abbilden, z.B. `yolo26n-obb`.
   - Pretrained/Scratch-Strategie fuer `-obb.pt` und `-obb.yaml` ergaenzen.
   - Metrik-Normalisierung fuer OBB-Box-Metriken testen.
   - OpenMMLab/MMYOLO fuer diesen Pfad nicht verwenden, solange der aktuelle
     MMYOLO-v8-Pfad nur normale Detection abdeckt.

4. Modellkatalog, Templates und UI
   - OBB-Templates in der Capability Matrix ergaenzen:
     `YOLOv26 OBB n/s/m/l/x`.
   - Projekt-/Modellerstellung um den neuen Task-Typ erweitern.
   - Operator-Texte und Default-Metriken anpassen.
   - OpenAPI/Frontend-Schemas neu erzeugen.

5. Inferenz, Runtime und Label Studio Predictions
   - Contract fuer OBB-Vorhersagen definieren:
     `label`, `score`, `points[4]`, optional `xywhr`.
   - Triton/ONNX-Postprocessing fuer OBB-Ausgaben implementieren.
   - Label-Studio-ML-Adapter OBB-Predictions wieder in
     `RectangleLabels` mit `rotation` konvertieren.
   - Batch-, Realtime- und Result-History-Ansichten um OBB-Ausgabe erweitern.
   - Runtime-Nodes, die nur achsparallele Detection erwarten, entweder
     ablehnen oder mit expliziter Axis-Aligned-Projektion versorgen.

6. Tests und Abnahme
   - Unit-Tests fuer Geometrie-Konvertierung Label Studio -> YOLO OBB.
   - Snapshot-Materialisierung mit 9-spaltigen Labels pruefen.
   - Ultralytics-Dataset-YAML und Modellstems pruefen.
   - Inferenz-Postprocessing mit synthetischen OBB-Tensoren pruefen.
   - Label-Studio-Prediction-Payload mit `rotation != 0` pruefen.
   - Optionaler Smoke-Test mit kleinem OBB-Dataset und `yolo26n-obb`.

## Vorbereitung fuer das Offline Update

Der normale Ablauf "Update exportieren und einspielen" bleibt moeglich, wenn
keine neue native Runtime-Abhaengigkeit benoetigt wird. Bei OBB ist vorab
wichtig:

1. Runtime-Version pruefen
   - In der Ziel-WSL pruefen, ob die installierte Training-Runtime eine
     Ultralytics-Version mit YOLO-OBB und den gewuenschten `yolo26*-obb`
     Modellen enthaelt.
   - Wenn eine neue Ultralytics-Version gepinnt oder nachinstalliert werden
     muss, reicht ein normales Update-ZIP nicht. Dann muss die vorbereitete
     WSL-Runtime neu exportiert werden.

2. Pretrained-Weights offline bereitstellen
   - Plattformweiter Zielzustand: Alle Transfer-Learning-Gewichte liegen lokal
     im gleichen Cache:
     `/srv/cv-platform/control/pretrained-checkpoints`.
   - Der Pfad wird bei Installation und Update angelegt und bleibt persistent.
   - Fallback-Pfad fuer bestehende Runtime-Installationen:
     `/opt/ybb-runtime/pretrained-checkpoints`.
   - Der Pfad kann ueber `CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT` ueberschrieben
     werden. Mehrere Pfade koennen mit dem OS-Pfadtrenner angegeben werden.
   - Wenn Transfer Learning aktiv ist, sucht jeder Trainer zuerst bzw. im
     Offline-Betrieb ausschliesslich in diesem Cache. Fehlt die Datei, soll der
     Trainingsstart mit einer klaren Meldung scheitern, statt still online zu
     laden oder heimlich auf Scratch umzuschalten.
   - Ultralytics/YOLO-Dateinamen folgen den nativen Modellnamen, z.B.
     `yolo26n.pt`, `yolo26n-cls.pt`, `yolo26n-seg.pt`,
     `yolo26n-obb.pt`.
   - OpenMMLab YOLOv8 akzeptiert die offiziellen `.pth`-Dateinamen oder
     Aliasnamen wie `detection-yolov8-n.pth`.
   - Torchvision akzeptiert Aliasnamen wie
     `classification-resnet-v1-18.pth`, `classification-resnet18.pth`,
     `segmentation-deeplab-v3-resnet50.pth` sowie die offiziellen
     PyTorch-Dateinamen.
   - PatchCore/Anomaly erwartet den lokalen WideResNet-Backbone, z.B.
     `anomaly-patchcore-anomalib-wide_resnet50_2.pth` oder einen offiziellen
     Torchvision-Dateinamen wie `wide_resnet50_2-95faca4d.pth`.
   - Fuer OBB konkret benoetigt, je nach ausgewaehlter Groesse:
     `yolo26n-obb.pt`, `yolo26s-obb.pt`, `yolo26m-obb.pt`,
     `yolo26l-obb.pt`, `yolo26x-obb.pt`.
   - Die `YBB-Deployment-UI.exe` hat dafuer den Reiter `Modelle`.
     Auf dem Entwicklungs-PC werden dort alle oder ausgewaehlte
     Pretrained-Gewichte als `ybb-*-pretrained-models-update.zip`
     exportiert. Dieses ZIP wird wie gewohnt nach `incoming` kopiert und auf
     dem Zielsystem im gleichen Reiter mit `Modelle einspielen` installiert.
   - CLI-Alternative:
     `deployment\export-pretrained-models-update.ps1 -All` oder gezielt
     `-Models ultralytics-oriented-detection-yolo26-n`.

3. Update-Inhalt vorbereiten
   - Code, Tests, OpenAPI-Vertraege und Frontend-Schemas committen.
   - Falls DB-Migrationen noetig sind, Migration im Update belassen; der
     Windows-Updater fuehrt Migrationen im normalen Ablauf aus.
   - Falls nur Code/Frontend/Contracts geaendert werden und die Runtime schon
     passt: normales Windows-Update-ZIP exportieren.

4. Vor dem Export pruefen
   - Backend Unit-Tests fuer Dataset, Training-Backend, Inferenz-Gateway und
     Label-Studio-ML-Adapter ausfuehren.
   - Frontend Tests fuer Task-Auswahl und Ergebnisanzeige ausfuehren.
   - Einen lokalen OBB-Snapshot erzeugen und pruefen, dass die Labeldateien
     neun Werte pro Zeile enthalten.

5. Offline einspielen
   - Wie bisher Update-ZIP am Entwicklungsrechner exportieren.
   - ZIP nach
     `C:\YBB_Inferenzserver\ybb-inferenzserver\incoming` kopieren.
   - Auf dem Zielrechner ueber `YBB-Deployment-UI.exe` im Reiter `Update`
     einspielen.
   - Nach dem Update Label Studio, Training Worker, Inference Gateway und
     Frontend kurz pruefen.

6. Abnahme auf dem Zielsystem
   - In Label Studio ein rotiertes Rechteck annotieren.
   - Snapshot erzeugen und Materialisierung pruefen.
   - Training mit einem kleinen OBB-Modell starten.
   - Deployment/Inference gegen ein Testbild ausfuehren.
   - Label-Studio-Predictions pruefen: Rotation muss sichtbar und im JSON
     erhalten sein.

## Grobe Einschaetzung

Das ist eine mittlere bis groessere Erweiterung, nicht nur ein Flag. Der
kritische Teil ist weniger Label Studio selbst, sondern die durchgaengige
Geometrie-Erhaltung von Annotation ueber Training bis Inferenz und UI.

Ein pragmatischer erster Release-Schnitt waere:

- `oriented_detection`
- Ultralytics YOLOv26 OBB Nano/Small/Medium/Large/XL
- Training + ONNX Export
- Realtime Inferenz und Label-Studio-Predictions
- Batch/Runtime-Nodes mit klarer Axis-Aligned-Projektion plus optionaler
  OBB-Geometrie

Damit bleibt das Offline-Update ueberschaubar, solange die vorhandene
Training-Runtime OBB-faehig ist und die Pretrained-Weights vorab lokal
bereitliegen.
