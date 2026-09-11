# Codex-Prompts v4 – Vor-Einzug-Winkel, Label Studio und temporale Sequenzmodelle im bestehenden Inferenz Server

## Dokumentzweck

Dieses Dokument ersetzt die v3-Promptfolge ab Prompt 05. Das primäre fachliche Ziel ist eine konkrete, signierte Drehkorrektur, die bereits vor dem ersten Vorzentrierungs-Walzenpaar aus der Einwurfsequenz berechnet wird. Die Kontrolle während beziehungsweise nach der Hauptdrehung bleibt eine sekundäre Feinkorrektur. Es entsteht keine neue Anwendung; alle Arbeiten erfolgen innerhalb des bestehenden Repositories mit seinen vorhandenen Paketen, Services, Verträgen, Runtime-Pipelines, Trainingsdiensten, Frontend-Komponenten und Deployment-Skripten.

Die fachliche Gesamtspezifikation bleibt in `Spezifikation.md`. Allgemeine Regeln werden einmalig in `AGENTS.md` hinterlegt und nicht in jedem Prompt wiederholt.

---

# 1. Einmalige Repository-Regeln für `AGENTS.md`

Diesen Block einmal in die Root-Datei `AGENTS.md` übernehmen oder in eine bereits vorhandene Datei integrieren:

```markdown
# Stammausrichtung – dauerhafte Arbeitsregeln

## Bestehendes System

Dieses Repository ist der bestehende „Inferenz Server“. Es darf keine zweite FastAPI-, Vite-, Datenbank-, Kamera-, Trainings-, Modellverwaltungs- oder Runtime-Anwendung daneben entstehen.

Wiederzuverwenden sind insbesondere:

- Runtime-Payloads, Handler, Node-Registry und Pipeline-Engine in `backend/packages/cv-runtime`
- gemeinsame Verträge in `backend/packages/cv-contracts`
- Status- und Task-Enums in `backend/packages/cv-status`
- Datenbank und Alembic-Migrationen in `backend/packages/cv-db` und `backend/migrations`
- Runtime-Ausführung in `backend/services/realtime-executor`
- Runtime-Konfiguration und Preview in `backend/services/runtime-admin-api`
- Training in `backend/packages/cv-training`, `backend/services/training-orchestrator` und `backend/workers/training-worker`
- Modellkatalog und Deploymentpfad in `backend/packages/cv-db`, `backend/packages/cv-deploy` und `backend/services/inference-gateway`
- bestehende Admin-Web-App in `frontend/apps/admin-web`
- bestehende OpenAPI-Verträge und generierte Frontend-Schemas
- bestehende Windows-/WSL2-Installer- und Updatepfade in `deployment`

## Zielsystem und Deployment

- Produktionsziel: Windows 11 Pro mit dem bereits vorhandenen Offline-Installer und der vorhandenen WSL2-Runtime
- Auf dem Zielsystem soll keine neue separate Docker-Anwendung entstehen.
- Bestehende Entwicklungs-, Builder- oder Exportmechanismen dürfen nicht entfernt werden, nur weil das Produktionsziel dockerfrei bedient wird.
- Persistente Daten und Kalibrierungen müssen Updates und Rollbacks überstehen.

## Arbeitsregeln

- Vor Änderungen immer die tatsächlich beteiligten Dateien und Symbole lesen.
- Keine parallelen APIs erfinden, wenn bereits Router, Services oder Verträge existieren.
- Keine neue SQLite-Datenbank einführen; die Plattform verwendet den bestehenden `cv-db`-/PostgreSQL-/Alembic-Pfad.
- Neue Runtime-Nodes folgen `RuntimeNodeHandler`, `RuntimeExecutionContext`, `NodeExecutionResult`, `RuntimePayload` und der Registry-Konvention.
- Fachlich komplexe Nodes dürfen wie `cv_runtime.nodes.board_cut` in einem eigenen Node-Modul liegen.
- Zustandsbehaftete Echtzeit-Nodes verwenden den vorhandenen `RuntimeExecutionContext.state`-Mechanismus und müssen im Realtime-Executor als stateful gekennzeichnet werden.
- Bestehende Pipelines und Verträge bleiben rückwärtskompatibel.
- SPS-Schreibzugriffe bleiben bis zu einer ausdrücklich freigegebenen Phase deaktiviert. Vision liefert zunächst Messwerte, Empfehlungen und Shadow-Ergebnisse.
- Primäres Ziel ist eine signierte Winkelkorrektur, die vor dem ersten Vorzentrierungs-Walzenpaar bereitsteht; eine Testimpuls-Suche während der Drehung ist nur sekundäre Nachkorrektur.
- Keine erfundenen Gradwerte: Winkel benötigen eine versionierte visuelle Rotationsspur, eine eindeutige Blick-/Richtungskonvention und ausreichende Konfidenz.
- Das verbindliche Sequenzlabeling erfolgt in Label Studio über einen markierten Zielorientierungsbereich; der Gradwert wird daraus und aus der Rotationsspur abgeleitet.
- Tests, Build und Schema-Generierung nach den vorhandenen Repository-Konventionen ausführen.
- Nach jedem Auftrag nur die dafür notwendigen Dateien ändern und die Aufgabe beenden.
```

---

# 2. Prompt 00 – Verbindliche Integrationskarte, keine Implementierung

```text
Arbeite im bestehenden Repository des Inferenz Servers.
Lies `AGENTS.md`, `Spezifikation.md` und mindestens folgende Dateien vollständig beziehungsweise in den relevanten Abschnitten:

- backend/packages/cv-runtime/src/cv_runtime/handlers.py
- backend/packages/cv-runtime/src/cv_runtime/context.py
- backend/packages/cv-runtime/src/cv_runtime/payloads.py
- backend/packages/cv-runtime/src/cv_runtime/registry.py
- backend/packages/cv-runtime/src/cv_runtime/engine.py
- backend/packages/cv-runtime/src/cv_runtime/nodes/builtin.py
- backend/packages/cv-runtime/src/cv_runtime/nodes/board_cut.py
- backend/services/realtime-executor/src/realtime_executor/live.py
- backend/packages/cv-db/src/cv_db/pipeline_store.py
- backend/packages/cv-contracts/src/cv_contracts/runtime.py
- backend/packages/cv-status/src/cv_status/enums.py
- frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx
- backend/packages/cv-training/src/cv_training/trainer.py
- backend/packages/cv-training/src/cv_training/trainer_types.py
- backend/packages/cv-training/src/cv_training/catalog.py
- backend/packages/cv-db/src/cv_db/model_catalog.py
- deployment/README.md

Ändere keinen Produktivcode.

Erstelle `docs/stem-orientation/implementation-map.md` mit:

1. den exakten bestehenden Erweiterungspunkten für neue Payloads, Handler und Node-Typen,
2. dem Lebenszyklus von `RuntimeExecutionContext.state` im Realtime-Betrieb,
3. der Stelle, an der neue stateful Node-Typen eingetragen werden müssen,
4. dem Weg eines neuen Node-Typs bis in den Pipeline-Designer,
5. dem vorhandenen Preview-/File-Test-Pfad,
6. dem bestehenden Result-History-/Persisted-Result-Pfad,
7. dem vorhandenen Kalibrierungs-Frame-Workflow,
8. den vorhandenen Trainings-, Modellkatalog-, Export- und Inferenzgrenzen,
9. den exakten Test- und Buildbefehlen, die in diesem Repository verfügbar sind,
10. einer Liste aller Annahmen aus `Spezifikation.md`, die mit dem realen Code kollidieren.

Besonders prüfen:

- Ob Standard-Property-Typen automatisch im Frontend gerendert werden.
- Ob ein neuer Standard-Node ohne individuelle Frontendänderung im Designer erscheint.
- Ob `RuntimeExecutionContext.state` über aufeinanderfolgende Liveframes geteilt wird.
- Ob der aktuelle Modell-/Trainerpfad Sequenzregression oder Multi-Task-Zeitreihenmodelle bereits unterstützt.

Akzeptanz:

- Jede Aussage nennt einen realen Dateipfad und ein konkretes Symbol.
- Keine neue Architektur wird erfunden.
- Keine Implementierung wird durchgeführt.
- Offene Punkte werden als offen markiert und nicht geraten.
```

---

# 3. Prompt 01 – Reiner Geometriekern aus einer Stammmaske

