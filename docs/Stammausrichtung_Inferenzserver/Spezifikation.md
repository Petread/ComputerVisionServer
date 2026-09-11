# Spezifikation – Stammausrichtung durch kamerabasierte Krümmungsanalyse

## 0. Dokumentstatus

- **Dokumentversion:** 2.2 – Vor-Einzug-Winkelentscheidung, Timeline-Labeling und abgeleitete Winkelziele
- **Zielsystem:** bestehende Anwendung „Inferenz Server“
- **Analysiertes Repository:** `backend`, `config`, `contracts`, `deployment`, `docs`, `frontend`
- **Zielgerät:** Windows 11 Pro mit vorbereiteter WSL2-Runtime, NVIDIA RTX 4500 Ada, ohne Docker-Abhängigkeit am Zielgerät
- **Backend:** bestehende Python-/FastAPI-Services
- **Frontend:** bestehende Vite-/React-Anwendung `frontend/apps/admin-web`
- **Implementierungsprinzip:** Erweiterung der bestehenden Runtime-Pipeline; keine zweite Anwendung und keine parallele Kamera-, Trainings-, Modell- oder Ergebnisplattform
- **Einführungsstufen:** Offline-Test → Livebeobachtung → Shadow Mode → assistierte SPS-Anbindung → optional geschlossene Regelung
- **Sicherheitsprinzip:** Die Bildverarbeitung liefert zunächst Beobachtungen und Empfehlungen. Verriegelung, Maschinenfreigabe, Begrenzungen und Aktorlogik bleiben in der SPS.

### 0.1 Verbindlichkeit und Reihenfolge der Unterlagen

Für die Umsetzung gilt folgende Reihenfolge:

1. `AGENTS.md` enthält dauerhafte Repository-Regeln.
2. Dieses Dokument beschreibt Ziel, Architektur, Datenverträge und Akzeptanzkriterien.
3. `docs/stem-orientation/implementation-map.md`, das durch Codex-Prompt 00 erstellt wird, bestätigt die exakten Erweiterungspunkte des tatsächlich geöffneten Repository-Stands.
4. Die freigegebene `Codex_Prompts_v4_PreEntry_Angle.md` zerlegt die Umsetzung in kleine Arbeitspakete.
5. Für das spätere Zeitmodell ist zusätzlich eine freigegebene `docs/stem-orientation/temporal-model-adr.md` erforderlich. Die Datensammlung und das Labeling funktionieren bereits vorher über den allgemeinen Temporal-Dataset-Pfad.

Wenn sich ein konkreter Dateipfad durch einen späteren Repository-Stand ändert, wird nicht geraten. Die Änderung wird zuerst in der `implementation-map.md` dokumentiert.

---

# 1. Prozessproblem und Ziel

Rundholzstämme werden vom Querförderer in den Längsförderer eingeworfen. Beim Einwurf rollen sie mehr oder weniger stark um ihre Längsachse. Anschließend kann das erste Vorzentrierungs-Walzenpaar den Stamm drehen.

Die nachfolgende Vorzentrierung arbeitet in der horizontalen Ebene symmetrisch. Sie kann den Stamm nur dann prozessgerecht führen, wenn der dominante Stammbogen möglichst **vertikal** steht. Liegt der Bogen horizontal, wird ein gekrümmter Stamm trotz mittigem Schließen der Walzen schräg beziehungsweise ungünstig in die Anlage geführt.

Der Bediener nutzt heute hauptsächlich die Beobachtung **vor dem Einzug**:

1. Er verfolgt den Stamm beim Einwurf und auf dem Weg zum ersten Walzenpaar.
2. Er sieht, wie sich die sichtbare Krümmung während des Rollens verändert.
3. Er merkt sich ungefähr die beste beobachtete Umfangslage.
4. Kurz vor beziehungsweise beim Greifen des ersten Walzenpaars kennt er bereits Drehrichtung und grobe Drehmenge.
5. Nach der Hauptdrehung kontrolliert er nur noch, ob eine kleine Nachkorrektur nötig ist.

Die Software soll genau diesen Ablauf nachbilden. Eine offene Suche erst während des Drehens wäre für den Maschinenzyklus zu langsam und ist nicht das Hauptverfahren.

## 1.1 Fachliches Endziel

Das System soll:

1. das Kamerabild intrinsisch entzerren,
2. einzelne Stämme segmentieren,
3. den aktuell relevanten Stamm auswählen und bis zur Entscheidungslinie verfolgen,
4. aus der Maske eine robuste projizierte Stammgeometrie erzeugen,
5. aus Rinden-, Ast- und Oberflächenmerkmalen die tatsächliche relative Stammrotation über die Zeit verfolgen,
6. innerhalb der Einwurfsequenz eine gute beziehungsweise beste beobachtete Zielorientierung erkennen,
7. den letzten gültigen Entscheidungsframe **vor** dem ersten Vorzentrierungs-Walzenpaar bestimmen,
8. daraus eine signierte Korrektur modulo 180° berechnen, beispielsweise `−43°` beziehungsweise `43° gegen den Uhrzeigersinn`,
9. Richtung, Winkel, Konfidenz und Reason Codes vor dem Walzenkontakt im Shadow Mode bereitstellen,
10. später die vorab berechnete Hauptdrehung über den bestehenden Siemens-SPS-Pfad anfordern,
11. nach der Hauptdrehung nur noch eine begrenzte Restabweichung kontrollieren und gegebenenfalls klein nachkorrigieren.

## 1.2 Wichtige fachliche Einschränkung

Die geplante Produktionskamera blickt bewusst **schräg** auf den Stamm. Eine einzelne schräge Ansicht liefert nur eine Projektion der räumlichen Stammform. Deshalb gilt:

> Aus einem einzelnen Frame darf keine belastbare absolute Drehkorrektur abgeleitet werden.

Eine Gradangabe ist nur zulässig, wenn mindestens folgende Informationen vorliegen:

- eine zusammenhängende zeitliche Beobachtung desselben Stamms,
- eine versionierte visuelle Rotationsspur,
- eine eindeutige Blick- und Vorzeichenkonvention,
- ein beobachteter oder durch ein validiertes Zeitmodell geschätzter Zielzustand,
- ausreichende Qualitäts- und Konfidenzwerte.

Die deterministische Baseline darf zunächst ein `projected_baseline`-Ergebnis liefern. Eine produktive Automatik setzt eine validierte Zielerkennung und eine unabhängige Winkelprüfung auf einem Gold-Testbestand voraus.

## 1.3 Nicht-Ziele der ersten Ausbaustufe

- keine vollständige 3D-Rekonstruktion des Stamms,
- keine Rundum-Punktwolke,
- keine garantierte metrische 3D-Bogenmessung aus einer einzelnen schrägen Kamera,
- keine vom Labeler geratenen Winkelangaben,
- keine offene Testimpuls- oder Extremwertsuche als primäre Maschinenstrategie,
- keine neue eigenständige FastAPI- oder Vite-Anwendung,
- keine neue allgemeine Modelltrainings-API neben den vorhandenen Diensten,
- kein Video-Transformer als erste Modellarchitektur,
- keine produktive SPS-Schreibfreigabe vor abgeschlossener Shadow-Evaluation,
- keine Unterstützung des separaten Edge-Agent-Runtimes in der ersten Ausbaustufe,
- keine Docker-Abhängigkeit am Zielgerät.

# 2. Verifizierter Ist-Zustand des Inferenz Servers

Die neue Funktion wird auf bestehenden Mechanismen aufgebaut.

## 2.1 Runtime-Nodes und Payloads

Die wesentlichen Verträge befinden sich in:

- `backend/packages/cv-runtime/src/cv_runtime/handlers.py`
- `backend/packages/cv-runtime/src/cv_runtime/context.py`
- `backend/packages/cv-runtime/src/cv_runtime/payloads.py`
- `backend/packages/cv-runtime/src/cv_runtime/registry.py`
- `backend/packages/cv-runtime/src/cv_runtime/engine.py`
- `backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py`
- `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py`

Ein Node-Handler implementiert:

```python
execute(context, node, inputs) -> NodeExecutionResult
```

Die Runtime verfügt bereits über typisierte Payloads für Frames, Segmentierung, Messungen, Events, Overlays und Resultate. Fachlich komplexe Nodes sind bereits durch `cv_runtime.nodes.board_cut` vorgelebt. Die Stammfunktion darf daher eigene, klar begrenzte Fach-Nodes erhalten.

## 2.2 Zustandsbehaftete Liveverarbeitung

`RuntimeExecutionContext` besitzt:

```python
state: dict[str, Any]
```

Der Realtime Executor hält im Livebetrieb einen gemeinsamen `pipeline_state` über aufeinanderfolgende Frames. In:

```text
backend/services/realtime-executor/src/realtime_executor/live.py
```

werden zustandsbehaftete Node-Typen über `_STATEFUL_PIPELINE_NODE_TYPES` erkannt. Sobald ein solcher Node enthalten ist, wird die Pipelineausführung serialisiert und der gemeinsame Zustand an die Frames weitergegeben.

Für die Stammausrichtung bedeutet das:

- `analysis.active_stem_tracker` ist stateful.
- `analysis.stem_temporal_orientation` ist stateful.
- Beide Node-Typen müssen in die bestehende Stateful-Erkennung aufgenommen werden.
- Jeder Node speichert ausschließlich unter `context.state[node.node_key]` oder einem gleichwertig eindeutig namensbasierten Schlüssel.
- Bei Runtime-Neustart, Pipelinewechsel oder Kanalneustart wird kein alter Stammzustand fortgesetzt.

Die initiale Umsetzung refaktoriert die Stateful-Erkennung nicht allgemein. Eine spätere Registry-Eigenschaft `stateful=true` ist nur eine optionale Plattformverbesserung und nicht Voraussetzung dieser Funktion.

## 2.3 Pipeline-Designer und Preview

Der Node-Katalog wird aus der bestehenden Runtime-Registry bereitgestellt und im vorhandenen Designer dargestellt:

