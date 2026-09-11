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

- Dieses Repository wird auf einem Entwicklungsrechner bearbeitet; die Anwendung ist hier nicht lauffähig.
- Ausführbare Updates werden auf einen separaten Server deployt. Lokale Verifikation beschränkt sich auf statische Prüfungen sowie Unit- und Build-Tests, soweit diese ohne laufende Anwendung möglich sind; Live-, Kamera- und End-to-End-Tests erfolgen auf dem Zielserver.
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
- Keine erfundenen Gradwerte aus Bildfolgen ohne belastbare Winkelreferenz.
- Tests, Build und Schema-Generierung nach den vorhandenen Repository-Konventionen ausführen.
- Nach jedem Auftrag nur die dafür notwendigen Dateien ändern und die Aufgabe beenden.

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
