# Workspace-Oberfläche als Serverupdate

Die neue Oberfläche ist im **normalen Produktionsbuild** der bestehenden Admin-Web-App
integriert. Sie verwendet die vorhandenen APIs für Kamera, Runtime, Graph, Training,
Datasets, Modelle, Aufnahmen und Edge-Verwaltung. Der lokale Demo-Einstieg bleibt separat
über `pnpm demo:admin-web` erreichbar.

## Bedienung

- Die Anwendung verwendet nun denselben Vollbildrahmen wie die Demo: standardmäßig
  eine 60-Pixel-Iconleiste mit YB-Zeichen, Modul-Topmenü und Statuszeile. Die Leiste lässt
  sich unten ausklappen; die Einstellung wird im Browser gespeichert. `Strg K` öffnet
  die Suche nach Arbeitsbereichen, Kanälen und Projekten.
- Startseite: Kanäle; Label-Manager starten in Projekte.
- Projekte: kompakte Tabelle, anschließend Dataset, Modelle, Automatisierung und
  Administration als Tabs. Vorhandene Modell-Unteransichten bleiben verfügbar.
  Die Datenbereitschaft zeigt Kennzahlen, fünf Verarbeitungsschritte und
  Klassenverteilung als flache Übersicht. Lange Begründungen, Klassen-, Modell- und
  Template-Namen umbrechen vollständig innerhalb ihrer Spalten.
- **Label Studio:** Der Link in jeder Projektzeile, im Projektkopf und im Dataset öffnet
  `/label-studio/projects/<label_studio_project_id>/data/` über den vorhandenen Proxy.
  Eine fehlende oder ungültige ID erzeugt keinen Link zur allgemeinen Projektliste.
- Kanäle: Strukturnavigation nach Bereich und Anlage, Listen- und Monitoransicht.
  Die Stammdaten werden unter System → Bereiche und Anlagen verwaltet; die Zuordnung
  erfolgt beim Anlegen oder im Konfigurationstab eines Kanals.
- Kanal: Betrieb, Pipeline, Ergebnisverlauf und Konfiguration. Die Liveansicht
  enthält einen großen Bildbereich und einen einblendbaren Inspektor. Das Overlay-Menü
  bietet Sichtbarkeit je Ebene, Solo, Alle/Keine und Deckkraft. Einpassen, Zoom,
  Verschieben, Standbild und Vollbild arbeiten auf dem vorhandenen Livebild; die
  Bildsteuerung sitzt unten links. Die Schaltflächen im Inspektor verwenden denselben
  kompakten Standard wie die Kanalübersicht.
- Aufnahmen: aufklappbarer Abschnitt im Betriebsinspektor mit Einzelbild, Serienaufnahme,
  Intervall, Vorhersagen und Zielprojekt samt direktem Label-Studio-Projektlink.
  Eine laufende Serie bleibt auch bei geschlossenem Abschnitt am Status erkennbar.
  Der Bildvergleich ist separat aufklappbar; Prüfregeln für kanonische Modellbilder
  und der vorhandene Aufnahme-/Sessionpfad bleiben erhalten.
- Ergebnisverlauf: eigener Kanal-Arbeitsbereich mit Ausführungsliste links und
  Messwerten rechts. Suche, Status, Zeitraum und Blättern verwenden die bestehende
  Ergebnis-API mit festem Kanalbezug. Die erste Seite folgt alle fünf Sekunden den
  neuesten Ergebnissen; eine angeklickte Ausführung bleibt zum Prüfen fixiert.
  „Neueste“ löst die Auswahl, „Pausiert“ unterbricht die Aktualisierung.
  Fachliche Stammauswertungen und Bewertungen bleiben verfügbar; verschachtelte
  Ausgaben und Rohdaten sind aufklappbar. Das aktuelle Live-Ergebnis bietet einen
  direkten Einstieg. Bei deaktivierter Speicherung führt ein Link zur Kanalkonfiguration.