```text
frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx
```

Normale Property-Typen sollen ohne eigene neue Seite verwendet werden. Eine eigene Frontend-Komponente ist nur für die interaktive intrinsische Kamerakalibrierung erforderlich.

Der vorhandene Preview-Pfad befindet sich in:

```text
backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py
POST /runtime/channels/{channel_id}/pipeline-preview
```

Ein Einzelbild-Preview kann Geometrie und Overlay prüfen. Ein zustandsbehafteter Zeitablauf wird dagegen mit einer echten Liveausführung oder einer `file_test`-Videoquelle geprüft, weil einzelne Preview-Aufrufe keinen vollständigen Stammablauf darstellen.

## 2.4 Kamera- und Dateiquellen

Der Realtime Executor unterstützt bereits Livekameras sowie Bilder und Videos als Testquelle:

```text
backend/services/realtime-executor/src/realtime_executor/sources.py
```

`FileTestFrameSource` kann statische Bilder und Videodateien abspielen. Deshalb wird für F1–F4-Tests und aufgezeichnete Sequenzen zunächst **keine neue Offline-Importanwendung** gebaut. Testbilder und Videos werden über den vorhandenen Channel-/File-Test-Pfad ausgeführt.

## 2.5 Vorhandener Kalibrierungs-Frame-Workflow

Ein Einzelbild kann bereits exklusiv aus einer gestoppten Kamera aufgenommen werden:

```text
POST /runtime/channels/{channel_id}/calibration-frame
```

Der Pipeline-Designer verwendet diesen Workflow bereits für die Brettkalibrierung. Die intrinsische Kamerakalibrierung erweitert genau diesen Bedienpfad. Es entsteht kein zweiter Kamera-Capture-Service.

## 2.6 Liveoverlay und Result-History

Die bestehende Runtime erzeugt Overlays und stellt sie über den vorhandenen Kanalpfad bereit. Persistierbare Ergebnisse laufen über:

- `ResultPayload`
- `output.result`
- die vorhandene Result-History und `ResultRecord`
- die vorhandenen History Policies des Kanals

Die Stammausrichtung soll diese Strukturen nutzen. Große Bilder, Masken oder Videos dürfen nicht als Base64-Blöcke in der Result-History gespeichert werden.

## 2.7 Training und Modellverwaltung

Das Segmentierungsmodell passt bereits in den bestehenden Task-Typ `segmentation` und nutzt die vorhandene Label-Studio-, Dataset-, Trainer-, Modell- und Deployment-Infrastruktur.

Für Zeitmodelle ist der Ist-Stand anders. `TaskType` umfasst aktuell:

- `classification`
- `detection`
- `oriented_detection`
- `segmentation`
- `anomaly`

Trainerkatalog, Modellkatalog, Export, Deployment und Inference Gateway sind auf diese Bildaufgaben ausgerichtet. Ein TCN oder eine GRU wird daher nicht einfach als vorhandener Task ausgegeben. Vor der Implementierung ist eine eigene ADR erforderlich.

## 2.8 Zieldeployment

Das vorhandene, empfohlene Zieldeployment ist ein Docker-freier Windows-Installer mit vorbereiteter WSL2-Runtime. Spätere Updates werden über die bestehenden Export-, Apply- und Rollback-Skripte eingespielt. Die Stammausrichtung erhält keinen eigenen Installer.

## 2.9 Ausführungsziel der ersten Version

Die erste Version wird für Runtime-Kanäle mit:

```text
execution_target = server
```

umgesetzt. Der separate Edge-Agent besitzt eine eigene, begrenzte Node-Registry und ist nicht Teil der ersten Freigabe. Eine Edge-Unterstützung wäre ein eigenes späteres Arbeitspaket.

---

# 3. Verbindliche Zielarchitektur

## 3.1 Zielpipeline

Die endgültige Server-Runtime-Pipeline lautet:

```text
source.channel_frame oder source.file_test
        ↓ frame
preprocessing.camera_undistort
        ↓ entzerrter frame
inference.segmentation
        ↓ segments
analysis.active_stem_tracker                         [stateful]
        ├── active_stem / gefilterte Segmente
        └── track_id
                ↓
analysis.stem_mask_geometry
        ↓ stem_geometry

entzerrter frame + active_stem + stem_geometry
        ↓
analysis.stem_surface_rotation                       [stateful]
        ↓ stem_rotation

stem_geometry + stem_rotation
        ↓
analysis.stem_temporal_orientation                   [stateful]
        ├── stem.orientation.v2
        └── overlay
                ↓
        output.overlay / output.display_output
                ↓
        output.result
                ↓ später optional
        output.siemens_sps_write
```

Falls der reale Pipeline-Executor einen separaten Surface-Rotation-Node unnötig erschwert, darf dessen Handler als klar getrennte Komponente im Temporal-Node aufgerufen werden. Payload, Tests und Verantwortungsgrenze bleiben trotzdem separat definiert. Die Entscheidung wird in `implementation-map.md` dokumentiert.

## 3.2 Warum diese Aufteilung

### Bildbezogene, zustandslose Schritte

- intrinsische Bildentzerrung,
- Segmentierungsinferenz,
- Geometrieberechnung aus einer ausgewählten Maske.

### Zeitliche, zustandsbehaftete Schritte

- Auswahl und Tracking des aktiven Stamms,
- relative Oberflächenrotation und deren Confidence,
- Session-Lebenszyklus,
- Zielorientierungs- und Entscheidungsframe-Erkennung,
- Vor-Einzug-Winkelberechnung,
- spätere Nachkontrolle.

Diese Schritte verwenden `RuntimeExecutionContext.state` und müssen im Realtime Executor als stateful gekennzeichnet werden.

### Fachliche Bedienoberflächen

- intrinsischer Kalibrierungsassistent,
- Definition von Beobachtungs-, Einwurf-, Entscheidungs- und Walzenzonen,
- Rotationskalibrierung mit bekannten Testwinkeln,
- Shadow-Auswertung,
- Session-Review,
- Datensatzfreigabe und Label-Studio-Verknüpfung.

## 3.3 Begründete Node-Grenzen

Es werden weiterhin keine Nodes für jedes kleine Teilergebnis gebaut. Separat sinnvoll sind jedoch:

- `analysis.active_stem_tracker`, weil die Objektidentität über Frames erhalten werden muss,
- `analysis.stem_mask_geometry`, weil die Maskengeometrie zustandslos und allgemein testbar ist,
- `analysis.stem_surface_rotation`, weil die relative Drehspur einen eigenen Payload, eigene Kalibrierung, Confidence und Wiederverwendungspotenzial besitzt,
- `analysis.stem_temporal_orientation`, weil dort Zielzustand, Decision Deadline und fachliche Empfehlung zusammengeführt werden.

Nicht separat erforderlich sind zunächst ein eigener Frame-Quality-Service, ein paralleler Session-Service oder eine zweite Runtime.

## 3.4 Verhältnis zur schrittweisen Implementierung

Der Geometrie-Node bleibt mit einem normalen `SegmentPayload` testbar. Der Surface-Rotation-Kern muss offline mit bekannten Winkelspuren prüfbar sein. Der Temporal-Node kann zunächst die deterministische Baseline verwenden und später dasselbe Input-/Output-Schema mit TCN oder GRU ergänzen.

# 4. Fachlicher Ablauf im Livebetrieb

## 4.1 Zustand vor dem Einwurf

- Mehrere Stämme können auf dem Querförderer sichtbar sein.
- Der Tracker bewertet nur Kandidaten in den konfigurierten Prozess-ROIs.
- Der Stamm nahe der Übergabe erhält eine Track-ID.
- Solange die Zuordnung nicht eindeutig ist, liefert das System `unknown` und keine Winkelangabe.

## 4.2 Einwurf und Beobachtungsphase

- Der Stamm rollt, springt oder rutscht beim Einwurf.
- Unscharfe Frames dürfen das Tracking unterstützen, aber nicht zwingend Geometrie oder Winkel aktualisieren.
- Die Maske liefert pro Frame die projizierte Stammgeometrie.
- Rinden-, Ast- und Oberflächenmerkmale liefern eine relative Rotationsspur.
- Die Temporal-Analyse führt Krümmung über kumulativer Rotation und erkennt Zielkandidaten.
- Rutschen wird von Rotation getrennt; eine Förderbewegung allein darf den Winkel nicht verändern.

## 4.3 Entscheidung vor dem ersten Walzenpaar

- Die Entscheidungslinie ist kamerakalibriert und versioniert.
- Der letzte qualitativ gültige Frame vor dieser Linie wird `decision_frame`.
- Spätestens dort wird die Hauptempfehlung eingefroren:

```text
Drehe 43° gegen den Uhrzeigersinn
Konfidenz 0,84
```

- Ist der Stamm nahezu gerade, der Zielzustand nicht beobachtet oder die Rotationsspur unsicher, lautet das Ergebnis `no_rotation` beziehungsweise `manual_required`.
- Keine Frames aus der späteren Walzendrehung dürfen rückwirkend in die Hauptentscheidung einfließen.

## 4.4 Hauptdrehung und Nachkontrolle

- Die SPS darf den vorab berechneten Auftrag erst ausführen, wenn das erste Walzenpaar den Stamm sicher kontrolliert.
- Die Hauptdrehung ist Feed-forward und nicht das Ergebnis einer offenen Testimpuls-Suche.
- Bleibt der Stamm sichtbar, kann die Kamera die tatsächlich ausgeführte Drehung und eine kleine Restabweichung kontrollieren.
- Eine Nachkorrektur ist begrenzt und überschreibt nie unbemerkt die ursprüngliche Empfehlung.

## 4.5 Sessionabschluss

Eine Session endet durch:

- Überschreiten der Entscheidungslinie und Erzeugung des Hauptresults,
- Verlassen des relevanten Bereichs,
- neuen aktiven Stamm,
- Timeout,
- Track-/Qualitätsverlust,
- Runtime-/Pipeline-Neustart.