```text
Lies `AGENTS.md`, `Spezifikation.md` und `docs/stem-orientation/implementation-map.md`.

Ziel dieses Auftrags ist ausschließlich ein deterministischer, unabhängig testbarer Geometriekern. Noch keine Runtime-Node-Registrierung, keine API, kein Frontend, keine Datenbank und kein Training.

Implementiere innerhalb des Pakets `backend/packages/cv-runtime` eine fachlich gekapselte Geometriekomponente für genau eine segmentierte Stamminstanz.

Der Eingang besteht aus:

- Polygon oder binärer Maske einer einzelnen Stamminstanz,
- Bildbreite und Bildhöhe,
- optionaler Konfiguration.

Der Ausgang muss als typisiertes internes Ergebnis mindestens enthalten:

- robuste projizierte Referenzachse,
- sichtbare Endpunkte,
- normierte Längskoordinate `s` von 0 bis 1,
- Mittellinienprofil mit standardmäßig 32 Stützstellen,
- Breitenprofil mit standardmäßig 16 Stützstellen,
- Residualprofil relativ zur geraden Referenzachse,
- vorzeichenbehaftete Krümmungskennzahl,
- RMS-Abweichung,
- maximale absolute Abweichung,
- Peak-to-Peak-Wert,
- sichtbarer Längenanteil,
- verworfene beziehungsweise unsichere Querschnitte,
- Qualitätsflags und begründete Fehlercodes.

Algorithmische Mindestanforderungen:

1. größte zusammenhängende sinnvolle Maskenkomponente verwenden,
2. Längsrichtung robust bestimmen,
3. globale Translation und Schräglage entfernen, aber die projizierte Krümmung erhalten,
4. Querschnitte orthogonal zur Referenzachse auswerten,
5. lokale Äste und kurze Konturauswüchse robust begrenzen,
6. unvollständig sichtbare Enden kennzeichnen,
7. keine Millimeterwerte behaupten, solange keine metrische Kalibrierung vorliegt,
8. keine räumliche 3D-Bogenrichtung aus einer einzelnen schrägen Ansicht behaupten.

Lege Unit-Tests mit synthetischen Masken an:

- gerader Stamm,
- C-Bogen nach beiden Vorzeichenrichtungen,
- S-Bogen,
- konischer Stamm,
- Astwulst als lokaler Ausreißer,
- abgeschnittenes Stammende,
- stark fragmentierte oder zu kleine Maske,
- Rotation und Translation derselben Maske.

Akzeptanz:

- Rotation und Translation im Bild ändern das normierte Residualprofil nur innerhalb klar definierter Toleranzen.
- Ein lokaler Ast verändert den niederfrequenten Bogenwert nicht unkontrolliert.
- Gerade synthetische Stämme liefern einen Wert nahe null.
- C-Bögen mit entgegengesetzter Richtung liefern entgegengesetzte Vorzeichen.
- Es wurden ausschließlich der Geometriekern und zugehörige Tests geändert.
```

---

# 4. Prompt 02 – Runtime-Payload und Node `analysis.stem_mask_geometry`

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md` und den in Prompt 01 erstellten Geometriekern.

Ziel: Der bestehende Runtime-Pipeline-Editor soll nach einem vorhandenen `inference.segmentation`-Node einen neuen fachlichen Analyse-Node ausführen können.

Verwende die bestehenden Verträge:

- `SegmentPayload` als Eingang,
- `RuntimeNodeHandler`, `RuntimeExecutionContext`, `NodeExecutionResult`,
- bestehende Registry- und Handler-Loader-Konvention,
- Overlay-Payloads für Debugdarstellung.

Implementiere:

1. einen typisierten Runtime-Payload für Stammgeometrie beziehungsweise Stammbeobachtung nach bestehender `RuntimePayload`-Konvention,
2. einen Handler in einem fachlichen Modul analog zum Muster `cv_runtime.nodes.board_cut`,
3. den Node-Typ `analysis.stem_mask_geometry` in `cv_runtime.registry`,
4. Eingangsport `segments`, optional `frame`,
5. Ausgang `stem_geometry` und optional `overlay`,
6. Node-Eigenschaften für Klassenfilter, Mindestscore, 32/16 Stützstellen, Glättung, Endbeschnitt und Debugoverlay,
7. Fail-closed-Verhalten bei keiner eindeutigen Stamminstanz.

Die Auswahl einer einzelnen aktiven Instanz bleibt in diesem Auftrag bewusst einfach und konfigurierbar, beispielsweise über `largest_matching_segment`; ein zeitliches Active-Stem-Tracking folgt separat.

Nutze keine neue API und keine neue Datenbanktabelle.

Akzeptanz:

- Der Node ist über den vorhandenen Node-Katalog abrufbar.
- Der vorhandene Pipeline-Designer kann ihn mit Standardfeldern konfigurieren, ohne eine parallele Designer-Seite.
- Eine Pipeline `source.file_test -> inference.segmentation -> analysis.stem_mask_geometry -> output.overlay` ist validierbar und previewfähig.
- Der Node gibt die Geometriewerte und ein nachvollziehbares Overlay mit Referenzachse, Mittellinie und verwendeten Stützstellen aus.
- Tests prüfen Registry, Handler-Loading, Payload-Serialisierung und mindestens einen Pipeline-Engine-Lauf.
```

---

# 5. Prompt 03 – Kamerakalibrierung und generischer Undistort-Node

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md` sowie den bestehenden Calibration-Frame-Workflow im Runtime-Admin-API und Pipeline-Designer.

Ziel: Intrinsische Objektivverzeichnung, insbesondere tonnenförmige Verzeichnung, soll korrigiert werden. Die beabsichtigte schräge Kameraperspektive darf nicht zu einer künstlichen Draufsicht umgerechnet werden.

Implementiere in zwei getrennten Teilen:

A. Generischer Runtime-Node

- Node-Typ `preprocessing.camera_undistort`
- Eingang `frame`, Ausgang `frame`
- bestehende `FramePayload`- und `RuntimeImageContext`-Provenance verwenden
- OpenCV-Kameramatrix und Distortionskoeffizienten anwenden
- Auflösung und Kalibrierungsauflösung validieren
- gültigen ROI und Transformationsmetadaten im Image-Context dokumentieren
- Remap-Matrizen im vorhandenen Runtime-State cachen, nicht pro Frame neu berechnen

B. Kalibrierungsworkflow innerhalb der bestehenden Runtime-Admin-Oberfläche

- bestehenden Calibration-Frame-Capture-Pfad wiederverwenden
- ChArUco oder Checkerboard unterstützen
- mehrere Kalibrierungsbilder sammeln
- Kameramatrix, Distortionskoeffizienten und Reprojektionfehler berechnen
- Vorher-/Nachher-Ansicht bereitstellen
- Profil versioniert einem Runtime-Channel zuordnen
- Produktionsfreigabe nur bei validem Profil

Vor einer Datenbankmigration zuerst prüfen, ob Channel-Source-Config beziehungsweise bestehende Kalibrierungsfelder ausreichen. Eine neue Tabelle nur dann anlegen, wenn Versionierung, Freigabe und Referenzierung andernfalls nicht sauber möglich sind; die Entscheidung dokumentieren.

Akzeptanz:

- Gerade synthetische Referenzlinien bleiben nach Korrektur gerade.
- Falsche Auflösung führt zu einem klaren Fehler oder einer expliziten, getesteten Skalierung.
- Die schräge Pose bleibt sichtbar und wird nicht homographisch „begradigt“.
- Runtime-Preview kann Roh- und entzerrtes Bild vergleichen.
- Kalibrierungsdaten bleiben über Installer-Update und Rollback erhalten.
```

---

# 6. Prompt 04 – Aktiven Stamm auswählen und über Frames verfolgen

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md` und die bestehenden stateful Nodes `analysis.zone_entry_exit` sowie `analysis.optical_flow_motion`.

Ziel: Aus mehreren segmentierten Stämmen wird der aktuell zur Übergabe beziehungsweise im Längsförderer relevante Stamm ausgewählt und mit einer stabilen Track-ID über aufeinanderfolgende Frames verfolgt.

Implementiere den stateful Node `analysis.active_stem_tracker`.

Eingänge:

- `segments`
- optional `frame`
- optional Maschinen-/Triggerdaten, soweit ein bestehender Payload dafür vorhanden ist

Ausgänge:

- genau eine aktive Stamminstanz mit Track-ID,
- Auswahlkonfidenz,
- Auswahlgrund,
- Bewegungswerte,
- optional Debugoverlay.

Auswahl- und Trackingkriterien:

- konfigurierbare Übergabe- und Längsförderer-ROIs,
- Klassen- und Scorefilter,
- räumliche Nähe zum erwarteten Übergabepunkt,
- zeitliche IoU-/Zentroid-/Formkontinuität,
- Bewegungsrichtung,
- Reihenfolge und Ausschluss bereits abgeschlossener Tracks,
- sichtbarer Längenanteil,
- Mehrdeutigkeit muss fail-closed enden.

Verwende `RuntimeExecutionContext.state` mit einem node-spezifischen Schlüssel. Trage den neuen Node-Typ an der realen Stelle in die Liste der stateful Pipeline-Nodes ein, sodass die bestehende Realtime-Ausführung Frames serialisiert und denselben Pipeline-State verwendet.

Akzeptanz:

- Track-ID bleibt bei normaler Bewegung stabil.
- Kurzzeitiges Verdeckungsszenario wird innerhalb einer konfigurierbaren Frist überbrückt.
- Ein Stammwechsel erzeugt eine neue Track-ID.
- Bei zwei gleich plausiblen Kandidaten wird keine sichere aktive Instanz behauptet.
- Realtime-Metriken weisen die Pipeline als stateful und serialisiert aus.
```

---

---