- Pipeline: Graph als primärer Editor, Palette links, Eigenschaften rechts.
  Die dunkle Graphfläche unterstützt Verschieben, Zoom und Einpassen. Die Palette ist
  durchsuchbar; Bausteine können per Klick oder Drag-and-drop eingefügt werden.
  Komplexe Kalibrierungen, der geführte Aufbau und Version/Vorlage öffnen sich in
  Dialogen. Prüfergebnisse stehen in einer ausklappbaren unteren Zeile.
  „Version & Vorlage“ trennt Versionsstand und Änderungsnotiz vom Vorlagenwechsel
  in zwei flache Formularansichten. Speichern und Aktivieren bleiben getrennte Aktionen.
  Entwürfe bleiben beim Tabwechsel erhalten;
  beim Verlassen eines ungespeicherten Kanal-/Graph-Arbeitsbereichs wird nachgefragt.
- Edge-Details und Systemverwaltung verwenden denselben kompakten Aufbau mit
  Arbeitsbereichen und Untermenü.
- Kanalkonfiguration: seitliche Navigation für Allgemein & Platz, Quelle, Ausführung,
  Datenerfassung & Historie und Schnittstellen. Alle bisherigen Kameraparameter bleiben
  verfügbar. Speichern verwendet weiterhin den vollständigen bestehenden Kanalvertrag;
  Verwerfen stellt die gespeicherten Felder wieder her.

## Daten und Kompatibilität

Alembic-Revision `20260907_0037` ergänzt im bestehenden PostgreSQL die Tabellen
`runtime_areas`, `runtime_facilities` und die optionale Kanalspalte `facility_id`.
Sie schließt an `20260820_0036` an. Bestehende Kanäle und Ortsbezeichnungen werden
nicht umgeschrieben oder automatisch einer geratenen Struktur zugewiesen.

Anlagen mit zugeordneten Kanälen und Bereiche mit Anlagen lassen sich nicht entfernen.
Umbenennungen einer Anlage aktualisieren die kompatible Ortsbezeichnung ihrer Kanäle.
Neue Stammdaten-Endpunkte verwenden die vorhandenen Kanalberechtigungen, CSRF-Prüfung
und Auditprotokollierung im Runtime-Admin-Service.

Das Update führt wie bisher `alembic upgrade head` aus. Ein Anwendungsrollback darf
die additive Datenbankrevision behalten; **kein Alembic-Downgrade** für einen reinen
Oberflächenrollback ausführen. Persistente Daten und Kalibrierungen bleiben im
vorhandenen Datenverzeichnis.

## Export und Installation

Aus dem Repository-Stamm mit der bestehenden Builder-/Exportumgebung:

```powershell
powershell -ExecutionPolicy Bypass -File deployment/export-windows-update.ps1 -Config deployment/windows-offline.config.ps1
```

Der Export baut standardmäßig das produktive Frontend für `/frontend/`, nimmt alle
Build-Dateien und die Backendmigration in das bestehende Offline-Update auf und erzeugt
das ZIP unter `dist`. Es wird keine weitere Anwendung installiert.

Der Export prüft außerdem das Vite-Manifest samt statischen und dynamischen Imports
und CSS-Dateien. Demo-Builds sowie unvollständige Produktionsbuilds werden abgewiesen.
`dist-demo` wird nicht in das Updatearchiv aufgenommen.

Das ZIP auf den Zielserver übertragen und im vorhandenen **YBB-Deployment-UI** als
Update auswählen. Alternativ den vorhandenen `apply-windows-update.ps1` mit
`-BundlePath <Pfad-zum-ZIP>` verwenden. Das Update umfasst Frontend **und** Backend;
ein bloßes Kopieren von CSS/JavaScript reicht wegen der Stammdatenmigration nicht aus.

## Verifikation