Ein optionaler Nachkontrollabschnitt wird als getrennte Phase beziehungsweise getrenntes Artefakt geführt.

# 5. Intrinsische Kamerakalibrierung und Entzerrung

## 5.1 Ziel

Korrigiert werden:

- radiale tonnen- oder kissenförmige Objektivverzeichnung,
- tangentiale Verzeichnung,
- kamerainterne Brennweiten- und Hauptpunktparameter.

Nicht korrigiert wird:

- die bewusst schräge Kamerapose,
- die Perspektive zu einer künstlichen Draufsicht,
- die räumliche Höhe des Stamms.

Die schräge Ansicht bleibt Teil des Bildsignals.

## 5.2 Kalibrierungsmethode

Unterstützt werden:

- ChArUco als bevorzugte Methode,
- Checkerboard als alternative Methode.

Der Assistent benötigt mehrere gültige Aufnahmen mit unterschiedlichen Positionen und Neigungen des Kalibrierboards. Er berechnet:

- Kameramatrix,
- Distortionskoeffizienten,
- Kalibrierungsauflösung,
- Reprojektionfehler gesamt und je Aufnahme,
- gültigen Ausgabebereich beziehungsweise ROI,
- Profilhash und Erstellungszeitpunkt.

## 5.3 Speicherentscheidung der ersten Version

Die Kalibrierung wird zunächst **im Config-Feld des Nodes `preprocessing.camera_undistort`** gespeichert. Gründe:

- die Werte sind klein,
- Pipelineversionen sind bereits versioniert und publizierbar,
- die Kalibrierung ist eindeutig dem konkreten Channel-/Pipeline-Stand zugeordnet,
- es ist keine neue generische Profilverwaltung erforderlich,
- Rollback stellt automatisch die frühere Kalibrierung wieder her.

Ein eigenständiges, wiederverwendbares Camera-Calibration-Profile mit eigener Datenbanktabelle wird erst eingeführt, wenn mehrere Pipelines oder Kanäle dasselbe Profil teilen müssen. Diese Erweiterung ist nicht Bestandteil der ersten Version.

Kalibrierungsbilder werden nicht in der Node-Konfiguration gespeichert. Diagnosebilder können über die vorhandene Capture-/Artefaktlogik referenziert werden.

## 5.4 Kalibrierungsschema

Die Node-Konfiguration enthält mindestens:

```json
{
  "schema_version": "camera.intrinsic-calibration.v1",
  "status": "valid",
  "source_channel_id": "...",
  "image_width": 2560,
  "image_height": 1440,
  "distortion_model": "opencv_standard",
  "camera_matrix": [0, 0, 0, 0, 0, 0, 0, 0, 1],
  "distortion_coefficients": [0, 0, 0, 0, 0],
  "rms_reprojection_error_px": 0.0,
  "accepted_observation_count": 0,
  "board_definition": {},
  "valid_roi": {},
  "generated_at": "...",
  "calibration_hash": "..."
}
```

## 5.5 Runtime-Node `preprocessing.camera_undistort`

Eingang:

- `FramePayload`

Ausgang:

- neues `FramePayload` mit neuem `RuntimeImageContext`

Anforderungen:

- Bildauflösung muss zur Kalibrierung passen.
- Eine bewusst erlaubte Skalierung muss explizit konfiguriert und getestet sein.
- Ohne valide Kalibrierung gilt Fail-closed oder expliziter Bypass nach Konfiguration; stilles Verwenden ungültiger Werte ist verboten.
- Remap-Matrizen werden gecacht und nicht pro Frame neu berechnet.
- Der Cache ist anhand von Kalibrierungshash, Auflösung und Node-Konfiguration begrenzt und threadsicher.
- Der Node ist nicht allein wegen dieses Performance-Caches als fachlich stateful einzustufen.

## 5.6 Erweiterung der Bildprovenienz

`RuntimeImageTransform` wird um den Transformtyp `undistort` erweitert. Da die Koordinatenabbildung nicht affine ist, wird sie in der ersten Version konservativ als nicht allgemein projektierbar gekennzeichnet. Alle Segmentierungen und Stammoverlays entstehen **nach** der Entzerrung und beziehen sich daher direkt auf den entzerrten Bildkontext.

Eine allgemeine Rückprojektion von Overlays zwischen Roh- und Undistort-Bild ist nicht Voraussetzung der ersten Version.

## 5.7 Kalibrierungs-UI

Der bestehende Calibration-Frame-Capture wird wiederverwendet. Die neue strukturierte Node-Property `camera_intrinsic_calibration` bietet:

- Bild aufnehmen,
- Board erkennen,
- Aufnahme annehmen oder verwerfen,
- Liste der Beobachtungen,
- Kalibrierung berechnen,
- Vorher-/Nachher-Vorschau,
- Reprojektionfehler,
- Ergebnis in die Node-Konfiguration übernehmen.

## 5.8 Akzeptanzkriterien

- Synthetisch oder real bekannte gerade Linien werden nach der Entzerrung deutlich gerader.
- Eine falsche Auflösung führt zu einem klaren Fehler.
- Die schräge Pose bleibt erhalten.
- Eine publizierte ältere Pipelineversion behält ihre frühere Kalibrierung.
- Update und Rollback verlieren die Kalibrierungsdaten nicht.

---

# 6. Segmentierungsmodell

## 6.1 Aufgabe

Das Segmentierungsmodell erkennt jeden sichtbaren Stamm als eigene Instanz. Es muss insbesondere Situationen abdecken, in denen mehrere Stämme dicht nebeneinanderliegen oder sich teilweise berühren.

## 6.2 Bestehende Infrastruktur

Verwendet werden:

- vorhandene Bildaufnahme und Capture-Workflows,
- vorhandene Label-Studio-Anbindung,
- vorhandene Dataset-/Snapshot-Funktionen,
- vorhandener Task-Typ `segmentation`,
- vorhandene Trainer und Modellverwaltung,
- vorhandener `inference.segmentation`-Node.

Es wird kein neuer Segmentierungstrainer gebaut.

## 6.3 Annotation

- Jeder Stamm wird als eigene Instanz annotiert.
- Teilweise sichtbare Stämme werden trotzdem getrennt markiert.
- Maschinenbauteile gehören nicht zur Stammmaske.
- Bewegungsunschärfe, unterschiedliche Rindenarten, Nässe, helle Holzstellen, Schatten und Verschmutzung müssen im Datensatz enthalten sein.
- Training, Validierung und Test werden nach Aufnahmefolgen beziehungsweise Stämmen getrennt, damit benachbarte Frames nicht in unterschiedliche Splits geraten.

## 6.4 Qualitätsgrenzen

Konfigurierbar sind mindestens:

- relevante Klassenlabels,
- Mindestscore,
- Mindestmaskenfläche,
- Mindestlänge,
- zulässiger ROI-Anteil,
- maximale Fragmentierung.

---

# 7. Auswahl und Tracking des aktiven Stamms

## 7.1 Node

```text
analysis.active_stem_tracker
```

Dieser Node ist stateful.

## 7.2 Eingänge

- `segments` als `SegmentPayload`,
- optional `frame` für visuelle Motion-/Texturmerkmale,
- optional `events`, sofern später vorhandene Maschinenereignisse als `EventsPayload` bereitstehen.

## 7.3 Ausgang

Der Ausgang bleibt ein `SegmentPayload`, enthält aber:

- genau eine aktive Stamminstanz,
- eine stabile `track_id` in der zugehörigen `RuntimeObjectReference`,
- Auswahlscore und Auswahlgrund in den Metadaten,
- Status `candidate`, `active`, `lost` oder `ambiguous`,
- ursprünglichen `image_context_id`,
- keine Änderung der Maskengeometrie.

Diese Entscheidung vermeidet einen unnötigen neuen Payload ausschließlich für die Instanzauswahl.

## 7.4 Auswahlkriterien

- Prozess-ROI für Übergabe und Längsförderer,
- Nähe zur Übergabeposition,
- Bewegungsrichtung,
- zeitliche Kontinuität,
- Form-, Längen- und Flächenähnlichkeit,
- Überlappung beziehungsweise Distanz zum vorherigen Track,
- Reihenfolge im Prozess,
- optional Maschinenereignisse.

## 7.5 Rutschen und Rollen

Das Tracking darf Querbewegung nicht automatisch als Rotation interpretieren. Es trennt:

- globale Translation der Maske,
- Änderung der Stammachse im Bild,
- sichtbare Textur-/Konturbewegung,
- Änderungen der projizierten Geometrie.

Ein absoluter Rollwinkel ist in der ersten Version nicht verpflichtend.

## 7.6 State-Lifecycle

Der State wird unter dem Node-Key gespeichert und enthält nur begrenzte Daten:

- aktuelle Track-ID,
- letzte Objektgeometrie,
- letzte Zeitstempel und Sequenznummern,
- Kandidatenstatus,
- Lost-Zähler,
- optional kompakte Motion-Merkmale.

Frames und vollständige Masken werden nicht unbegrenzt im State gehalten.

## 7.7 Abbruchbedingungen

- Track länger als konfiguriert verloren,
- neuer eindeutiger Stamm übernimmt,
- Zeitstempelsprung,
- Bildauflösung oder Bildkontext ändert sich,
- Pipeline-/Runtime-Neustart.

Bei unklarer Zuordnung wird keine automatische Empfehlung ausgegeben.

---

# 8. Deterministischer Geometriekern

## 8.1 Ziel

Der Geometriekern erhält genau eine Stammmaske beziehungsweise ein Polygon und erzeugt eine robuste Beschreibung der **projizierten** Stammform im entzerrten Kamerabild.

Er ist unabhängig von FastAPI, Datenbank, Pipeline-Registry und Frontend testbar.

## 8.2 Geometrische Normalisierung

Die Form wird in ein stammeigenes 2D-Koordinatensystem überführt:

- Längskoordinate `s` von 0 bis 1,
- Normalrichtung quer zur projizierten Stammachse,
- globale Translation entfernt,
- globale Schräglage im Bild entfernt,
- projizierter Bogen bleibt erhalten.