# 7. Prompt 05 – Relative Stammrotation und Vor-Einzug-Winkel als deterministische Baseline

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md` und die Ergebnisse der Prompts 01 bis 04. Prüfe insbesondere die aktuell implementierten Payloads, Ports und stateful Node-Typen, bevor du Verträge erweiterst.

Ziel dieses Auftrags ist nicht nur eine spätere Qualitätsbewertung während des Drehens. Die Software muss bereits aus der Bildfolge vom Einwurf bis zur konfigurierten Entscheidungslinie eine konkrete Vor-Einzug-Empfehlung ableiten:

- signierte Korrektur in Grad,
- daraus abgeleitete Richtung,
- Konfidenz,
- Ergebnis muss vor dem Greifen beziehungsweise vor der eigentlichen Drehung durch das erste Vorzentrierungs-Walzenpaar bereitstehen.

Die Beobachtung nach dem Greifen ist nur eine spätere Nachkontrolle.

## 1. Prozesszonen

Erweitere die Konfiguration der Stammausrichtung um kalibrierte Bildzonen beziehungsweise Linien:

- `observation_entry_roi`: Beginn der relevanten Beobachtung,
- `throw_in_roi`: Einwurf-/Rollbereich,
- `decision_line` oder `decision_roi`: letzte Position, an der die Hauptentscheidung spätestens feststehen muss,
- `first_roller_roi`: nur zur Diagnose und späteren Nachkontrolle,
- konfigurierbare positive Drehrichtung und eindeutiger Betrachtungsstandpunkt, z. B. `clockwise_viewed_from_configured_log_end`.

Die Konvention muss im Result, in Artefakten und im Overlay eindeutig sichtbar sein. Begriffe wie „Uhrzeigersinn“ sind ohne gespeicherten Betrachtungsstandpunkt unzulässig.

## 2. Bildbasierte Rotationsspur

Implementiere einen reinen, testbaren Kern zur relativen Drehverfolgung der Stammoberfläche. Er darf im bestehenden `analysis.stem_temporal_orientation` liegen oder als fachlich klarer Runtime-Node `analysis.stem_surface_rotation` extrahiert werden, wenn die vorhandenen Ports sonst zu einem unübersichtlichen Super-Node führen. Begründe die Wahl im Change Report.

Benötigte Eingänge:

- entzerrter Frame,
- aktive Stammmaske beziehungsweise aktiver Track,
- `stem_geometry`,
- Framezeitstempel und Framefolge.

Die Rotationsspur muss mindestens enthalten:

- `delta_rotation_deg` zum letzten gültigen Frame,
- `cumulative_rotation_deg_unwrapped`,
- `rotation_direction`,
- `rotation_confidence`,
- Anzahl und Qualität verfolgter Rinden-/Astmerkmale,
- Inlier-Anteil,
- erkannte Translationskomponente,
- `trace_discontinuity`,
- Reason Codes.

Anforderungen:

- Featuretracking beziehungsweise optischer Fluss ausschließlich innerhalb eines robust erodierten Stamm-Maskenbereichs,
- globale Stammtranslation, Änderung der Bildlage und Achsrotation möglichst herausrechnen,
- mehrere Merkmale robust zusammenfassen; kein einzelner Ast darf den Winkel bestimmen,
- bei zu wenigen Merkmalen, Bewegungsunschärfe, Verdeckung oder Sprung keine Gradzahl erfinden,
- Unwrap über mehrere Frames, aber sauberer Reset bei Trackwechsel und Diskontinuität,
- eine kalibrierbare Zuordnung zwischen beobachteter Oberflächenbewegung und Winkel in Grad,
- Unterstützung eines Offline-Kalibrierungsdatensatzes mit temporären Umfangsmarkierungen oder bekannten Winkelstellungen,
- Kalibrierungs-ID und Methode in jeder Rotationsspur speichern.

Die erste Gradzuordnung darf empirisch kalibriert sein. Sie muss aber versioniert, testbar und durch bekannte Testwinkel geprüft werden. Eine ungeprüfte Pixel-zu-Grad-Konstante ist unzulässig.

## 3. Deterministische Vor-Einzug-Schätzung

Erweitere den stateful Node `analysis.stem_temporal_orientation` so, dass er für denselben Track synchron führt:

- projizierte Krümmungswerte aus `stem_geometry`,
- kumulative relative Rotation,
- Qualitäts- und Sichtbarkeitswerte,
- Kandidaten für eine gute Zielstellung,
- letzten gültigen Entscheidungsframe vor der `decision_line`.

Die Baseline soll die Beziehung

`projizierte Krümmung über kumulativer Stammrotation`

analysieren und mindestens erkennen:

- Verbesserung und Verschlechterung,
- Vorzeichenwechsel beziehungsweise Nulldurchgang,
- ein eingeklammertes oder überschrittenes Projektionsminimum,
- mehrdeutige beziehungsweise S-förmige Verläufe,
- unzureichend beobachtete Rotation,
- Diskontinuität der Rotationsspur.

Sobald ein belastbarer Zielkandidat und ein gültiger Entscheidungsframe vorliegen, berechne:

`correction_deg = wrap_to_half_turn(target_rotation_deg - decision_rotation_deg)`

mit einem Wertebereich von `[-90°, +90°)`, weil Zielstellungen im Abstand von 180° für „Bogen oben“ und „Bogen unten“ als gleichwertig behandelt werden.

Die Baseline darf eine Gradzahl nur ausgeben, wenn:

- die Winkelkalibrierung gültig ist,
- die Rotationsspur bis zum Entscheidungsframe zusammenhängend ist,
- die beobachtete Rotation ausreicht,
- der Zielkandidat eindeutig genug ist,
- die Qualitätsschwellen erfüllt sind.

Da eine schräge Kamera nur eine Projektion sieht, kennzeichne das Ergebnis ausdrücklich als `projected_baseline`, solange keine menschlich gelabelte Zielstellung oder ein validiertes Temporalmodell vorliegt. Bei Unsicherheit lautet das Ergebnis `manual_required` und nicht eine scheinpräzise Gradzahl.

## 4. Ergebnis vor dem Walzenpaar

Der Node muss spätestens am Entscheidungsframe ein einfrierbares Ergebnis erzeugen:

- `decision_ready`,
- `recommended_rotation_deg_signed`,
- `recommended_rotation_deg_abs`,
- `recommended_direction`,
- `target_frame_or_interval_candidate`,
- `decision_frame`,
- `target_observed`,
- `bow_relevant`,
- `confidence`,
- `estimate_method`,
- `reason_codes`,
- vollständige Versions- und Richtungsprovenienz.

Dieses Ergebnis ist in diesem Auftrag ausschließlich Shadow Mode. Keine SPS-Schreiboperation.

## 5. Tests

Erstelle mindestens:

- synthetische Rotationsspur mit bekannten Winkelschritten,
- reine Translation ohne Rotation,
- Rotation mit kurzzeitigem Rutschen,
- Featureverlust und Wiederauftauchen,
- F1–F4-artige Folge mit bekanntem Winkel zwischen den Frames und Zielbereich zwischen F3 und F4,
- Modulo-180-Fälle nahe `+89°/-89°`,
- Trackwechsel und Runtime-Neustart.

Akzeptanz:

- Das Resultat ist vor der Entscheidungslinie verfügbar.
- Eine bekannte synthetische Sequenz liefert Richtung und Winkel innerhalb festgelegter Toleranz.
- Reine Translation erzeugt keine relevante kumulative Rotation.
- Bei ungültiger Kalibrierung oder diskontinuierlicher Spur wird keine Gradzahl ausgegeben.
- Bestehende Pipelines ohne die neuen Nodes bleiben unverändert.
- Alle Entscheidungen sind über Overlay und Reason Codes nachvollziehbar.
```

---

# 8. Prompt 06 – Shadow-Ergebnis, Vor-Einzug-Review und Result-History

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md`, die Ergebnisse aus Prompt 05 und den vorhandenen Pfad für `output.persisted_result`, Result Service und Result-History-Frontend.

Ziel: Die vor dem ersten Walzenpaar berechnete Hauptempfehlung muss im bestehenden Resultpfad gespeichert, gefiltert und nachvollzogen werden können. Die spätere Lage nach einer Drehung ist nur ein zusätzlicher Kontrollwert.

Jeder gespeicherte Stammfall enthält mindestens:

- Track-/Session-ID,
- Channel und Pipeline-Version,
- Beobachtungsbeginn und Entscheidungszeitpunkt,
- Segmentierungsmodell-, Kalibrierungs- und Featureversion,
- Prozesszonen-/Entscheidungslinien-Version,
- kompakte Krümmungszeitreihe,
- kompakte Rotationsspur mit Qualitätswerten,
- automatisch bestimmter Zielkandidat,
- Entscheidungsframe,
- vor dem Einzug berechnete signierte Korrektur in Grad,
- Richtungskonvention und Blickstandpunkt,
- Konfidenz, Methode und Reason Codes,
- optionale Restkorrektur beziehungsweise Nachkontrolle,
- Verweise auf Roh-/Diagnosevideo und ausgewählte Frames.

Das Liveoverlay zeigt spätestens vor der Entscheidungslinie beispielsweise:

`Drehe 43° gegen den Uhrzeigersinn – Konfidenz 0,84`

oder bei fehlender Absicherung:

`Keine sichere Winkelentscheidung – Bediener erforderlich`.

Erweitere die vorhandene Result-History oder einen darin integrierten Stammausrichtungs-Review. Keine zweite Web-App.

Der Schnellreview darf zur Diagnose folgende Felder erfassen:

- Vorschlag plausibel: ja / nein / unklar,
- Richtung korrekt / falsch / unklar,
- Winkel grob zu klein / passend / zu groß / nicht beurteilbar,
- Bogen relevant / nahezu gerade / unklar,
- finale Lage `good` / `acceptable` / `bad`,
- optional manuell markierter Zielzeitpunkt beziehungsweise kurzer Zielbereich,
- Kommentar und Sonderfall.

Dieser Schnellreview ersetzt nicht das spätere verbindliche Label-Studio-Labeling. Er darf jedoch als Vorannotation und für Kandidatenauswahl verwendet werden.

Eine neue Datenbanktabelle ist nur zulässig, wenn die vorhandene Result-History keine versionierten Sequenzannotationen und Zielintervalle aufnehmen kann. Begründe die Entscheidung.

Akzeptanz:

- Ein Vor-Einzug-Result erscheint vor dem Walzenkontakt im vorhandenen Live-/Resultpfad.
- Richtung, Winkel, Blickkonvention, Konfidenz und Entscheidungsframe sind eindeutig dargestellt.
- Ein späterer Nachkontrollwert überschreibt niemals unbemerkt die ursprüngliche Hauptempfehlung.
- Reviewdaten bleiben über Updates erhalten.
- Noch keine produktive SPS-Schreiboperation.
```

