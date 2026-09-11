# Lokale Vorschau der neuen Bedienoberfläche

Die Oberfläche ist inzwischen auch in den produktiven Build integriert. Diese Seite
beschreibt weiterhin ausschließlich die isolierte Demo. Export, Migration und
Installation stehen unter [Workspace als Serverupdate](runtime/frontend-workspace-update.md).

Die Vorschau läuft innerhalb der vorhandenen Admin-Web-App als expliziter Vite-Modus
`demo`. Sie benötigt ausschließlich Node.js und die Frontend-Abhängigkeiten.
Backend, Datenbank, WSL, Kameras und der produktive Server werden nicht gestartet
oder angesprochen. Es gibt keine zweite Vite- oder Serveranwendung.

## Starten

Im Repository unter Windows:

```powershell
.\scripts\dev\start-ui-demo.ps1
```

Dann **http://127.0.0.1:4173/** öffnen. Der Prozess läuft bis Strg+C.
Ist der Port belegt, kann ein anderer verwendet werden:

```powershell
.\scripts\dev\start-ui-demo.ps1 -Port 4174
```

Alternativ mit pnpm im PATH:

```powershell
pnpm demo:admin-web
```

Fehlende Abhängigkeiten vorher über `pnpm install --frozen-lockfile` installieren.
Der Windows-Starthelfer findet Node.js auch unter `C:\Program Files\nodejs`.

## Bedienumfang

- **Kanäle:** Strukturbaum Bereich → Anlage, Suche und Zustandsfilter, Listen- und
  Monitoransicht, Mehrfachzuordnung, lokale Kanal-Erstellung und Konfiguration.
- **Kanaldetail / Betrieb:** große Canvas-Arbeitsfläche mit synthetischer Bildsequenz,
  Zoom, Verschieben, Bild einpassen, Einfrieren, Vollbild und PNG-Export. Runtime
  und Stream sind getrennt steuerbar. Es werden keine gültigen Winkel erfunden.
- **Overlay-Ebenen:** Ebenensymbol am oberen rechten Bildrand öffnet die Steuerung.
  Augensymbol je Ebene, Solo, Alle/Keine, Standardauswahl und Deckkraft. Der globale
  Sichtbarkeitsschalter behält die Einzelauswahl bei. Die Ebenenauswahl wird je Kanal
  lokal gespeichert. Bild und Overlays verwenden dieselbe Transformation.
  Der rechte Messwertebereich lässt sich an seiner linken Kante in der Breite
  verändern. Dasselbe gilt für den Node-Inspektor; die Breiten bleiben gespeichert.
- **Pipeline:** verschiebbare Nodes, Palette, rechter Inspektor, Parameter,
  Verbindungen hinzufügen/entfernen, Strukturprüfung, Demo-Test, Entwurf speichern
  und gespeicherte Version aktivieren. Ein großer Kalibrierungsdialog zeigt das
  Setzen von Referenzpunkten. „Geführter Aufbau“ ist nur bei Bedarf sichtbar.
- **Projekte & Modelle:** kompakte Projekttabelle, Dataset, Modellansichten,
  Beispielmetriken, Trainings- und Deploymentaktionen, Automatisierung und lokale
  Bereinigung. Eine Bildauswahl zählt lokale Dateien; es findet kein Upload statt.
- **Ergebnisse / Aufnahmen:** filterbare Historie, Details, Export, Einzel- und
  Intervallaufnahmen. Aktive Demo-Intervallaufnahmen laufen beim Tabwechsel weiter.
- **Edge-Geräte / System:** Gerätestatus, Konfiguration, Demo-Updates und Rollback,
  Anlagenstammdaten, Jobs, Snapshots, Modellkatalog, Vorlagen, Zugänge und Audit.
- **Szenarien:** oben zwischen Normalbetrieb, offline und gestoppter Runtime wechseln.
  Strg+K öffnet eine modulübergreifende Objektsuche.

Direkteinstiege:

```text
http://127.0.0.1:4173/#/channels
http://127.0.0.1:4173/#/channels/chn-demo-1
http://127.0.0.1:4173/#/channels/chn-demo-1?tab=Pipeline
http://127.0.0.1:4173/#/pipelines
http://127.0.0.1:4173/#/system/structure
```