Die schräge Kameraperspektive wird dadurch **nicht** in eine Draufsicht umgerechnet.

## 8.3 Algorithmus

Mindestablauf:

1. Polygon oder Maske validieren.
2. Kleine Fragmente und Löcher nach konfigurierbaren Grenzen bereinigen.
3. Größte plausible zusammenhängende Komponente bestimmen.
4. Initiale Längsrichtung robust über PCA oder ein gleichwertiges Verfahren schätzen.
5. In vielen Längspositionen Querschnitte orthogonal zur initialen Achse auswerten.
6. Je Querschnitt robuste linke/rechte Maskengrenze und Mittelpunkt bestimmen.
7. Unsichere oder durch Astwülste stark verfälschte Querschnitte markieren beziehungsweise begrenzen.
8. Aus den gültigen Mittelpunkten eine robuste gerade Referenzachse bestimmen.
9. Mittelpunkte auf normierte Längspositionen resamplen.
10. Niederfrequentes Residualprofil quer zur Referenzachse glätten.
11. Breitenprofil getrennt resamplen.
12. Qualitätskennwerte und Reason Codes berechnen.

## 8.4 Referenzachse und Bogenkennwerte

Die globale Lage wird entfernt, aber nicht die gesuchte Form. Die Referenzachse ist daher eine robuste Gerade durch die Stammmitten, nicht die gekrümmte Mittellinie selbst.

Der vorzeichenbehaftete Hauptbogen wird bevorzugt aus der geglätteten Mittellinie relativ zu einer robusten Endsehne im nutzbaren Längsbereich bestimmt. Dadurch ist das Vorzeichen für C-Bögen stabiler als ein einfacher Mittelwert der Residuen.

Mindestkennwerte:

- `signed_bow_px`,
- `rms_residual_px`,
- `max_abs_residual_px`,
- `peak_to_peak_px`,
- `median_width_px`,
- `signed_bow_normalized`,
- `rms_residual_normalized`,
- gültiger Längsanteil,
- gültiger Querschnittsanteil,
- Shape-Klasse `straight`, `c_bow`, `s_bow_or_complex`, `uncertain`.

Normierte Werte werden durch eine robuste mittlere Stammbreite geteilt. Millimeterwerte sind nur zulässig, wenn später eine geeignete metrische Kalibrierung vorhanden ist.

## 8.5 Standardauflösung der Profile

- Mittellinie/Residualprofil: standardmäßig 32 Stützstellen,
- Breitenprofil: standardmäßig 16 Stützstellen,
- zusätzlich Gültigkeitsmasken für fehlende beziehungsweise unsichere Stützstellen.

## 8.6 Qualitätsmerkmale

- Maskenscore,
- sichtbarer Längsanteil,
- Anteil gültiger Querschnitte,
- Enden vollständig/abgeschnitten,
- Fragmentierungsgrad,
- Blur-Schätzung, falls ein Frame vorliegt,
- Clipping am Bildrand,
- lokale Ausreißeranzahl,
- Stabilität der Referenzachse.

## 8.7 Fehler- und Reason Codes

Mindestens:

```text
no_matching_segment
multiple_segments_without_selection
mask_too_small
mask_too_fragmented
insufficient_visible_length
insufficient_cross_sections
both_ends_clipped
geometry_unstable
image_context_missing
frame_quality_too_low
```

Fail-closed bedeutet: Ein ungültiger Frame erzeugt keine gültige Orientierungsentscheidung, darf den bestehenden Track aber nicht automatisch löschen.

---

# 9. Runtime-Payload `StemGeometryPayload`

## 9.1 Zweck

Der Geometrie-Node gibt einen typisierten Payload aus, der direkt vom Temporal-Node verarbeitet werden kann.

## 9.2 Schema

```text
payload_type: stem_geometry
schema_version: stem.geometry.v1
track_id
source_object_id
image_context_id
frame_sequence
timestamp
reference_axis
normalized_s[32]
centerline_image_xy[32]
residual_profile_px[32]
residual_valid_mask[32]
width_profile_px[16]
width_valid_mask[16]
signed_bow_px
rms_residual_px
max_abs_residual_px
peak_to_peak_px
median_width_px
signed_bow_normalized
rms_residual_normalized
visible_fraction
valid_cross_section_fraction
shape_class
quality_score
reason_codes[]
feature_schema_version
metadata
```

## 9.3 Provenienz

Der Payload referenziert:

- den entzerrten `image_context_id`,
- das ausgewählte Quellobjekt,
- Track-ID,
- Segmentierungsmodellroute aus Upstream-Metadaten,
- Kalibrierungshash,
- Geometriealgorithmus- und Feature-Schema-Version.

## 9.4 Overlay

Der Geometrie-Node kann zusätzlich ein `OverlayPayload` ausgeben mit:

- Stammkontur,
- Referenzachse,
- geglätteter Mittellinie,
- Querschnittspunkten,
- verworfenen Bereichen,
- aktuellem Bogenwert und Qualitätsstatus.

Das Overlay bezieht sich direkt auf den entzerrten Bildkontext.

---

# 10. Node `analysis.stem_mask_geometry`

## 10.1 Eingang

- required: `segments`
- optional: `frame`

## 10.2 Ausgang

- `stem_geometry`
- optional `overlay`

## 10.3 Konfiguration

- Klassenfilter,
- Mindestscore,
- Auswahlstrategie für die Übergangsphase,
- Anzahl Profilstützstellen,
- Mindestlänge und Mindestfläche,
- Endbeschnitt,
- Glättungsstärke,
- Ausreißergrenzen,
- Qualitätsgrenzen,
- Debugoverlay ein/aus.

## 10.4 Verhalten

- Der Node ist stateless.
- Er analysiert maximal eine aktive Instanz.
- Liegt nach dem Tracker genau eine Instanz vor, wird genau diese verwendet.
- Vor Einführung des Trackers kann `largest_matching_segment` als Entwicklungsstrategie verwendet werden.
- Bei unklarer Auswahl wird strukturiert abgebrochen.

---

# 11. Stateful Rotations- und Temporal-Nodes

## 11.1 Node `analysis.stem_surface_rotation`

Dieser Node beziehungsweise fachlich getrennte Handler erhält:

- entzerrten Frame,
- aktive Stammmaske/Track,
- `StemGeometryPayload`,
- Frameindex und Zeitstempel.

Er gibt `StemRotationPayload` aus mit mindestens:

```text
schema_version
track_id
frame_sequence
timestamp
delta_rotation_deg
cumulative_rotation_deg_unwrapped
rotation_direction
rotation_confidence
matched_feature_count
inlier_ratio
translation_component
trace_discontinuity
calibration_id
rotation_viewpoint_convention
reason_codes
```

Die Auswertung verwendet mehrere Rinden-/Astmerkmale oder dichten optischen Fluss im robusten Maskeninneren. Translation, Maßstabsänderung und Änderung der projizierten Stammachse werden soweit möglich kompensiert. Bei unzureichender Datenqualität bleibt der Winkel ungültig.

## 11.2 Rotationskalibrierung

Die Abbildung auf Grad benötigt eine versionierte Kalibrierung. Unterstützt wird mindestens ein Offline-Verfahren mit temporären Umfangsmarkierungen oder bekannten Winkelstellungen. Gespeichert werden:

- Methode,
- Kamera-/Intrinsikbezug,
- betrachtetes Stammende und positive Drehrichtung,
- gültiger Durchmesser-/Bildlagenbereich,
- Fehlerstatistik,
- Hash und Version.

## 11.3 Node `analysis.stem_temporal_orientation`

Dieser Node kombiniert:

- Session-Lifecycle,
- Geometriezeitreihe,
- Rotationsspur,
- Zielorientierungs-Kandidaten,
- Decision-Line-Deadline,
- deterministische und später gelernte Winkelabschätzung,
- Vor-Einzug-Result,
- optionale Nachkontrolle.

Ein paralleler Session-Service wird zunächst nicht gebaut.

## 11.4 Eingänge und Ausgänge

Eingänge:

- required: `stem_geometry`,
- required für Winkel: `stem_rotation`,
- optional: Maschinenereignisse, wenn später verfügbar.

Ausgänge:

- `result` als `ResultPayload`,
- `overlay` als `OverlayPayload`.

## 11.5 State-Schema

Der State unter `context.state[node.node_key]` enthält mindestens:

```text
schema_version
session_id
track_id
session_state
started_at
last_observation_at
last_frame_sequence
bounded_observation_buffer
rotation_trace_summary
target_candidates
best_target_candidate
decision_frame
frozen_pre_entry_estimate
post_rotation_verification
last_recommendation
lost_counter
reason_codes
```

Keine Base64-Bilder oder unbegrenzten Zeitreihen im State.

## 11.6 Zustandsautomat

```text
WAITING_FOR_LOG
CANDIDATE_VISIBLE
ACTIVE_LOG_SELECTED
THROW_IN_PROGRESS
APPROACHING_DECISION_LINE
PRE_ENTRY_DECISION_READY
WAITING_FOR_FIRST_ROLLER
MAIN_ROTATION_IN_PROGRESS          # später, optional
POST_ROTATION_VERIFICATION         # später, optional
FINISHED
ABORTED
```

## 11.7 Neustartverhalten

- State wird bei Runtime-/Pipeline-Neustart verworfen.
- Eine nicht abgeschlossene Session wird `aborted_runtime_restart`.
- Eingefrorene alte Winkelaufträge werden nach Neustart nicht erneut ausgegeben.
- Ein späterer SPS-Pfad benötigt neue Sessionsequenz und Watchdog.

# 12. Deterministische temporale Analyse

## 12.1 Zweck

Die deterministische Baseline dient als transparente erste Vor-Einzug-Schätzung, Golden-Data-Werkzeug und Vergleich für TCN/GRU. Sie ist nicht automatisch produktionsfreigegeben, weil die schräge Kamera nur eine Projektion sieht.

## 12.2 Eingangsfolge