---

# 9. Prompt 06A – Korrekturprompt, falls Prompts 05 und 06 der Version 3 bereits umgesetzt wurden

Dieser Prompt ist **jetzt vor Prompt 07A auszuführen**, wenn die bisherige Implementierung nur Qualitätsverlauf, `continue/reverse/hold` oder Testimpulse vorsieht.

```text
Arbeite auf dem aktuellen Repositorystand nach den bereits ausgeführten Prompts 00 bis 06. Setze bestehende Änderungen nicht zurück. Lies zuerst die tatsächlich vorhandene Implementierung und mindestens:

- backend/packages/cv-runtime/src/cv_runtime/payloads.py
- backend/packages/cv-runtime/src/cv_runtime/context.py
- backend/packages/cv-runtime/src/cv_runtime/registry.py
- backend/packages/cv-runtime/src/cv_runtime/engine.py
- backend/packages/cv-runtime/src/cv_runtime/nodes/stem_orientation.py, falls vorhanden
- weitere durch Prompts 01–06 erzeugte Stammdateien
- backend/services/realtime-executor/src/realtime_executor/live.py
- backend/packages/cv-db/src/cv_db/runtime_execution_store.py
- backend/packages/cv-db/src/cv_db/models.py
- frontend/apps/admin-web/src/features/results/result-history-page.tsx
- frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx
- alle Tests, die durch Prompts 01–06 entstanden sind

Erstelle vor der Änderung eine kurze Liste:

- vorhandene Stamm-Payloads,
- vorhandene Node-Typen und Ports,
- vorhandene State-Felder,
- bestehender Resultvertrag,
- aktuelle Reviewfelder,
- Annahmen der v3-Implementierung, die dem neuen Hauptziel widersprechen.

Ziel: Korrigiere die bestehende Implementierung so, dass die primäre Aufgabe eine Vor-Einzug-Winkelentscheidung ist. Die Hauptausgabe muss vor dem ersten Vorzentrierungs-Walzenpaar bereitstehen. Eine scorebasierte Suche während des Drehens bleibt höchstens optionale Nachkontrolle.

## A. Konfiguration und Prozessdeadline

Ergänze in der bestehenden Node-Konfiguration:

- `observation_entry_roi`,
- `throw_in_roi`,
- `decision_line` beziehungsweise `decision_roi`,
- `first_roller_roi`,
- `rotation_viewpoint_convention`,
- `positive_rotation_direction`,
- minimale/maximale Sequenzlänge,
- Confidence-Schwellen,
- Deadband und maximal ausgebbarer Winkel.

Nutze vorhandene ROI-/Property-Konventionen des Pipeline-Designers. Keine separate Konfigurationsseite, außer die vorhandenen Renderer reichen nachweislich nicht aus.

## B. Rotationskern und Payload

Erstelle einen reinen, unit-testbaren Rotationskern, bevorzugt in:

- `backend/packages/cv-runtime/src/cv_runtime/stem_rotation.py`

oder einem anhand der vorhandenen Paketstruktur besser passenden Pfad.

Ergänze einen typisierten `StemRotationPayload` mit mindestens:

- Track-/Frame-/Zeitbezug,
- `delta_rotation_deg`,
- `cumulative_rotation_deg_unwrapped`,
- Richtung,
- Confidence,
- Feature-/Inlierstatistik,
- Translationskomponente,
- Diskontinuitätsflag,
- Rotationskalibrierungs-ID,
- Blick-/Vorzeichenkonvention,
- Reason Codes.

Die Rotation wird aus mehreren Merkmalen im robusten Stamm-Maskeninneren ermittelt. Reine Translation, Änderung der Stammachse und Bildmaßstab sind soweit möglich zu kompensieren. Bei unzureichender Kalibrierung oder Qualität bleibt der Gradwert ungültig.

Entscheide anhand der vorhandenen Ports:

1. eigener stateful Node `analysis.stem_surface_rotation` mit Eingängen `frame`, aktiver Stamm und `stem_geometry`, oder
2. klar getrennter Handler/Kern innerhalb des vorhandenen Temporal-Nodes.

Bevorzuge den separaten Node, wenn die Runtime mehrere Inputs sauber unterstützt. Registriere ihn im Node-Katalog und in `_STATEFUL_PIPELINE_NODE_TYPES`. Dokumentiere die Entscheidung.

## C. Temporal-Node und Vor-Einzug-Winkel

Erweitere den vorhandenen `analysis.stem_temporal_orientation` rückwärtskompatibel:

- Geometrie- und Rotationsspur pro Track führen,
- Zielkandidaten aus Krümmungsverlauf über kumulativer Rotation bestimmen,
- Decision Frame beim Überschreiten der konfigurierten Decision Line einfrieren,
- Korrektur `wrap_to_half_turn(phi_target - phi_decision)` in `[-90°, +90°)` berechnen,
- bei validem Deadband `no_rotation`,
- bei Unsicherheit `manual_required`,
- keine Frames nach der Decision Line für die Hauptentscheidung verwenden.

Der bisherige Qualitäts-/Trendverlauf bleibt als Diagnose erhalten. `continue/reverse/hold` wird nur noch für optionale spätere Nachkorrektur verwendet.

## D. Resultvertrag und UI

Führe `stem.orientation.v2` ein oder erweitere den bestehenden Vertrag mit sauberer Versionierung. Mindestens:

- `decision_ready`,
- Decision Frame und Gültigkeitszeit,
- `recommended_rotation_deg_signed`,
- `recommended_rotation_deg_abs`,
- `recommended_direction`,
- Blick-/Vorzeichenkonvention,
- Zielkandidat,
- `target_observed`,
- `bow_relevant`,
- `angle_confidence`,
- `estimate_method`,
- Reason Codes und Versionen,
- separates optionales `post_rotation_verification`.

Bestehende v1-Leser dürfen nicht ungeprüft brechen. Ergänze Migration/Adapter oder parallele Felder nach vorhandener Contract-Konvention.

Overlay und Result-History müssen die eingefrorene Vor-Einzug-Hauptempfehlung eindeutig zeigen. Ein späterer Kontrollwert darf sie nicht überschreiben.

## E. Tests

Ergänze mindestens:

- bekannte positive/negative Rotationsschritte,
- reine Translation,
- Rotation plus Translation,
- Schlupf/kurze Stagnation,
- Featureverlust und Trace-Diskontinuität,
- Modulo-180-Grenzen,
- F1–F4-artige Sequenz mit bekanntem Winkel und Ziel zwischen F3/F4,
- Decision Frame vor dem Walzen-ROI,
- Trackwechsel und Runtime-Neustart,
- Rückwärtskompatibilität des bisherigen Resultpfads.

Nicht in diesem Auftrag:

- keine Label-Studio-/Dataset-Erweiterung,
- kein TCN/GRU,
- keine SPS-Schreibfreigabe,
- keine offene Testimpulsregelung.

Akzeptanz:

- Die bestehende v3-Implementierung bleibt baubar und vorhandene Resultleser brechen nicht.
- Vor der Decision Line kann im Shadow Mode eine abgesicherte signierte Gradkorrektur ausgegeben werden.
- Ohne gültige Rotationskalibrierung oder zusammenhängende Trace wird keine Gradzahl erfunden.
- Hauptentscheidung verwendet keine späteren Walzenframes.
- Der Change Report listet exakt, welche v3-Annahmen ersetzt, beibehalten oder migriert wurden.
```
---

# 10. Verbindliche Datenannahmen ab Prompt 07

Für die erste Trainingsphase gelten:

- Es stehen zunächst keine aufgezeichneten SPS- oder Bedienertastensignale zur Verfügung.
- Der Beobachtungsclip endet automatisch an einer visuell konfigurierten Entscheidungslinie **vor** dem ersten Vorzentrierungs-Walzenpaar.
- Der Labeler markiert in Label Studio den Frame beziehungsweise kurzen Zeitbereich, in dem der Bogen innerhalb der beobachteten Sequenz am besten vertikal stand.
- Die Software berechnet aus diesem Zielintervall und der bildbasierten Rotationsspur den signierten Korrekturwinkel zum Entscheidungsframe.
- Das manuelle Label ist der Zielbereich, nicht eine vom Labeler geschätzte Gradzahl.
- `final_orientation_quality` mit `good`, `acceptable`, `bad` bleibt ein sekundäres Label für die Lage am Entscheidungsfenster.
- Ist keine gute Zielstellung sichtbar oder die Rotationsspur unzuverlässig, wird kein Winkelziel erzeugt; die Sequenz kann trotzdem für andere Köpfe verwendet werden.
- Das verbindliche Labeling erfolgt im automatisch provisionierten Label-Studio-Projekt. Keine separate Stamm-Labeling-Anwendung.
- Dataset-, Timeline-Annotation-, Snapshot- und Trainergrundlagen werden allgemein für weitere Sequenzaufgaben umgesetzt; kein `TaskType.STEM_ORIENTATION`.

Ergänze die Root-`AGENTS.md` um:

```markdown
## Primäres Ziel der Stammausrichtung

- Die primäre Runtimeausgabe ist eine signierte Korrektur in Grad, die vor dem ersten Vorzentrierungs-Walzenpaar bereitsteht.
- Die Beobachtung während beziehungsweise nach der Hauptdrehung ist nur eine begrenzte Nachkontrolle.
- Eine offene Testimpuls-Suche darf nicht den Hauptalgorithmus ersetzen.
- Ohne gültige visuelle Rotationsspur, eindeutige Richtungsdefinition und ausreichende Konfidenz wird keine Gradzahl ausgegeben.
- Uhrzeigersinn/Gegenuhrzeigersinn ist immer relativ zu einem versionierten, konfigurierten Betrachtungsstandpunkt definiert.

## Temporale Trainingsdaten

- Eine Sequenz endet am visuellen Entscheidungsframe vor dem ersten Walzenpaar.
- Der Labeler markiert in Label Studio einen Zielorientierungsbereich auf der Videozeitleiste.
- Der signierte Winkel wird aus Zielbereich plus versionierter visueller Rotationsspur abgeleitet; er wird nicht vom Labeler geraten.
- `good/acceptable/bad` ist ein zusätzliches Endlagenlabel und nicht das alleinige Trainingsziel.
- Niedrige Rotationskonfidenz oder ein nicht sichtbares Ziel erzeugt kein gültiges Winkelziel.
- Temporale Dataset-, Label-Studio-, Snapshot- und Trainererweiterungen sind allgemein wiederverwendbar.
```

---

# 11. Prompt 07A – Allgemeiner Dataset-Typ und Schema-Registry für zeitliche Sequenzen

```text
Lies `AGENTS.md`, `Spezifikation.md`, `docs/stem-orientation/implementation-map.md` sowie:

- backend/packages/cv-status/src/cv_status/enums.py
- backend/packages/cv-contracts/src/cv_contracts/dataset.py
- backend/packages/cv-datasets/src/cv_datasets/models.py
- backend/packages/cv-datasets/src/cv_datasets/conversion.py
- backend/packages/cv-datasets/src/cv_datasets/materialization.py
- backend/packages/cv-datasets/src/cv_datasets/validation.py
- backend/packages/cv-db/src/cv_db/models.py
- backend/packages/cv-db/src/cv_db/repositories.py
- backend/services/admin-api/src/admin_api/api/routes/datasets.py
- backend/services/dataset-service/src/dataset_service/api/routes.py
- frontend/packages/schemas
- frontend/apps/admin-web/src/features/datasets

Ziel: Baue eine allgemeine, rückwärtskompatible Datasetgrundlage für zeitliche Sequenzen mit gemischten Annotationen. Verwende keinen stammspezifischen Task-Typ und zwinge die Aufgabe nicht in reine Bild- oder reine Temporal-Classification.

Führe einen allgemeinen Task-Typ nach Repository-Konvention ein, bevorzugt `temporal_sequence`. Falls ein anderer Name technisch besser passt, dokumentiere ihn in einer ADR-Notiz; die Semantik muss Timeline-Intervalle, Sequenzklassen und abgeleitete Regressionsziele unterstützen.

Definiere neben vorhandenen Bilditems ein kanonisches Sequenzitem mit mindestens:

- `sequence_id`, `group_id`,
- primäres Videoasset und optionale Diagnoseassets,
- geordnete Frameindizes und Zeitstempel,
- `decision_frame_index` und `decision_timestamp`,
- URI/SHA256 des Featuremanifests,
- URI/SHA256 der Rotationsspur,
- Feature-, Rotation-, Kalibrierungs- und Annotationsschema,
- menschliche Sequenz- und Timeline-Labels,
- abgeleitete Targets getrennt von Human Labels,
- Labelquelle und Labelqualität,
- Ausschlussgrund,
- Split,
- vollständige Provenance,
- keine Inline-Videos, Rohbilder oder unbeschränkten Framearrays in der Datenbank.

Erweitere beziehungsweise erstelle eine allgemeine Annotation-Schema-Registry. Ein Schema kann mindestens definieren:

- Sequenz-Choices,
- boolesche Labels,
- Timeline-Punkte oder -Intervalle,
- Mehrfachauswahl und Freitext,
- Abhängigkeiten/Required-When-Regeln,
- Parser und Validierung,
- einen versionierten `target_builder`, der aus Human Labels und automatisch aufgezeichneten Spuren Trainingsziele ableitet.

Registriere als erstes Schema `stem_orientation_angle.v1`:

- `sequence_valid`: `usable`, `unusable`,
- `bow_relevance`: `relevant`, `nearly_straight`, `unclear`,
- `target_visible`: `yes`, `no`, `unclear`,
- Timeline-Intervall `target_orientation`: Bereich, in dem der Bogen am besten vertikal steht,
- `final_orientation_quality`: `good`, `acceptable`, `bad`,
- optional `special_case`: `occluded`, `motion_blur`, `tracking_failed`, `rotation_trace_invalid`, `insufficient_rotation`, `multiple_stems`, `incomplete_sequence`, `ambiguous_target`, `s_bow`,
- optional `comment`.

Schema-Regeln:

- Bei `sequence_valid=unusable` wird die Sequenz vollständig ausgeschlossen.
- Bei `bow_relevance=nearly_straight` ist kein Winkelziel erforderlich; sie bleibt für Relevanz-/No-Rotation-Training verwendbar.
- Bei `bow_relevance=relevant` und `target_visible=yes` ist genau ein gültiges Zielintervall erforderlich.
- Das Zielintervall muss innerhalb der Frames bis einschließlich Entscheidungsframe liegen.
- Bei `target_visible=no/unclear` wird kein Winkelziel erzeugt.
- `final_orientation_quality` beschreibt nur das finale Entscheidungsfenster und darf nicht auf alle Frames kopiert werden.
- Der Labeler gibt keinen Gradwert ein.

Bestehende Bilddatasets und Manifeste bleiben semantisch und nach Möglichkeit bytekompatibel.

Akzeptanz:

- Bestehende Dataset-Tests bleiben grün.
- Ein allgemeines Temporal-Sequence-Dataset kann erstellt, gelesen und aufgelistet werden.
- Das registrierte Schema erzeugt deterministisch dieselbe Label-Studio-Konfiguration.
- Timeline-Intervalle und Sequenz-Choices werden typisiert validiert.
- Ungültige Zielintervalle nach dem Entscheidungsframe werden abgelehnt.
- Noch kein externes Projekt und kein Import.
```

---

# 12. Prompt 07B – Label-Studio-Projekt automatisch anlegen und zuordnen

```text
Lies `AGENTS.md`, `Spezifikation.md`, die Ergebnisse aus Prompt 07A und:

- backend/packages/cv-datasets/src/cv_datasets/label_studio.py
- backend/packages/cv-db/src/cv_db/repositories.py
- backend/workers/control-worker/src/control_worker/jobs/dataset.py
- backend/services/admin-api/src/admin_api/api/routes/datasets.py
- backend/services/dataset-service/src/dataset_service/api/routes.py
- frontend/packages/api-client/src/index.ts
- frontend/apps/admin-web/src/features/datasets/project-mapping-page.tsx
- frontend/apps/admin-web/src/features/datasets/datasets-page.tsx

Ziel: Ein Dataset kann aus der bestehenden Inferenzserver-Oberfläche ein passendes Label-Studio-Projekt automatisch und idempotent provisionieren. Keine manuelle Projekt-ID und keine rohe XML-Eingabe im Standardweg.

Erweitere den vorhandenen `LabelStudioClient` nach bestehenden HTTP-, Audit- und Fehlerkonventionen um Projektanlage, Lesen, Aktualisierung und nötige Kompensation. Verwende die im installierten Label-Studio-System tatsächlich vorhandene API und kapsle alle Details im Client.

Der allgemeine Provisionierungsvertrag enthält mindestens:

- `dataset_id`,
- `title`,
- `annotation_schema_id`,
- `training_pipeline_slug` beziehungsweise Recipe-Referenz,
- Auto-Sync-/Auto-Snapshot-Optionen,
- Splitkonfiguration,
- optionalen Idempotency-Key.

Der Worker-Job:

1. validiert Dataset und Schema,
2. erzeugt die XML-Konfiguration nur aus der Schema-Registry,
3. verwendet für Sequenzen eine Videoansicht mit Timeline-Label `target_orientation`,
4. zeigt die Sequenz-Choices `sequence_valid`, `bow_relevance`, `target_visible`, `final_orientation_quality`, Sonderfall und Kommentar,
5. prüft vorhandene Zuordnung für `dataset_id + schema_id`,
6. legt bei Bedarf genau ein Projekt an,
7. speichert Mapping, Schema-/Config-Version und Syncoptionen,
8. schreibt Auditdaten,
9. behandelt Teilfehler nachvollziehbar.

Frontend:

- Aktion „Label-Studio-Projekt anlegen“ in bestehender Dataset-/Mappingseite,
- Dataset, Schema und Titel auswählen,
- kein XML-/Projekt-ID-Feld im Standardablauf,
- nach Erfolg vorhandenen Link zu Label Studio anzeigen,
- manuelles Mapping für Altprojekte bleibt erhalten.

Akzeptanz:

- Ein Klick erzeugt genau ein Projekt und Mapping.
- Wiederholung erzeugt kein Duplikat.
- Das Projekt zeigt ein Video mit markierbarem Zielintervall und den definierten Sequenzlabels.
- Client- und Workerfehler sind getestet und auditierbar.
- Keine eigene Stamm-Labeling-UI.
```