Die Projektkorrektur `2026.09.13-workspace4` passt Datenbereitschaft und Projektmodelle
an das Workspace-Design an. Sie ändert keine Backendverträge oder Datenbanktabellen.
Die isolierte Browserprüfung enthält lange Bezeichnungen und prüft die Dataset- und
Modellansicht bei 1024, 1366 und 1670 Pixel Breite sowie die Modellauswahl.

Die Kanalüberarbeitung `2026.09.13-workspace3` verwendet ausschließlich bestehende
Backendverträge und benötigt keine zusätzliche Datenbankmigration. Lokal werden
TypeScript, ESLint, Produktionsbuild, Kanal-/Designer-/Ergebnistests und die erweiterte
isolierte Browserprüfung ausgeführt. Letztere prüft auch die Bildsteuerung unten links,
Aufnahmen im Inspektor, Version/Vorlage sowie Kanalsuche, Auswahl und Blättern im
Ergebnisverlauf. Der Prüfbericht liegt unter `.tmp/ui-workspace-validation/report.json`.

Die Layoutkorrektur `2026.09.07-workspace2` behebt die alten `align-items: start`-
und Größenregeln, die trotz Workspace-Styling Inhalte auf Eigenbreite und geringe Höhe
begrenzt hatten. Kanal-/Projektübersicht, Kanalbetrieb und Designer verwenden nun
ausdrückliche Flächen mit eigenem Scrollbereich. Diese Korrektur fügt keine weitere
Datenbankmigration hinzu und wird über denselben Offline-Updatepfad eingespielt.

`scripts/dev/check-ui-workspace.mjs` prüft den **gebauten Produktionsstand** in einem
isolierten Chrome per DevTools-Protokoll. Es startet keinen App- oder API-Server:
Jede Browseranfrage wird abgefangen, mit lokalen Build-Dateien bzw. Testantworten
beantwortet oder blockiert. Die Fixtures liegen ausschließlich im Testskript;
die Produktions-App importiert sie nicht. Ein künstliches Testbild ist entsprechend
beschriftet und dient nur der Prüfung von Bildformat und Overlay-Canvas.

Chrome mit einem separaten Profil und `--remote-debugging-port=9228` starten, danach
aus dem Repository-Stamm `node scripts/dev/check-ui-workspace.mjs` ausführen.
Voraussetzung ist der normale Produktionsbuild mit `/frontend/` als Basis.
Screenshots und Prüfberichte entstehen unter `.tmp/ui-workspace-validation/`.
Geprüft werden unter anderem 1366×768, 1670×1010 und 1920×1080, volle Inhaltsbreite,
Canvas-Höhe, Anlagenfilter, Graph-/Kanalentwürfe beim Tabwechsel, Kalibrierungsdialog,
Overlay-Sichtbarkeit und direkter Label-Studio-Projektlink.

Lokal geprüft: TypeScript, Produktions- und Demobuild, 130 relevante Frontendtests,
acht neue Backendtests einschließlich PostgreSQL-DDL der Migration sowie die
Ablehnung des Demobuilds durch den Exportvalidator.

Die bestehende Suite `test_runtime_channel_management.py` hat in dieser lokalen
Umgebung sieben Fehler bei 15 bestandenen Tests. Dieselben sieben Fehler wurden
ohne die neuen Kanalstore-Änderungen reproduziert; sie betreffen unter anderem alte
Pipeline-Namenserwartungen, Kamera-Testvoraussetzungen und Runtime-/Authentifizierungs-
erwartungen. Sie werden nicht als bestandene Updateprüfung ausgewiesen.

Auf dem Zielserver nach dem Update prüfen: Anmeldung, Laden von Projekten und Kanälen,
direkter Label-Studio-Projektsprung, bestehende und neue Ortszuordnungen, Livebild mit
passgenauen Overlays bei unterschiedlichen Kameraformaten, Graph-Kalibrierung sowie
Speichern und erneutes Öffnen. Reale Kamera-, Stream- und Ende-zu-Ende-Prüfungen wurden
auf dem Entwicklungsrechner nicht ausgeführt.