- vorzeichenbehaftete und betragsmäßige Krümmungskennwerte,
- Residual- und Breitenprofile,
- Sichtbarkeit und Qualitätswerte,
- Track-/Positionsinformationen,
- frameweise und kumulative relative Rotation,
- Rotationstrace-Confidence und Diskontinuitäten,
- Zeitabstand und Decision-Zonenstatus.

## 12.3 Mindestfunktionen

- Zeitreihen robust glätten,
- reine Translation von Rotation unterscheiden,
- Krümmung über kumulativer Rotation auftragen,
- Verbesserung/Verschlechterung und Vorzeichenwechsel erkennen,
- Projektionsminimum beziehungsweise Zielkandidat eingrenzen,
- unzureichende Rotation erkennen,
- S-Bögen und widersprüchliche Verläufe markieren,
- Decision Frame bestimmen,
- signierte Korrektur modulo 180° berechnen,
- bei ungültiger Spur keine Winkelzahl ausgeben.

## 12.4 Ergebniszustände

```text
insufficient_observation
bow_not_relevant
target_candidate_observed
projected_minimum_bracketed
pre_entry_angle_ready
no_rotation
manual_required
uncertain
```

Die Winkelberechnung lautet:

```text
correction_deg = wrap_to_half_turn(target_rotation_deg - decision_rotation_deg)
```

mit `[-90°, +90°)`.

## 12.5 Vertrauenswert

Er berücksichtigt:

- Datenmenge und Sichtbarkeit,
- Geometriequalität,
- Rotationskalibrierung,
- zusammenhängende Rotationstrace,
- Merkmals-/Inlierqualität,
- Stärke und Eindeutigkeit des Bogens,
- Eindeutigkeit des Zielkandidaten,
- Abstand zur Decision Line,
- Konsistenz deterministischer Teilkennwerte.

## 12.6 Schräge Ansicht

Ein Projektionsminimum ist nicht automatisch die physikalisch perfekte Vertikalstellung. Daher trägt die Baseline `estimate_method=projected_baseline`. Das später gelabelte Zielintervall und das validierte Temporalmodell sollen lernen, welche Sequenzmuster in der festen schrägen Ansicht tatsächlich einer guten Vertikalstellung entsprechen.

# 13. Resultvertrag `stem.orientation.v2`

Der Temporal-Node gibt mindestens aus:

```json
{
  "schema_version": "stem.orientation.v2",
  "session_id": "...",
  "track_id": "...",
  "session_state": "PRE_ENTRY_DECISION_READY",
  "operating_mode": "shadow",
  "decision": {
    "ready": true,
    "frame_sequence": 152,
    "timestamp": "...",
    "valid_until": "..."
  },
  "recommendation": {
    "rotate_required": true,
    "signed_rotation_deg": -43.2,
    "absolute_rotation_deg": 43.2,
    "direction": "counter_clockwise",
    "rotation_viewpoint_convention": "viewed_from_configured_log_end",
    "deadband_deg": 5.0
  },
  "target_estimate": {
    "observed": true,
    "frame_start": 103,
    "frame_end": 108,
    "estimate_method": "projected_baseline"
  },
  "current_observation": {
    "signed_bow_normalized": 0.0,
    "rms_residual_normalized": 0.0,
    "cumulative_rotation_deg": 0.0,
    "rotation_confidence": 0.0
  },
  "bow_relevant": true,
  "angle_confidence": 0.0,
  "reason_codes": [],
  "post_rotation_verification": null,
  "versions": {
    "pipeline_version_id": "...",
    "calibration_hash": "...",
    "rotation_calibration_id": "...",
    "segmentation_model_version_id": "...",
    "feature_schema_version": "stem.geometry.v1",
    "rotation_schema_version": "stem.rotation.v1",
    "analyzer_version": "...",
    "temporal_model_version_id": null
  }
}
```

Regeln:

- `decision.ready=true` muss vor dem ersten Walzenpaar erreicht sein.
- Ein ungültiger Winkel wird als `null` mit Reason Code ausgegeben, nicht als 0°.
- `0°/no_rotation` ist nur bei gültigem Ergebnis und Deadband zulässig.
- Ein späteres Nachkontrollergebnis steht getrennt unter `post_rotation_verification`.
- Leser von `stem.orientation.v1` werden entweder rückwärtskompatibel unterstützt oder über eine dokumentierte Migration behandelt.

# 14. Shadow Mode, Result-History und verbindliches Labeling

## 14.1 Liveanzeige

Die Liveansicht zeigt:

- Track-/Sessionstatus,
- Entscheidungslinie und verbleibende Beobachtungsstrecke,
- Stammmaske und Mittellinie,
- projizierte Krümmung,
- kumulative visuelle Rotation und Confidence,
- Zielkandidat,
- eingefrorenen Vor-Einzug-Winkel und Richtung,
- Blick-/Vorzeichenkonvention,
- Modell-/Baseline-Methode,
- Reason Codes.

Beispiel:

```text
Drehe 43° gegen den Uhrzeigersinn
Entscheidung bereit – Konfidenz 84 %
```

## 14.2 Persistierung

Persistiert werden:

- ursprüngliches Vor-Einzug-Result,
- Decision Frame und Zielkandidat,
- kompakte Geometrie- und Rotationsspur,
- Konfidenz-/Reason-Code-Verlauf,
- Referenz auf Video-, Diagnose-, Feature- und Rotationstrace-Artefakte,
- optionales getrenntes Nachkontrollergebnis,
- vollständige Versionsprovenienz.

Nicht persistiert werden vollständige Frames/Videos in Datenbankfeldern oder unbegrenzte Zeitreihen im Result-JSON.

## 14.3 Integriertes Schnellreview

Das Schnellreview darf erfassen:

- Vorschlag plausibel: ja / nein / unklar,
- Richtung korrekt / falsch / unklar,
- Winkel grob zu klein / passend / zu groß / nicht beurteilbar,
- `bow_relevance`,
- finale Lage `good` / `acceptable` / `bad`,
- optionaler Zielzeitpunkt/-bereich,
- Sonderfall und Kommentar,
- Aktion „Zum Labeln senden“.

Es dient Diagnose und Vorannotation, ist aber nicht die immutable Ground Truth.

## 14.4 Verbindliches Ground-Truth-Labeling

```text
Vor-Einzug-Session
    ↓
MP4 + Features + visuelle Rotationsspur + Decision Frame
    ↓
automatischer Import in Label Studio
    ↓
Labeler markiert beste Zielorientierung auf der Timeline
    ↓
Labeler bewertet Relevanz, Sichtbarkeit und finale Lage
    ↓
Snapshot-Target-Builder berechnet signierten Winkel zum Decision Frame
    ↓
immutable Temporal-Snapshot
```

Der Labeler muss keine Gradzahl schätzen. Es wird keine zweite Labeling-Anwendung aufgebaut.

# 15. Test- und Entwicklungsbetrieb mit vorhandenen Quellen

## 15.1 Einzelbilder

- Channel mit Testbild beziehungsweise eingebettetem Testfoto,
- Pipeline-Preview bis zum Geometrie-Node,
- Overlayvergleich von Rohbild, entzerrtem Bild, Maske und Mittellinie.

## 15.2 Sequenzen

- `source.file_test` mit Videodatei,
- Echtzeit- oder beschleunigte Wiedergabe nach Source-Konfiguration,
- dieselbe stateful Pipeline wie im späteren Livekanal,
- automatische Erzeugung eines Temporal-Sample-Artefakts möglich,
- keine separate Sequenzanwendung.

## 15.3 F1–F4-Beispiel

Die vorhandenen F1–F4-Bilder und Masken dienen als Golden Data für:

- Maskenbereinigung und Geometrie,
- Feature-/Rindenverfolgung,
- Trennung von Translation und Rotation,
- Trend- und Vorzeichenwechselanalyse,
- Zielintervall zwischen beobachteten Frames,
- Winkelberechnung, **wenn** die relative Rotation zwischen den Testframes bekannt beziehungsweise kalibriert ist,
- korrekte Unsicherheitsausgabe, wenn diese Referenz fehlt.

Ohne bekannte Testwinkel bleiben sie ein Richtungs-/Methodentest, keine metrische Gold-Ground-Truth.

---

# 16. Allgemeiner Temporal-Dataset- und Label-Studio-Weg

## 16.1 Grundsatz

Die Plattform wird um einen allgemeinen Task-Typ für zeitliche Sequenzen erweitert, bevorzugt `temporal_sequence`. Es wird kein `TaskType.STEM_ORIENTATION` eingeführt.

Die allgemeine Infrastruktur unterstützt:

- Sequenz-Choices,
- Timeline-Punkte/-Intervalle,
- Feature- und Spurreferenzen,
- schemaabhängig abgeleitete Klassifikations- oder Regressionsziele,
- immutable Snapshots und leakage-freie Splits.

## 16.2 Automatische Sampleaufzeichnung

Ohne SPS-/Bedienersignale werden Grenzen visuell definiert:

- Start beim Eintritt des aktiven Tracks in `observation_entry_roi`,
- Decision Frame als letzter gültiger Frame vor `decision_line`,
- Ende des primären Clips am Decision Frame,
- optionale spätere Verifikationsaufnahme als separates Asset.

Pro Sample:

- `sequence.mp4`,
- optional `diagnostic.mp4`,
- `features.jsonl`,
- `rotation-trace.jsonl`,
- `sequence-manifest.json` mit Checksummen, Decision Frame, Zonen-, Blick- und Versionsprovenienz.

## 16.3 Automatische Label-Studio-Provisionierung

Die Anwendung kann aus Dataset und registriertem Schema idempotent ein Label-Studio-Projekt anlegen, konfigurieren und intern zuordnen. Projekt-ID und XML werden im Standardweg nicht manuell eingegeben. Das manuelle Mapping vorhandener Altprojekte bleibt erhalten.

## 16.4 Erstes Annotationsschema

`stem_orientation_angle.v1`:

```text
sequence_valid:
- usable
- unusable

bow_relevance:
- relevant
- nearly_straight
- unclear

target_visible:
- yes
- no
- unclear

target_orientation:
- Timeline-Intervall der besten beobachteten vertikalen Bogenlage

final_orientation_quality:
- good
- acceptable
- bad

special_case, optional:
- occluded
- motion_blur
- tracking_failed
- rotation_trace_invalid
- insufficient_rotation
- multiple_stems
- incomplete_sequence
- ambiguous_target
- s_bow
```

Der Labeler markiert einen kurzen Zielbereich, nicht eine Gradzahl.

## 16.5 Automatischer Taskimport

Ein fertiges Sample wird idempotent als Videoaufgabe importiert. Taskmetadaten enthalten Decision Frame, FPS, Schema-, Kalibrierungs-, Rotationstrace- und Blickkonvention. Eine deterministische Zielbereichsschätzung darf als korrigierbare `prediction` mitgesendet werden.

## 16.6 Kanonischer Temporal-Snapshot und Target Builder

Ein Sequenzitem enthält:

- Video-, Feature- und Rotationstrace-Referenzen,
- geordnete Frames/Zeitstempel,
- Decision Frame,
- Human Labels,
- getrennte Derived Targets,
- Provenance und Labelqualität.

Der registrierte Target Builder berechnet für gültige Stammsequenzen:

```text
phi_target = robustes Zentrum der Rotation im Zielintervall
phi_decision = Rotation am Decision Frame
e = wrap_to_half_turn(phi_target - phi_decision)
```

Gespeichert werden mindestens:

- `signed_correction_deg`,
- Richtung,
- Zielwinkelintervall/Toleranz,
- `sin(2e)` und `cos(2e)`,
- Winkelkonfidenz,
- Labelquelle und Builder-Version.

## 16.7 Label- und Ausschlussregeln

- `unusable`: vollständig ausschließen.
- `unclear`: standardmäßig ausschließen.
- `relevant + target_visible=yes`: gültiges Zielintervall erforderlich.
- Zielintervall muss bis einschließlich Decision Frame liegen.
- Ungültige/discontinuous Rotationstrace: kein Winkelziel.
- `nearly_straight`: für Relevanz/No-Rotation nutzbar, kein zufälliger Winkel.
- `final_orientation_quality` bleibt getrennte Nebenannotation.

## 16.8 Split- und Leakage-Regeln

- Split nach Session/`group_id`,
- alle Varianten desselben Stamms im selben Split,
- keine Frames nach Decision Frame im Hauptmodell,
- Scaler nur aus Train,
- doppelte Hashes und Zeitüberlappungen prüfen.

## 16.9 Labelqualität

### Gold

Zielintervall plus unabhängige bekannte Winkelreferenz, beispielsweise temporäre Umfangsmarkierung oder verifizierte Rotationsmessung.

### Silver

Zielintervall durch Labeler, Winkel aus versionierter visueller Rotationstrace mit ausreichender Confidence.

### Weak

Automatischer Zielkandidat oder Schnellreview ohne bestätigte Label-Studio-Annotation.

# 17. Architektur-Gate für das zeitliche ML-Modell

## 17.1 Kein ML als Voraussetzung der Datensammlung

Recorder, Label-Studio-Provisionierung, Timeline-Labeling, Rotationstrace und Target Builder funktionieren ohne trainiertes Temporalmodell.

## 17.2 Verbindliche ADR

Vor TCN-/GRU-Code wird eine ADR erstellt. Sie vergleicht:

A. allgemeiner `temporal_sequence`-Pfad durch Trainer, Modellkatalog, Deployment und Runtime,

B. spezialisiertes direktes Laden im Stamm-Node,

C. Missbrauch als bestehende Bild-Classification.

C ist unzulässig. A ist bevorzugt.

Die ADR bewertet:

- Rückwärtskompatibilität,
- gemischte Klassifikations-/Winkelziele,
- Modellregister und Promotion,
- ONNX-/TensorRT-/Triton-Pfad,
- kausalen Streamingzustand und Decision Deadline,
- Latenz,
- Wiederverwendbarkeit,
- Gold-/Silver-Evaluation,
- Risiko einer Parallelinfrastruktur.

## 17.3 Datenlage der ersten Winkelmodell-Version

Nicht erforderlich:

- SPS- oder Bedienertastensignale,
- gelabelte Bedieneraktionen,
- Walzenencoder als Ground Truth.

Vorhanden:

- kausale Bild-/Geometrie-/Trackingsequenz bis zum Decision Frame,
- visuelle Rotationsspur,
- menschlich markiertes Zielintervall,
- daraus abgeleitetes signiertes Winkelziel mit Confidence,
- Bow-Relevance,
- Ziel-sichtbar-Label,
- optionale finale Qualitätsklasse.

Silver-Winkelziele sind nicht unabhängig von der visuellen Rotationstrace. Vor produktiver Automatik ist deshalb ein Gold-Testbestand mit bekannter Winkelreferenz verpflichtend.

## 17.4 Warum TCN/GRU als erste Baseline

Das Modell erhält strukturierte kausale Featurefolgen und keine Rohvideos. Das Problem ist primär eine multivariate Zeitreihe mit Winkel-/Klassifikationsköpfen.

### TCN

- kausale 1D-Faltungen,
- klares Zeitfenster,
- paralleles Training,
- geringe Parameterzahl und Latenz,
- einfacher Export und Diagnose.

### GRU

- natürlicher Streaming-State,
- variable Sequenzlängen,
- kompakt,
- aber komplexerer Reset/Debugging und weniger paralleles Training.

## 17.5 Warum zunächst kein Video-Transformer

- Geometrie und Rotation sind explizit vorhanden.
- Der Goldbestand wird anfangs begrenzt sein.
- Die Ausgabe muss strikt kausal vor einer Deadline entstehen.
- Video-Transformer würden neue Clip-/Tensor-/Gateway-/Deploymentverträge benötigen.
- Ein kleiner 2D-Frame-Encoder plus TCN/GRU wird vor einem Rohvideo-Transformer geprüft.

# 18. Anforderungen an das erste TCN-/GRU-Modell

## 18.1 Eingang pro Zeitschritt

- Residual-/Mittellinienprofil,
- Breitenprofil,
- projizierte Krümmungskennwerte,
- Gültigkeitsmasken,
- Sichtbarkeit und Qualitätswerte,
- Track-/Positionsmerkmale,
- frameweise und kumulative Rotation,
- Rotationstrace-Confidence und Diskontinuitätsmaske,
- Bewegungs-/Rindenfeaturestatistik,
- optional visueller Embedding-Vektor.

Keine SPS-/Taster-/Aktionsfeatures als Pflichtinput.

## 18.2 Trainingsfenster

- Primäres Sample endet am Decision Frame.
- Frames danach sind verboten.
- Präfix-Augmentation nur, wenn das Ziel bereits beobachtet wurde und der Winkel zum neuen Endframe neu berechnet wird.
- Ungültige Winkelziele maskieren nur den Winkelkopf.
- Alle Varianten derselben Session bleiben im selben Split.

## 18.3 Ausgänge

- `bow_relevance_probability`,
- `target_observed_probability`,
- `sin(2e)` und `cos(2e)`,
- rekonstruierter `signed_correction_deg` in `[-90°, +90°)`,
- Winkelkonfidenz/Unsicherheit,
- optional `good/acceptable/bad` als Nebenaufgabe,
- optional Out-of-Distribution-Indikator.

Die Hauptausgabe ist Richtung und Gradzahl vor dem Einzug; die Qualitätsklasse ersetzt sie nicht.

## 18.4 Evaluation

- Mean/Median/P90/P95 Winkelerror modulo 180°,
- Anteil innerhalb ±5°, ±10°, ±15°, ±20°,
- Richtungsgenauigkeit oberhalb Deadband,
- gefährliche Gegenrichtungsrate,
- schwere Fehler >30°,
- No-Rotation-Fehlerrate bei nahezu geraden Stämmen,
- Confidence-Coverage und Kalibrierung,
- Bow-Relevance/Target-Observed-Metriken,
- Auswertung nach Sichtbarkeit, Bogenstärke, Tracequalität, Stammgröße und Sequenzlänge,
- Gold und Silver getrennt,
- Vergleich TCN, GRU, deterministischer Baseline und Decision-Frame-Baseline.

Das Modell darf nur im Shadow Mode eingesetzt werden, wenn Feature-, Rotation-, Snapshot-, Kalibrierungs- und Blickkonvention kompatibel sind.

# 19. SPS- und Maschinenintegration

## 19.1 Stufe 1 – Datensammlung ohne Maschinensignale

Sessiongrenzen, Decision Frame und Rotationsspur werden visuell bestimmt. SPS-/Bedienersignale sind keine Voraussetzung für Recorder, Labeling oder das erste Winkelmodell.

## 19.2 Stufe 2 – Vor-Einzug-Shadow Mode

Die Pipeline zeigt und speichert vor dem ersten Walzenpaar:

- Drehen ja/nein,
- signierte Gradzahl,
- Richtung,
- Confidence,
- Gültigkeit und Reason Codes.

Keine produktiven SPS-Werte.

## 19.3 Stufe 3 – Dry-Run des Feed-forward-Auftrags

Der vorhandene Node `output.siemens_sps_write` bleibt zunächst:

- `enabled=false`,
- `dry_run=true`,
- `plc_writes_allowed=false`.

Simuliert wird der bereits vor dem Greifen eingefrorene Auftrag. Keine offene Suche durch viele Testimpulse.

## 19.4 Stufe 4 – assistierte Hauptdrehung

Vision überträgt beispielsweise:

```text
session_sequence
decision_ready
valid_until
rotate_required
signed_rotation_deg
absolute_rotation_deg
direction_code
rotation_viewpoint_code
angle_confidence
model/calibration versions
reason_code
heartbeat
```

Die SPS entscheidet und verriegelt:

- sicheren Walzenkontakt,
- zulässigen Maschinenzustand,
- Winkel-/Zeit-/Geschwindigkeitsgrenzen,
- Umsetzung auf den realen Drehantrieb,
- Watchdog und Bedienervorrang,
- sicheren Stopp.

## 19.5 Nachkontrolle

Bleibt die Sicht erhalten, kann Vision:

- tatsächlich beobachtete Ausführungsrotation,
- Restwinkel,
- kleine begrenzte Korrekturempfehlung

liefern. Diese Phase ist sekundär, begrenzt und kein Ersatz für die Hauptentscheidung.

## 19.6 Fail-safe-Regeln

Keine Drehung bei:

- ungültiger intrinsischer oder Rotationskalibrierung,
- Track-/Sessionverlust,
- zu geringer Sichtbarkeit,
- nicht beobachtetem Ziel und unzureichender Modellkonfidenz,
- abgelaufenem Auftrag,
- unklarer Blick-/Richtungskonvention,
- nearly-straight/Deadband,
- Winkel außerhalb SPS-Grenzen,
- Runtime-Neustart ohne neuen Handshake,
- SPS-Interlock nicht erfüllt.

Ein ungültiger Winkel ist `null/manual_required`, niemals stillschweigend 0°.

# 20. Frontend-Integration

## 20.1 Pipeline-Designer

- neue Nodes über vorhandenen Node-Katalog,
- Standardfelder über vorhandene Property-Renderer,
- strukturierte Properties für Intrinsik, Prozesszonen, Rotationskalibrierung und Blickkonvention,
- Preview zeigt Maske, Achse, Krümmung, Rotationsspur und Decision Line,
- Einzelbildpreview erklärt, dass ein Winkel eine Sequenz benötigt.

## 20.2 Channel-Detailseite

- vorhandene Kamera-/Stream-/Testdateifunktionen bleiben,
- Liveanzeige zeigt Countdown/Position bis Decision Line,
- Vor-Einzug-Winkel, Richtung und Confidence werden eingefroren dargestellt,
- optionale Nachkontrolle wird getrennt angezeigt.

## 20.3 Result-History, Sampleauswahl und Label Studio

Detailansicht für `stem.orientation.v2`:

- ursprüngliche Vor-Einzug-Empfehlung,
- Decision Frame,
- Zielkandidat,
- kompakte Krümmungs- und Rotationskurve,
- ausgewählte Diagnoseframes,
- Schnellreview,
- Aktion „Zum Labeln senden“,
- Import-/Label-Studio-Status,
- Versions- und Provenienzdaten.

Im automatisch angelegten Label-Studio-Projekt markiert der Labeler das Zielorientierungsintervall und bewertet Sequenz, Bogenrelevanz, Zielsichtbarkeit und finale Lage. Keine zweite Labeling-Web-App.

# 21. API- und Persistenzerweiterungen

## 21.1 Grundsatz

Nur fehlende Funktionen werden in vorhandenen Services ergänzt. Keine zweite Trainings-, Modell-, Dataset- oder SPS-API.

## 21.2 Erforderliche Erweiterungen

Voraussichtlich:

1. intrinsische Kamerakalibrierung,
2. Prozesszonen-/Decision-Line-Konfiguration,
3. Rotationskalibrierungs- und Testartefakte,
4. allgemeine Label-Studio-Projektprovisionierung,
5. Temporal-Sample-Recorder für Video, Features und Rotationstrace,
6. idempotenter Autoimport,
7. schemaabhängiger Timeline-Export und Target Builder,
8. Schnellreview/„Zum Labeln senden“,
9. später allgemeine Temporal-Sequence-Trainingsintegration.

## 21.3 Persistenz

- Intrinsik/Zonen: versionierte Pipelinekonfiguration,
- Rotationskalibrierung: versioniertes Artefakt mit Hash,
- Live-State: `RuntimeExecutionContext.state`,
- Hauptresult: vorhandene Result-History,
- optionales Nachkontrollresult getrennt,
- Sample-Ledger: Sequence-ID, Decision Frame, Asset-/Importstatus,
- Video/Features/Rotationstrace: verwalteter Artefaktspeicher,
- Human Labels: Label Studio,
- immutable Trainingsstand: Snapshot-/DVC-Pfad.

## 21.4 Reproduzierbarkeit

Jede Entscheidung referenziert:

- Channel/Pipeline,
- Intrinsik und Prozesszonen,
- Rotationskalibrierung und Blickkonvention,
- Segmentierungsmodell,
- Geometrie-/Rotation-/Feature-Schema,
- Analyzer/Temporalmodell,
- Decision Frame,
- Betriebsmodus,
- Maschinenereignisse beziehungsweise deren Nichtverfügbarkeit.

# 22. Leistung und Stabilität

## 22.1 Zielwerte für den ersten Liveprototyp

- 20 bis 30 ausgewertete Frames/s bei geeigneter Kamerakonfiguration,
- keine Frame-Reihenfolgefehler in stateful Pipelines,
- Geometriekern möglichst unter 10 ms pro gültiger Maske auf der Zielhardware,
- gesamte Pipeline einschließlich Segmentierung als Zielwert median unter 100 ms und P95 unter 150 ms,
- keine unbeschränkte Speicherzunahme über lange Laufzeiten,
- kein Persistieren großer Inline-Bilder in der Result-History.

Diese Werte sind Entwicklungsziele und werden nach Auswahl der Kamera, Auflösung und des Segmentierungsmodells gemessen und gegebenenfalls angepasst.

## 22.2 Serialisierung

Da die Pipeline stateful ist, wird `pipeline_max_inflight=1` wirksam. Dies ist für zeitliche Ordnung und SPS-Nebenwirkungen beabsichtigt. Die Segmentierungsinferenz muss deshalb innerhalb des verfügbaren Framebudgets liegen. Bei höherem Kamera-FPS wird über vorhandene Sampling-/Max-FPS-Konfiguration kontrolliert reduziert, nicht durch parallele, ungeordnete Sessionauswertung.

## 22.3 Qualitätsmonitoring

Livekennwerte:

- Kamera-/Source-Health,
- verarbeitete und verworfene Frames,
- Segmentierungslatenz,
- Geometrielatenz,
- Temporal-Node-Latenz,
- Trackverluste,
- unsichere Sessions,
- Runtime-State-Größe,
- letzte Kalibrierungsvalidität.

---

# 23. Deployment und Update

Die Umsetzung wird in den vorhandenen Windows-/WSL2-Pfad integriert:

- bestehende Python-Pakete erweitern,
- Frontend über bestehenden Build integrieren,
- OpenCV-Abhängigkeiten im vorbereiteten Runtime-Bundle sicherstellen,
- optionale neue Alembic-Migrationen über vorhandenen Updatepfad ausführen,
- Kalibrierung in Pipelineversionen erhalten,
- Review- und Ergebnisdaten nicht überschreiben,
- neue Funktionen standardmäßig deaktiviert lassen,
- Upgrade von einer Version ohne Stammausrichtung testen,
- Rollback dokumentieren und testen,
- keine neue EXE und keinen zweiten Updater einführen.

Die produktive Zielinstallation bleibt Docker-frei; die vorhandene WSL2-Runtime und der vorhandene Installer werden weiterverwendet.

---

# 24. Teststrategie

## 24.1 Unit-Tests Geometrie

- gerader Stamm,
- C-Bogen in beiden Vorzeichenrichtungen,
- S-Bogen,
- konischer Stamm,
- lokale Astwulst,
- abgeschnittenes Ende,
- fragmentierte/zu kleine Maske,
- Translation und Rotation derselben Maske,
- numerische Stabilität und Gültigkeitsmasken.

## 24.2 Unit-Tests Tracking, Rotation und Temporal-State

- stabiler Track und Kandidatenauswahl,
- Trackverlust, Wiederaufnahme und neuer Stamm,
- reine Translation ohne Rotation,
- bekannte positive und negative Winkelschritte,
- Rotationstrace-Unwrap und Diskontinuität,
- Kalibrierung gegen bekannte Winkelstellungen,
- Zielkandidat, Decision Frame und Modulo-180-Korrektur,
- unzureichende Rotation und fehlende Oberflächenmerkmale,
- Neustart, State-Reset und Pufferbegrenzung.

## 24.3 Kalibrierungstests

- synthetisch bekannte Distortion,
- ungültige Boarddetektion,
- zu wenige Beobachtungen,
- falsche Auflösung,
- Profilserialisierung und Hash,
- Roh-/Undistort-Kontext und Provenienz.

## 24.4 Runtime-Integration

- Registry und Handler-Loading,
- Payload-Serialisierung,
- Pipelinevalidierung,
- `source.file_test -> undistort -> segmentation -> tracker -> geometry -> surface_rotation -> temporal -> output.result`,
- stateful Reihenfolge über viele Frames,
- eingefrorenes Winkelresult vor der Decision Line,
- Overlay im Livepfad,
- getrennte Nachkontrolle,
- Result-History ohne Inline-Bilder,
- keine Zukunftsframes und keine alten Winkelaufträge nach Neustart.

## 24.5 Frontend

- neue Nodes im Designer,
- Standardproperties,
- Kalibrierungsassistent,
- Einzelbild-Preview,
- Live-/Reviewdarstellung,
- Fehler- und Unsicherheitszustände.

## 24.6 Regression

- bestehende Nicht-Stamm-Pipelines bleiben unverändert,
- bestehende Brettschnitt-Nodes bleiben funktionsfähig,
- bestehende Kamera-, Trainings-, Label- und Resultseiten bleiben funktionsfähig,
- `plc_writes_allowed=false` verhindert weiterhin produktive SPS-Schreibzugriffe.

## 24.7 Deploymenttests

- Windows-Installer einer Version mit Stammausrichtung,
- Update von einer älteren Version,
- Migration mit vorhandener Datenbank,
- Rollback,
- Smoke-Test einer bestehenden Pipeline und einer Stamm-Testpipeline.

---

# 25. Akzeptanzkriterien nach Entwicklungsphase

## Phase A – Integrationskarte