---

# 13. Prompt 07C – Vor-Einzug-Sequenz, Featuremanifest und Rotationsspur automatisch aufzeichnen

```text
Lies `AGENTS.md`, `Spezifikation.md`, die Ergebnisse der Prompts 05/06 beziehungsweise 06A und die vorhandenen Capture-, Result-, Asset-, Channel- und Storagepfade.

Ziel: Aus jeder geeigneten Stammausrichtungs-Session entsteht automatisch ein reproduzierbares Temporal-Sample, dessen primärer Clip spätestens am Entscheidungsframe vor dem ersten Walzenpaar endet.

Implementiere einen allgemeinen Temporal-Sample-Recorder im vorhandenen Runtime-/Result-/Datasetpfad; kein paralleler Recorder-Service.

Sessiongrenzen ohne SPS-Signale:

- Start: aktiver Track tritt in `observation_entry_roi` ein und erfüllt Mindestqualität.
- Laufzeit: Einwurf und Bewegung bis zur Entscheidungslinie.
- Entscheidung: letzter gültiger Frame unmittelbar vor Überschreiten von `decision_line` beziehungsweise Eintritt in `decision_roi`.
- Ende des primären Clips: Entscheidungsframe; keine Frames aus der späteren Walzendrehung als Eingang des Hauptmodells.
- Optional kann ein separates, eindeutig als `verification` gekennzeichnetes Nachkontrollasset aufgezeichnet werden. Es darf nicht unbemerkt in das primäre Trainingssample gelangen.

Pro Sample speichern:

- `sequence.mp4` mit konstanter, dokumentierter FPS und Frameindexzuordnung,
- optional `diagnostic.mp4` mit Maske, Mittellinie, Krümmung, kumulativer Rotation, Entscheidungslinie und Shadow-Schätzung,
- `features.jsonl` mit framegenauen Geometrie-/Qualitäts-/Trackingfeatures,
- `rotation-trace.jsonl` mit relativer/kumulativer Rotation, Konfidenz und Diskontinuitäten,
- `sequence-manifest.json` mit Checksummen, Session-/Track-ID, Decision Frame, Blick-/Drehrichtungskonvention, Versionen und Provenance.

Anforderungen:

- Frame, Feature und Rotationseintrag sind über Frameindex und Zeitstempel eindeutig verbunden.
- MP4-Aufzeichnung blockiert die Realtime-Pipeline nicht; nutze vorhandene Queue-/Assetmechanismen.
- Keine Videos oder Frames inline in der Datenbank.
- Deduplication und Retention konfigurierbar.
- Sample wird nur als angle-label-fähig markiert, wenn die Rotationstrace bis zum Decision Frame gültig ist; die eigentliche Ground Truth entsteht erst nach Label Studio.
- Bei nahezu geradem Stamm kann trotzdem ein Sample für Bow-Relevance entstehen.

Akzeptanz:

- Ein `file_test`-Video erzeugt deterministisch dasselbe Manifest und denselben Decision Frame.
- Clip endet vor dem ersten Walzenbereich.
- Feature- und Rotationstrace sind framegenau ausgerichtet.
- Ein Trackwechsel schließt das alte Sample sauber ab.
- Optionales Verification-Video ist klar getrennt.
```

---

# 14. Prompt 07D – Fertige Sequenzen automatisch und idempotent nach Label Studio importieren

```text
Lies `AGENTS.md`, `Spezifikation.md`, die Ergebnisse aus 07B/07C und den bestehenden Label-Studio-Import-/Syncpfad.

Ziel: Jedes freigegebene Temporal-Sample wird automatisch als genau eine Videoaufgabe in das zugeordnete Label-Studio-Projekt importiert.

Implementiere einen allgemeinen Importjob mit Idempotenzschlüssel mindestens aus:

`dataset_id + label_project_id + sequence_id + primary_asset_sha256`.

Taskdaten enthalten:

- Video-URL über den vorhandenen und abgesicherten Local-Files-/Media-Pfad,
- `sequence_id`, `group_id`,
- Frameanzahl und FPS,
- `decision_frame_index` und Decision-Zeit,
- Feature-, Rotation-, Kalibrierungs- und Annotationsschema,
- Blick-/Drehrichtungskonvention,
- Qualitäts-/Provenance-Metadaten,
- optional URL des Diagnosevideos.

Optional darf die deterministische Baseline einen Zielintervall-Kandidaten als Label-Studio-`prediction` liefern. Er ist deutlich als Vorhersage gekennzeichnet und wird erst durch Bestätigung/Korrektur zur Ground Truth.

Der Labeler führt in Label Studio aus:

1. Sequenz verwendbar beurteilen.
2. Bogenrelevanz beurteilen.
3. Entscheiden, ob eine beste Zielstellung im Clip sichtbar ist.
4. Bei relevantem Bogen und sichtbarem Ziel einen kurzen Timeline-Bereich `target_orientation` markieren.
5. Finale Lage am Clipende als `good`, `acceptable` oder `bad` bewerten.
6. Sonderfälle und Kommentar optional erfassen.

Anforderungen:

- Wiederholter Job erzeugt keinen doppelten Task.
- Label-Studio-Task-ID wird intern mit Sequence-ID und Assetversion verbunden.
- Fehlerhafte oder fehlende Medien werden als `needs_attention` geführt.
- Sampling- und Maximalraten begrenzen Datenwachstum.
- Bestehende Bildtask-Imports bleiben unverändert.

Akzeptanz:

- Video ist in Label Studio abspielbar und framegenau labelbar.
- Zielintervall kann markiert und gespeichert werden.
- Automatische Vorhersage ist korrigierbar und nicht mit Ground Truth verwechselt.
- Doppelte Imports werden verhindert.
```

---

# 15. Prompt 07E – Label-Studio-Export materialisieren und signiertes Winkelziel ableiten

```text
Lies `AGENTS.md`, `Spezifikation.md`, die Ergebnisse aus 07A bis 07D sowie den vorhandenen Snapshot-, DVC-, Validierungs- und Splitpfad.

Ziel: Label-Studio-Annotationen werden in einen immutable allgemeinen Temporal-Snapshot überführt. Für `stem_orientation_angle.v1` wird aus dem menschlich markierten Zielintervall und der aufgezeichneten visuellen Rotationsspur ein versioniertes signiertes Korrekturwinkel-Target zum Decision Frame abgeleitet.

Implementiere einen allgemeinen schemaabhängigen `target_builder`. Der Dataset-Kern kennt allgemeine Human Labels und Derived Targets; die stammbezogene Winkelmathematik liegt in einem registrierten Adapter/Builder und nicht als Sonderfall in allen Datasetfunktionen.

Für jede gültige Stammsequenz:

1. Annotation und Revision aus Label Studio lesen.
2. Video-, Feature- und Rotationstrace-Checksummen prüfen.
3. Decision Frame aus dem Manifest übernehmen und validieren.
4. Zielintervall in Frameindizes/Zeitstempel umrechnen.
5. Kumulative Rotation im Zielintervall robust interpolieren.
6. Zielrotation als Median beziehungsweise robustes Zentrum des Intervalls bestimmen.
7. Korrektur zum Decision Frame berechnen:

   `e = wrap_to_half_turn(phi_target - phi_decision)` in `[-90°, +90°)`.

8. Zusätzlich speichern:

- `signed_correction_deg`,
- `rotation_direction`,
- `target_angle_interval_deg`,
- `angle_tolerance_deg`,
- `sin_2e`, `cos_2e`,
- `angle_label_confidence`,
- `angle_label_source`,
- Rotation-/Kalibrierungs-/Builder-Version,
- Ausschluss-/Reason Codes.

Labelregeln:

- `usable + relevant + target_visible=yes + gültiges Zielintervall + gültige Rotationstrace` erzeugt ein Winkelziel.
- `nearly_straight` erzeugt ein No-Rotation-/Relevanzsample, aber kein erzwungenes Winkelziel.
- `target_visible=no/unclear`, Trace-Diskontinuität oder zu geringe Winkelkonfidenz erzeugt `angle_target_valid=false`.
- Eine ungültige Winkelableitung verwirft nicht automatisch das gesamte Sample; es kann für Relevanz, Tracking oder Qualitätsköpfe dienen.
- `final_orientation_quality` bleibt separat und wird nicht in einen Gradwert umgerechnet.
- Human Labels und Derived Targets werden getrennt gespeichert.

Labelqualität:

- `silver`: Zielintervall durch Labeler, Winkel aus visueller Rotationstrace.
- `gold`: zusätzlich bekannte/markierte Winkelreferenz oder unabhängig verifizierte Rotationsmessung.
- Modelle und Evaluation berichten Gold und Silver getrennt.

Split/Leakage:

- Split nach kompletter Session/`group_id`,
- alle Clips/Varianten desselben Stamms im selben Split,
- keine Scaler-/Threshold-Bildung auf Val/Test,
- keine Frames nach dem Decision Frame im Hauptmodellinput,
- Checks auf doppelte Assethashes und Zeitüberlappung.

Akzeptanz:

- Ein markiertes Zielintervall plus synthetische bekannte Rotation ergibt den erwarteten signierten Winkel modulo 180°.
- Grenzfälle nahe ±90° sind ohne Sprung korrekt.
- Niedrige Trace-Konfidenz erzeugt kein scheinpräzises Ziel.
- Snapshot ist immutable, reproduzierbar und DVC-/Provenance-kompatibel.
- Bestehende Bildsnapshots bleiben unverändert.
```