Die Dummy-Oberfläche verwendet Hash-Links, damit jeder Arbeitsbereich auch auf einem
einfachen lokalen Preview-Server erreichbar ist. Produktive Routen werden nicht verändert.

## Daten und Isolation

Gespeicherte Demoänderungen liegen ausschließlich im Browserspeicher unter
`cv-ybbs-workspace-demo-v1`. „Demo zurücksetzen“ rechts oben stellt alle Beispieldaten
wieder her. Ungespeicherte Kanal- und Graphentwürfe überstehen Tabwechsel; beim
Objektwechsel erscheint Speichern/Verwerfen/Abbrechen. Ein Reload verliert verworfene
oder nicht gespeicherte Entwürfe, aber keine bereits gespeicherten Demoänderungen.

Die Demo lädt den produktiven Router, API-Client und die Anmeldung nicht. Eine
Content-Security-Policy beschränkt Ressourcen auf lokale Assets und Vite-HMR.
Produktiver Build und Demobuild haben getrennte Ausgabeordner:

```powershell
pnpm build:demo       # frontend/apps/admin-web/dist-demo
pnpm preview:demo     # fertige Demo lokal ansehen
pnpm build:admin-web  # bisheriger produktiver Build in dist
```

Der normale Build enthält keine Demo-Oberfläche. `dist-demo` gehört nicht in ein
Produktionsupdate. API-, Datenbank- und Runtimeverträge bleiben unverändert.

## Was diese Vorschau nachweist

Die Vorschau dient der lokalen Abnahme von Design, Informationsdichte, Navigation
und Interaktionen. Sie verwendet eigene lokale Darstellungsmodelle. Training,
Inferenz, Kalibrierung, Deployments, Benutzerverwaltung und Updates sind simuliert.
Die Graphprüfung prüft lediglich die Demostruktur, keine realen Node-Verträge.
Die Oberfläche ist noch nicht an die produktiven Arbeitsabläufe angebunden.

Nach der UI-Abnahme werden die Arbeitsbereiche auf die bestehenden fachlichen
Komponenten und API-Verträge übertragen. Die Bereiche-/Anlagenverwaltung braucht
dann die im Plan beschriebene additive PostgreSQL-/Alembic-Erweiterung. Diese
Backendänderung ist bewusst nicht Teil des angeforderten lokalen Dummy-Frontends.

## Prüfungen

```powershell
pnpm --filter @cv-platform/admin-web test -- src/demo/model.test.ts
pnpm --filter @cv-platform/admin-web lint
pnpm build:demo
pnpm build:admin-web
```

Visuell bei 1920 × 1080 und 1366 × 768 prüfen: Kanalübersicht, Livefläche,
Overlaysteuerung, Graph/Inspektor, Projekttabelle, Stammdaten und Dialoge. Zusätzlich
Tastatur, Browser-Zurück, ungültige Namen, leere Trefferlisten, Offline-Szenario und
Persistenz nach Neuladen prüfen. Reale Kamera-, Edge- und Ende-zu-Ende-Tests erfolgen
erst bei der späteren Integration auf dem Zielserver.

Ein automatisierter Browsercheck ohne zusätzliche npm-Abhängigkeiten liegt unter
`scripts/dev/check-ui-demo.mjs`. Er benötigt die laufende Demo und einen **isolierten**
Chrome-Testbrowser mit aktiviertem DevTools-Port 9227. Er setzt die lokalen Demo-
Beispieldaten in diesem Testbrowser zurück. Ausführen mit:

```powershell
node scripts/dev/check-ui-demo.mjs
```

Andere lokale Adressen lassen sich über `UI_DEMO_URL` und `UI_DEMO_CDP_URL` setzen.
Screenshots werden unter `.tmp/ui-demo-validation` abgelegt. Der Check prüft
Navigation, Filter, Overlays, Entwürfe, Aktivierung, Stammdaten, Bildschirmgrößen
und das Ausbleiben produktiver API-Aufrufe.