- reale Dateipfade/Symbole bestätigt,
- Node-, State-, Result-, Dataset- und Trainergrenzen dokumentiert,
- keine Implementierung.

## Phase B – Geometriekern

- synthetische Maskentests bestanden,
- Translation/Rotation der Maske verändert normiertes Profil nur innerhalb Toleranz,
- lokale Äste zerstören den niederfrequenten Bogenwert nicht.

## Phase C – Runtime-Geometrie

- Node im Katalog/Designer,
- Preview und Overlay funktionieren,
- F1–F4-Geometrie nachvollziehbar.

## Phase D – Intrinsik und Prozesszonen

- valide Kalibrierung,
- Roh-/Undistort-Vorschau,
- Beobachtungs-, Decision- und Walzenzonen versioniert,
- falsche Auflösung verhindert.

## Phase E – Active-Stem-Tracking

- aktiver Stamm bleibt über Einwurf und Anfahrt stabil,
- Ambiguität führt fail-closed,
- stateful Pipeline bleibt geordnet.

## Phase F – Rotationsspur und deterministische Vor-Einzug-Baseline

- bekannte Rotationen innerhalb definierter Toleranz,
- Translation erzeugt keinen Winkel,
- Decision Frame wird vor dem Walzenbereich erkannt,
- signierte Korrektur modulo 180° möglich,
- ungültige Spur erzeugt keine Gradzahl.

## Phase G – Shadow Mode

- Vor-Einzug-Result mit Winkel/Richtung gespeichert,
- Zielkandidat, Decision Frame und Provenance sichtbar,
- Schnellreview möglich,
- keine SPS-Schreibzugriffe.

## Phase H – Allgemeiner Temporal-Sequence-/Label-Studio-Weg

- allgemeiner Sequenz-Task und Schema-Registry,
- Projekt automatisch provisionierbar,
- MP4, Features und Rotationstrace automatisch erzeugt,
- Timeline-Zielbereich labelbar,
- idempotenter Import,
- immutable Snapshot,
- Derived Angle Target mit Confidence.

## Phase I – Temporal-Angle-ADR und TCN/GRU

- ADR vor Modellcode,
- TCN, GRU und Baselines auf identischen Splits,
- kausale Vor-Einzug-Winkelvorhersage,
- Gold/Silver getrennt,
- Winkel-/Richtungskonvention versioniert,
- Rückfall auf `manual_required` bei Inkompatibilität.

## Phase J – Feed-forward-SPS-Anbindung

- Hauptauftrag liegt vor Walzenkontakt vor,
- Dry-Run und Interlocks geprüft,
- SPS führt erst nach sicherem Walzenkontakt aus,
- keine offene Testimpuls-Suche als Hauptverfahren,
- Nachkorrektur begrenzt,
- Watchdog/Bedienerübernahme/Audit geprüft.

# 26. Implementierungsreihenfolge

Für einen neuen Lauf gelten die Codex-Prompts v4:

1. Prompt 00 – Integrationskarte,
2. Prompt 01 – Geometriekern,
3. Prompt 02 – Runtime-Geometrie,
4. Prompt 03 – Intrinsik/Undistort,
5. Prompt 04 – Active-Stem-Tracking,
6. Prompt 05 – Rotationsspur und deterministische Vor-Einzug-Winkelbaseline,
7. Prompt 06 – Shadow-Result und Review,
8. Prompt 07A – allgemeiner Temporal-Sequence-Typ und Schema-Registry,
9. Prompt 07B – Label-Studio-Projektprovisionierung,
10. Prompt 07C – Vor-Einzug-Clip/Features/Rotationstrace,
11. Prompt 07D – automatischer Label-Studio-Import,
12. Prompt 07E – Snapshot und abgeleitetes Winkelziel,
13. Prompt 08 – Temporal-Angle-ADR,
14. Prompt 09 – TCN/GRU für signierten Korrekturwinkel,
15. Prompt 10 – Winkelmodell im Vor-Einzug-Shadowpfad,
16. Prompt 11 – Feed-forward-Hauptdrehung und begrenzte Nachkontrolle,
17. Prompt 12 – Installer/Update/Rollback.

Wenn Prompts 00 bis 06 der v3-Fassung bereits umgesetzt wurden, wird zuerst **Prompt 06A** ausgeführt. Danach beginnt die Folge bei 07A.

Die Labeldatensammlung ist nach 07D nutzbar. Ein Winkel-Target entsteht nach 07E. Ein Modell ist für Recorder und Labeling nicht erforderlich.

# 27. Hauptrisiken und Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
| --- | --- |
| Schräge Kamera vermischt horizontale und vertikale Projektion | Zielintervall durch Fachlabeler, Temporalmodell, Gold-Testbestand; keine naive Einzelbildentscheidung |
| Visuelle Rotation ist nicht metrisch kalibriert | bekannte Testwinkel/temporäre Umfangsmarkierungen, versionierte Kalibrierung, Confidence und Fail-closed |
| Rutschen wird als Rollen interpretiert | Translation herausrechnen, Maskenkoordinaten, mehrere Oberflächenmerkmale, synthetische/echte Tests |
| Zielstellung wurde im Clip nicht beobachtet | `target_visible=no`, kein Winkelziel, Runtime `manual_required` |
| Mehrere Stämme dicht zusammen | Instanzsegmentierung, ROIs, stateful Tracker, Ambiguität fail-closed |
| Bewegungsunschärfe | Global Shutter, kurze Belichtung, diffuse Beleuchtung, Quality Gate |
| Perspektivische/Tonnenverzeichnung | Intrinsik und Undistort; keine künstliche Draufsicht |
| Sichtbarer Längsabschnitt ändert sich | normierte Stammkoordinate, Gültigkeitsmasken, gemeinsame Bereiche |
| Äste/Rindenfetzen verfälschen Geometrie | robuste Konturen und niederfrequente Form; für Rotation mehrere Merkmale statt Einzelpunkt |
| Silver-Winkelziel verwendet dieselbe visuelle Spur wie Runtime | unabhängiger Gold-Testbestand mit bekannten Winkeln; getrennte Berichte |
| Entscheidung kommt zu spät | Decision Line, harte Deadline, kausale Inferenz, Latenzmonitoring |
| Stateful Pipeline zu langsam | kontrolliertes Sampling/FPS, kleine Modelle, serialisierte State-Nodes, Metriken |
| Zu wenig gelabelte Sequenzen | automatische Aufzeichnung/Import, Sampling, Active-Learning-Auswahl |
| Temporalmodell wird in Bild-Classification gezwungen | allgemeiner `temporal_sequence`-Pfad und verpflichtende ADR |
| Alte Aufträge nach Neustart | State-Reset, Sessionsequenz, valid_until, Handshake, Watchdog |
| SPS dreht in falscher physischer Richtung | versionierte Blick-/Vorzeichenkonvention und explizites Mapping im Dry-Run |
| Update beschädigt Bestand | kleine Schritte, Regressionstests, vorhandener Installer/Updater, Rollbacktest |

# Anhang A – Konkrete Repository-Anknüpfungspunkte

| Funktion | Primäre Dateien |
| --- | --- |
| Runtime-Handlervertrag | `backend/packages/cv-runtime/src/cv_runtime/handlers.py` |
| Runtime-Context und State | `backend/packages/cv-runtime/src/cv_runtime/context.py` |
| Runtime-Payloads | `backend/packages/cv-runtime/src/cv_runtime/payloads.py` |
| Node-Registry | `backend/packages/cv-runtime/src/cv_runtime/registry.py` |
| Pipelineausführung | `backend/packages/cv-runtime/src/cv_runtime/engine.py` |
| Beispiel komplexer Fach-Nodes | `backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py` |
| Stateful Realtime-Ausführung | `backend/services/realtime-executor/src/realtime_executor/live.py` |
| Kamera-/File-Test-Quellen | `backend/services/realtime-executor/src/realtime_executor/sources.py` |
| Runtime-Admin, Preview, Calibration Frame | `backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py` |
| Pipelinepersistenz und Katalog | `backend/packages/cv-db/src/cv_db/pipeline_store.py` |
| Result-History | `backend/packages/cv-db/src/cv_db/runtime_execution_store.py`, `backend/packages/cv-db/src/cv_db/models.py` |
| Runtime-Verträge | `backend/packages/cv-contracts/src/cv_contracts/runtime.py` |
| Task-Typen | `backend/packages/cv-status/src/cv_status/enums.py` |
| Trainerverträge und Katalog | `backend/packages/cv-training/src/cv_training/trainer.py`, `trainer_types.py`, `catalog.py` |
| Modellkatalog | `backend/packages/cv-db/src/cv_db/model_catalog.py` |
| Pipeline-Designer | `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx` |
| Channel-Testdatei und Kamera-UI | `frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx` |
| Result-History-UI | `frontend/apps/admin-web/src/features/results/result-history-page.tsx` |
| SPS-Output | `backend/packages/cv-runtime/src/cv_runtime/siemens_sps.py`, `docs/runtime/siemens-sps-write-node.md` |
| Windows-/WSL2-Deployment | `deployment/README.md`, `deployment/export-windows-update.ps1`, `deployment/update-windows-offline.ps1`, `deployment/rollback.ps1` |

# Anhang B – Neue beziehungsweise zu erweiternde Runtime-Bestandteile

## Neue Dateien, vorbehaltlich Implementation Map

```text
backend/packages/cv-runtime/src/cv_runtime/stem_geometry.py
backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py
backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py
```

## Zu erweiternde Dateien

```text
backend/packages/cv-runtime/src/cv_runtime/payloads.py
backend/packages/cv-runtime/src/cv_runtime/registry.py
backend/services/realtime-executor/src/realtime_executor/live.py
backend/packages/cv-contracts/src/cv_contracts/runtime.py
frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx
frontend/apps/admin-web/src/features/results/result-history-page.tsx
```

Kalibrierungs- und Review-API-Dateien beziehungsweise Migrationen werden erst nach Prompt 00 verbindlich benannt.