---

# 16. Prompt 08 – ADR für allgemeine temporale Winkel- und Sequenzmodelle

```text
Lies `AGENTS.md`, `Spezifikation.md`, den Snapshot aus Prompt 07E und die tatsächliche Trainings-/Modell-/Deploymentarchitektur:

- cv-status Task-Typen,
- cv-training Catalog, Recipes, Trainer und Artefakte,
- Training Orchestrator und Worker,
- Modellkatalog und Versionierung,
- Deployment Releases,
- Inference Gateway/Triton/ONNX-Pfade,
- Runtime Model Binding und Node-Inferenz.

Noch keine Implementierung. Erstelle `docs/stem-orientation/temporal-angle-model-adr.md`.

Die ADR muss eine allgemeine Erweiterung für `temporal_sequence`-Modelle beschreiben und mindestens vergleichen:

A. Integration in vorhandenen Training-/Modell-/Deploymentpfad,
B. spezialisiertes Laden im Stamm-Node,
C. missbräuchliche Abbildung als Bildklassifikation.

C ist unzulässig. A ist bevorzugt, sofern kein belegter Blocker besteht.

Das erste Modell erhält nur eine kausale Sequenz bis zum Decision Frame. Es soll primär ausgeben:

- Bow-Relevance,
- Wahrscheinlichkeit, dass eine verwertbare Zielstellung beobachtet wurde,
- `sin(2e)` und `cos(2e)` für den signierten Korrekturwinkel modulo 180°,
- Winkelkonfidenz beziehungsweise Unsicherheit,
- optional finale Qualitätsklasse `good/acceptable/bad` als Nebenaufgabe.

Keine SPS- oder Bedienertastensignale sind Voraussetzung.

Begründe und plane:

- kleines kausales TCN als erste Baseline auf Featuresequenzen,
- GRU als Vergleich auf demselben Snapshot,
- deterministische Baseline als verpflichtender Vergleich,
- optionaler kleiner 2D-Frame-Encoder erst bei nachgewiesenem Informationsdefizit,
- kein Rohvideo-Transformer in Phase 1,
- strikte Kausalität und Decision-Deadline,
- Modellartefakt mit Featurefolge, Scaler, Winkelkonvention, Wrap-Regel, Mindestfenster und Confidence-Kalibrierung,
- getrennte Evaluation auf Gold- und Silver-Winkelzielen,
- Runtimevertrag für Vor-Einzug-Winkel und sekundäre Nachkontrolle.

Die ADR definiert einen konkreten Dateiänderungsplan und Migrationspfad. Keine neue parallele Trainings-API.

Akzeptanz:

- Reale Dateien und Symbole sind genannt.
- Die Einbindung in vorhandenen Orchestrator, Worker, Katalog und Deploymentpfad ist konkret.
- Angle- und Sequenzverträge sind allgemein wiederverwendbar.
- Noch kein Modellcode.
```

---

# 17. Prompt 09 – TCN und GRU für Vor-Einzug-Korrekturwinkel trainieren

```text
Lies `AGENTS.md`, `Spezifikation.md`, die freigegebene Temporal-Angle-ADR und den Snapshot aus Prompt 07E.

Implementiere ausschließlich die freigegebene allgemeine Temporal-Sequence-Integration.

## Datensatzbildung

- Primäres Sample ist die vollständige kausale Featuresequenz vom Beobachtungsstart bis zum Decision Frame.
- Frames nach dem Decision Frame sind als Modelleingang verboten.
- Winkelziel stammt ausschließlich aus dem versionierten Target Builder.
- `angle_target_valid=false` maskiert den Winkel-Loss, darf aber andere Köpfe trainieren.
- `nearly_straight` trainiert Bow-Relevance/No-Rotation, nicht einen zufälligen Winkel.
- `unusable` und standardmäßig `unclear` werden ausgeschlossen.
- Optionale Präfix-Augmentation ist nur zulässig, wenn das Zielintervall bereits im Präfix beobachtet wurde und der Winkel zum neuen Endframe aus derselben Rotationstrace neu berechnet wird.
- Alle Varianten derselben Session bleiben im selben Split.

Eingangsfeatures:

- normiertes Residual-/Mittellinienprofil,
- Breitenprofil,
- Krümmungskennwerte,
- Sichtbarkeit, Bild-/Maskenqualität,
- Track-/Positionsmerkmale,
- frameweise Rotation und kumulative Rotation samt Confidence/Masken,
- Bewegungs-/Rindenfeaturestatistik,
- optional visueller Embedding-Vektor gemäß ADR,
- keine SPS-/Taster-/Aktionsfeatures.

## Modelle

- kleines kausales TCN als Standardbaseline,
- GRU als Vergleich,
- nicht-temporale Baseline auf Decision-Frame-Features,
- deterministische Projektions-/Rotation-Baseline,
- kein Video-Transformer.

## Ausgänge und Loss

- Bow-Relevance-Kopf,
- Target-Observed-/Angle-Valid-Kopf,
- normalisierter Doppelwinkelvektor `sin(2e), cos(2e)`,
- kreisförmiger Winkel-Loss ohne Sprung an ±90°,
- Winkelunsicherheit beziehungsweise kalibrierte Confidence,
- optional ordinaler Qualitätskopf `bad < acceptable < good`.

Die Inferenz rekonstruiert:

`e = 0.5 * atan2(sin_2e, cos_2e)`

und gibt einen Wert in `[-90°, +90°)` aus.

## Evaluation

Mindestens:

- mittlerer, medianer und P90/P95 absoluter Winkelerror modulo 180°,
- Anteil innerhalb ±5°, ±10°, ±15° und ±20°,
- Drehrichtungsgenauigkeit oberhalb eines konfigurierten Deadbands,
- gefährliche Gegenrichtungsrate,
- Rate schwerer Fehler, z. B. >30°,
- No-Rotation-Fehlerrate bei nahezu geraden Stämmen,
- Confidence-Coverage-Kurven und Fehler nach Confidence-Schwelle,
- Bow-Relevance- und Target-Observed-Metriken,
- optionale Qualitätsmetriken,
- Auswertung nach Sichtbarkeit, Krümmungsstärke, Rotationstrace-Qualität, Stammgröße und Sequenzlänge,
- Gold- und Silver-Testset separat,
- TCN/GRU/deterministische/nicht-temporale Baseline auf identischem Split.

Artefakt enthält:

- Snapshot-ID,
- Feature-/Rotation-/Annotationsschema,
- Klassen- und Featureordnung,
- Scaler und Maskierungsregeln,
- Winkel- und Blickkonvention,
- Wrap-Regel,
- Deadband,
- Mindestsequenz,
- Confidence-Kalibrierung,
- Codeversion.

Akzeptanz:

- reproduzierbarer Job über vorhandene Orchestrierung,
- kein Session-Leakage,
- CPU-Smoke-Test und GPU-Training auf RTX 4500 Ada,
- Laden/Speichern reproduziert Vorhersagen innerhalb Toleranz,
- keine automatische Produktion-Promotion,
- Gold/Silver-Ergebnisse klar getrennt.
```

---

# 18. Prompt 10 – Vor-Einzug-Winkelmodell in die Runtime integrieren

```text
Lies `AGENTS.md`, `Spezifikation.md`, die freigegebene ADR und das Artefakt aus Prompt 09.

Ziel: Der stateful Runtimepfad berechnet fortlaufend, aber strikt kausal, die Hauptkorrektur und friert sie spätestens am Decision Frame ein. Die Empfehlung liegt vor, bevor das erste Vorzentrierungs-Walzenpaar den Stamm dreht.

Integriere das Modell entsprechend der ADR in `analysis.stem_temporal_orientation` oder einen allgemeinen Temporal-Inference-Node.

Eingang:

- exakt dieselben versionierten Features, Masken und Scaler wie im Training,
- nur Frames bis zum aktuellen Zeitpunkt,
- keine zukünftigen Frames,
- keine SPS-/Tasterdaten als Pflichtinput.

Ausgang `stem.orientation.v2` mindestens:

- `decision_ready`,
- `decision_frame_index`,
- `recommended_rotation_deg_signed`,
- `recommended_rotation_deg_abs`,
- `recommended_direction`,
- `rotation_viewpoint_convention`,
- `bow_relevance_probability`,
- `target_observed_probability`,
- `angle_confidence`,
- `final_quality_probabilities` optional,
- `estimate_method` (`deterministic`, `tcn`, `gru`, `fusion`),
- Modell-/Snapshot-/Feature-/Rotation-/Kalibrierungsversion,
- Reason Codes.

Verhalten:

- Vorhersage wird spätestens an der Decision Line eingefroren und mit Gültigkeitsdauer versehen.
- Modulo-180 und Deadband werden exakt wie im Training angewandt.
- Nahezu gerader Stamm beziehungsweise Winkel im Deadband ergibt `no_rotation`.
- Bei inkompatiblem Schema, zu kurzer Sequenz, nicht beobachtetem Ziel, hoher Unsicherheit, Trace-Diskontinuität oder großer Abweichung zwischen Modell und deterministischer Baseline ergibt sich `manual_required`.
- Deterministische Baseline bleibt parallel sichtbar.
- Keine Hauptentscheidung darf Frames aus der späteren Walzendrehung verwenden.

Sekundäre Nachkontrolle:

- Nach Ausführung einer Hauptdrehung darf die Kamera, sofern weiterhin sichtbar, einen separaten Residual-/Verifikationswert berechnen.
- Dieser Wert wird als `post_rotation_verification` gespeichert und überschreibt nie die ursprüngliche Vor-Einzug-Empfehlung.
- Noch keine SPS-Schreiboperation.

Akzeptanz:

- Offline-Preview und Trainer-Evaluator stimmen innerhalb Toleranz überein.
- Ergebnis steht vor der Decision Line bereit.
- Trackwechsel/Neustart setzt Zustand sicher zurück.
- Keine Zukunftsleckage.
- Bestehende Bildmodelle und Pipelines bleiben unverändert.
```

---

# 19. Prompt 11 – Feed-forward-Hauptdrehung und begrenzte Nachkorrektur, erst nach Freigabe

```text
Lies `AGENTS.md`, `Spezifikation.md`, die vollständige Shadow-Evaluation, den bestehenden Siemens-SPS-Node und die realen Maschinen-/Interlockverträge.

Dieser Auftrag darf erst nach ausdrücklicher Freigabe ausgeführt werden.

Ziel: Die vor dem ersten Walzenpaar berechnete Richtung und Gradzahl ist der primäre Feed-forward-Auftrag. Eine scorebasierte Suche mit vielen Testimpulsen ist nicht der Hauptablauf.

## Dry-Run-Vertrag

Über den bestehenden Result-/SPS-Pfad werden mindestens bereitgestellt:

- Session-/Sequenz-ID,
- `decision_ready`,
- Gültigkeitszeit/Watchdog,
- `rotate_required`,
- kanonische Richtung,
- signierter und absoluter Winkel,
- Confidence,
- Blick-/Richtungskonvention,
- Modell-/Kalibrierungsversion,
- Reason Code,
- Heartbeat.

Die konkrete Abbildung auf vorhandene SPS-Datentypen erfolgt im bestehenden `output.siemens_sps_write`-Node beziehungsweise dessen vorhandener Konfiguration. Keine neue SPS-Kommunikation in einem Parallelservice.

## Hauptablauf

1. Vision friert den Auftrag vor der Decision Line ein.
2. SPS prüft Maschinenzustand, Interlocks, Session und Gültigkeit.
3. Erst wenn das erste Walzenpaar den Stamm sicher kontrolliert, wird die vorab berechnete Hauptdrehung ausgeführt.
4. SPS begrenzt Winkel, Zeit, Geschwindigkeit und maximale Ausführung.
5. Die Kamera kann die tatsächliche Rotation weiter verfolgen, sofern die Sicht bleibt, und einen Stop-/Residualwert liefern.
6. Optional ist höchstens eine begrenzte Feinkorrektur beziehungsweise eine kleine Zahl klar begrenzter Nachkorrekturen zulässig.

Die Nachkorrektur ist nicht als offene Extremwertsuche auszuführen. Sie dient nur dazu, Ausführungsfehler, Schlupf oder eine kleine Restabweichung zu kompensieren.

Fail-safe:

- `plc_writes_allowed=false` verhindert jeden Schreibzugriff,
- keine Aktion bei niedriger Confidence, ungültiger Kalibrierung, Track-/Sessionverlust, abgelaufenem Auftrag, unklarer Richtungskonvention oder SPS-Interlock,
- nearly-straight oder Deadband → keine Drehung,
- Winkel außerhalb SPS-Grenzen → Bediener,
- Widerspruch zwischen Vision und gemessener Ausführung → sicher stoppen,
- Bediener hat jederzeit Vorrang.

Akzeptanz:

- Dry-Run zeigt den bereits vor dem Walzenkontakt verfügbaren Hauptauftrag.
- Der simulierte PLC-Ablauf nutzt ihn erst nach sicherem Walzenkontakt.
- Keine offene Testimpuls-Suche als Hauptstrategie.
- Nachkorrekturen sind begrenzt, auditierbar und deaktivierbar.
- Watchdog, Sessionhandshake und globale Schreibfreigabe sind wirksam.
```

---

# 20. Prompt 12 – Installer, Updates und Rückwärtskompatibilität

```text
Lies `AGENTS.md`, `deployment/README.md` und alle seit Prompt 06A beziehungsweise 07A geänderten Pakete, Verträge, Migrationen, Frontendteile, Label-Studio-Workflows und Temporalmodelle.

Integriere die Erweiterungen in den bestehenden Windows-/WSL2-Installer- und Offline-Updatepfad. Kein neuer Installer.

Prüfe und teste:

- neue Temporal-Sequence-Verträge und generierte Frontendschemas,
- automatische Label-Studio-Provisionierung,
- Video-/Timeline-Labeling und Local-Files-Zugriff,
- Vor-Einzug-Sample-Recorder,
- Rotationsspur- und Winkelkalibrierungsartefakte,
- Target Builder und Temporal-Snapshots,
- optionale TCN-/GRU-Artefakte,
- Runtime `stem.orientation.v2`,
- Alembic-Migrationen,
- Update von Versionen mit v1-Qualitätsresultaten,
- Rückwärtslesbarkeit oder dokumentierte Migration alter Resultate,
- Rollback und deaktivierter Zustand nach Installation.

Akzeptanz:

- Erstinstallation und Update verwenden vorhandene Export-/Apply-Skripte.
- Bestehende Bilddatasets, Label-Studio-Projekte, Pipelines und Trainingsjobs bleiben funktionsfähig.
- Ein Temporal-Dataset kann Projekt, Videoaufgabe, Timeline-Annotation und Snapshot durchlaufen.
- Persistente Kalibrierungs-, Label-, Sample- und Reviewdaten bleiben erhalten.
- Ohne konfigurierte Stammausrichtung ändert sich kein bestehender Kanal.
- Rollbackgrenzen sind dokumentiert und getestet.
```

---

# 21. Ausführungsreihenfolge ab dem aktuellen Stand

Wenn Prompts 00 bis 06 der v3-Fassung bereits ausgeführt wurden:

1. **Prompt 06A** – bisherigen Qualitäts-/Testimpulsansatz auf Vor-Einzug-Winkel korrigieren,
2. Prompt 07A – allgemeiner Temporal-Sequence-Datasettyp und Schema-Registry,
3. Prompt 07B – Label-Studio-Projekt automatisch provisionieren,
4. Prompt 07C – Vor-Einzug-Clip, Features und Rotationsspur aufzeichnen,
5. Prompt 07D – automatische Videoaufgaben nach Label Studio importieren,
6. Prompt 07E – Zielintervall materialisieren und Winkelziel ableiten,
7. Prompt 08 – Temporal-Angle-Modell-ADR,
8. Prompt 09 – TCN/GRU für signierten Korrekturwinkel,
9. Prompt 10 – Vor-Einzug-Winkelmodell im Shadow-Runtimepfad,
10. Prompt 11 – Feed-forward-Hauptdrehung plus begrenzte Nachkontrolle erst nach Freigabe,
11. Prompt 12 – Installer-/Updatehärtung.

Für einen neuen Repositorylauf gelten die überarbeiteten Prompts 05 und 06 direkt; Prompt 06A wird dann übersprungen.

Die erste nutzbare Labeldatensammlung entsteht nach Prompt 07D. Das erste ableitbare Winkel-Trainingsziel entsteht nach Prompt 07E. Ein trainiertes Modell ist weder für Aufzeichnung noch Labeling Voraussetzung.
