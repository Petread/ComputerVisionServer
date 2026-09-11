# Edge-Orchestrierung M3 – Umsetzungs-Follow-up

- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`
- Vorgänger: M1 Fleet Foundations und M2 Durable Edge Runtime

## Ergebnis

M3 schließt die bisher absichtlich offene Lücke zwischen einem veröffentlichten Kanal-Pipeline-
Stand und der M2-Runtime auf einem Edge-Gerät. Der normale Start-/Restart-/Stop-Vertrag eines
Kanals funktioniert nun unabhängig vom Ausführungsort. Für `server` bleibt der zentrale Runtime-
Executor zuständig; für `edge_device` übernimmt ein eigener Deployment-Controller das Erzeugen,
Prüfen, Übertragen und Aktivieren eines unveränderlichen Workload-Releases.

Ein Release bindet den veröffentlichten Channel-Pipeline-Graphen, die konkret aufgelösten
Model-Releases, ONNX-Dateien, Label Maps, optionale INT8-Kalibrierung, Golden Inputs,
Quellenkonfiguration und den beim Build beobachteten Capability-Digest. Alle übertragenen Inhalte
sind per SHA-256 adressiert. Prepare führt auf dem Zielgerät zusätzlich die lokale TensorRT- und
Golden-Input-Admission aus. Erst danach darf M2 das Release atomar in den inaktiven A/B-Slot
schalten.

## Bezug zu M2 und Scope

M3 implementiert den im M2-Follow-up angekündigten Release-/Admission-Slice vollständig:

- deterministische, unveränderliche Workload-Releases aus veröffentlichten Channel-Pipelines;
- Wiederverwendung der bestehenden Model Registry und aktiven Deployment-Releases;
- serverseitige Capability-, Storage-, Node-Typ-, Quellenadapter- und Secret-Verfügbarkeitsprüfung;
- content-addressed Manifest-, Konfigurations-, ONNX-, Label-, Kalibrier- und Golden-Artefakte;
- Edge-lokalen ONNX-zu-TensorRT-Build mit produktiver TensorRT-Python-API auf Windows,
  produktivem `trtexec` auf Jetson oder Test-Simulator;
- Engine-Cache pro Release, Modell, Präzision, Input Shapes, Plattform, Architektur,
  TensorRT-Version und GPU;
- Golden-Input-Integritätsprüfung als lokale Freigabestufe;
- langlebigen Deployment-Controller mit Restart-Recovery und idempotenten Remote-Kommandos;
- fortsetzbaren Datei-Upload ohne vollständiges Einlesen großer Modelle in den Server-RAM;
- A/B-Commit, automatisches Rollback bei Prepare-/Commit-Fehlern und bestätigten Stop;
- persistierte Release- und Deployment-Historie sowie Sichtbarkeit in Kanal- und Fleet-Ansicht.

M4 bleibt Eigentümer der zentralen Ingestion von Edge-Resultaten und optionaler Livebilder. M3
ändert die vorhandene lokale Result-Outbox nicht und erzeugt keine ausgehende Edge-Verbindung.

## Steuerungsfluss

1. Der Operator startet oder restartet einen Edge-platzierten Kanal über denselben Channel-Endpunkt
   wie einen Server-Kanal.
2. Der zentrale Executor beansprucht ausschließlich `server`-Kommandos. Der M3-Controller
   beansprucht ausschließlich `edge_device`-Kommandos mit Datenbanksperre und ablaufender Claim-
   Lease.
3. Der Builder lädt genau die veröffentlichte, kanalgebundene Pipeline und löst logische
   Model-Referenzen über die vorhandene Model Registry auf konkrete Releases auf.
4. Alle Dateien werden in den zentralen Edge-CAS geschrieben. Aus dem kanonischen Inhalt entsteht
   ein Release-Digest und daraus eine stabile `ewrl_*`-ID. Ein identischer Build liefert dasselbe
   Release.
5. Die zentrale Admission vergleicht das Release mit dem letzten Capability-Snapshot des
   registrierten Geräts. Abgelehnte Releases werden mitsamt Prüfreport gespeichert, aber nicht
   übertragen.
6. Der Controller lädt fehlende Blobs anhand des vom Edge bestätigten Offsets hoch. Ein erneuter
   Controller-Lauf setzt denselben Transfer und dasselbe Deployment fort.
7. `prepare` prüft alle Blobs, baut beziehungsweise lädt die TensorRT Engines, prüft Golden Inputs
   und schreibt den Zustand in den inaktiven M2-Slot.
8. `commit` aktiviert den Slot atomar. Bei einem Fehler ruft der Controller idempotent `rollback`
   auf und M2 stellt den vorherigen Standby-Slot wieder her.
9. Erst nach erfolgreichem Commit wird der Kanal als `running` markiert und eine nach Gerätewechsel
   gesetzte Deployment-Revalidierung gelöscht.
10. Stop bleibt bestätigt: Erst ein erfolgreicher Edge-Stop setzt den Kanal auf `stopped`. Ein
    unerreichbares altes Ziel verhindert damit weiterhin einen unbestätigten Target-Wechsel.

## Persistenz

Migration `20260721_0029` ergänzt:

### `edge_workload_releases`

Die Tabelle enthält Release-ID und Digest, Channel-/Pipeline-/Gerätebindung, Capability-Digest,
Zielplattform, Status, Manifest, interne Artefaktquellen, Admission-Report, Gesamtgröße,
Node-Typen und Ersteller. Der Release-Digest ist eindeutig. Die API liefert keine internen
Server-Dateipfade aus.

### `edge_deployment_attempts`

Jeder Start-/Restart-Versuch bindet genau ein Control-Kommando an Release und Gerät. Persistiert
werden vorheriges Deployment, gewünschter Zustand, Controller-/Remote-Status, Transferfortschritt,
lokaler Admission-/Engine-Report, Fehler und Aktivierungs-/Endzeit. Die eindeutige Command-ID macht
einen nach Prozessabbruch erneut beanspruchten Versuch idempotent.

### `runtime_channels`

`edge_workload_release_id` und `edge_deployment_id` zeigen den aktuell bestätigten Edge-Stand. Der
Read-Vertrag ergänzt Deployment- und Admission-Status, ohne die einheitliche Kanalübersicht in
Server- und Edge-Listen aufzuteilen.

## Release-Inhalt und Geheimnisse

Der veröffentlichte Pipeline-Graph und die Runtime-Konfiguration werden als JSON-Artefakte
gespeichert. Modellknoten enthalten die konkret aufgelöste Model-Version und das aktive
Deployment-Release; das ONNX-Artefakt stammt aus dem bestehenden Export-/Serving-Paket. Für INT8
ist ein vorhandener Kalibrier-Cache Pflicht. Ohne Cache wird der Build reproduzierbar abgelehnt.

Quellen-Credentials sind bewusst kein Bestandteil des zentral lesbaren Release-Manifests und
werden nicht in der Release-API ausgegeben. Der Builder prüft, dass referenzierte Secret-Dateien
lesbar sind. Erst der Controller materialisiert sie in das unmittelbar an das Ziel gesendete
Deployment-Manifest. Der Agent persistiert dieses Manifest in seinem lokal geschützten M2-State.
Aufgrund der festgelegten Architektur erfolgt diese Übertragung unverschlüsselt im isolierten VLAN;
VLAN, ACL, Edge-Firewall und eingeschränkte Dateirechte sind daher verpflichtende Schutzgrenzen.

## TensorRT- und Golden-Admission

Der Edge Agent besitzt einen `EdgeEngineManager`. Auf Windows verwendet er produktiv die
TensorRT-Python-Builder-API; auf Jetson ruft er `trtexec` ohne Shell und mit festem Argument-Array
auf. Build-Timeout, optionaler Binary-Pfad und Modus sind konfigurierbar:

- `YBB_EDGE_ENGINE_BUILDER_MODE=tensorrt_python|trtexec|simulated`;
- `YBB_EDGE_TRTEXEC_PATH`;
- `YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS`.

`simulated` ist ausschließlich für Softwaretests vorgesehen. Das Windows-Offline-Geräteprofil
verwendet `tensorrt_python`, die Jetson-Profile verwenden `trtexec`. FP32 und FP16 werden direkt
unterstützt; INT8 verlangt den im Release adressierten Kalibrier-Cache. Engines werden zunächst
temporär geschrieben und erst nach erfolgreichem Build atomar übernommen. Der Prepare-Response
enthält Buildzeit, Größe, Digest und Cache-Hit.

Golden Tests prüfen in M3 mindestens die vollständige Adressierung und Integrität der Eingabe und
eines optionalen Erwartungsartefakts. Der Vergleichstyp `artifact_integrity` ist plattformneutral.
Modell-/task-spezifische numerische Output-Vergleiche benötigen einen entsprechenden Executor im
Offline-Paket und werden nicht stillschweigend als bestanden behandelt, sondern abgelehnt.

## API und Bedienung

Neue zentrale Endpunkte:

- `GET /api/v1/runtime/edge-deployment-controller`;
- `GET|POST /api/v1/runtime/edge-workload-releases`;
- `GET /api/v1/runtime/edge-workload-releases/{workload_release_id}`;
- `GET /api/v1/runtime/edge-deployments`;
- `GET /api/v1/runtime/edge-deployments/{edge_deployment_id}`.

Die Listen können nach Kanal und Gerät gefiltert werden. Der explizite Release-Build ist für
Vorabprüfung und Diagnose gedacht; der reguläre Kanalstart baut oder verwendet das Release
automatisch. Mutationen verwenden die bestehende Fleet-Schreibberechtigung und der manuelle Build
erzeugt ein Audit-Event.

Der Edge-Vertrag ergänzt:

- `POST /edge/v1/deployments/{deployment_id}/stop`;
- lokalen Admission-Report und Engine-Builds im Deployment-State;
- `stopping|stopped` im Deployment-Zustand.

Kanal-Karten zeigen Edge-Deployment, Admission und Release-ID. Die Gerätedetailseite zeigt die
Release- und Deployment-Historie einschließlich Transferfortschritt.

## Fehler- und Recovery-Semantik

- Ein nicht erreichbares, deaktiviertes oder inkompatibles Gerät wird vor Transfer abgelehnt.
- Ein fehlendes Artefakt, falscher Hash, fehlender Source Adapter, nicht lesbares Secret,
  fehlendes TensorRT oder zu wenig Speicher erzeugt einen eindeutigen Admission-Fehler.
- Der Controller hält keine Datenbanksession während langer Netzwerk- oder TensorRT-Aufrufe.
- Ein Verarbeitungskommando mit abgelaufener Claim-Lease kann nach Controller-Neustart erneut
  beansprucht werden.
- Blob, Prepare, Commit, Rollback und Stop verwenden aus Command-/Release-IDs abgeleitete stabile
  Idempotency-IDs.
- Ein Abbruch nach Remote-Commit, aber vor zentralem Commit, wiederholt gefahrlos denselben Ablauf.
- Ein Aktivierungsfehler führt zu automatischem Rollback. Der Kanal wird erst nach bestätigtem
  Remote-Erfolg als laufend dargestellt.
- Das laufende Edge-Deployment und seine lokale Outbox bleiben bei Serverausfall unverändert.

## Automatisierte Abnahme

- strikte Serialisierung aller M3-Verträge;
- deterministischer Release-Digest und Deduplizierung identischer Builds;
- zentrale Node-/Capability-/Storage-/TensorRT-/Source-/Secret-Admission;
- vollständiger und fortgesetzter dateibasierter CAS-Transfer;
- simulierte Engine-Erzeugung, Engine-Cache und Golden-Input-Integrität;
- Start über den normalen Channel-Vertrag und getrennte Command-Claims für Server/Edge;
- erfolgreicher Prepare-/Commit-Pfad und persistierte Kanalbindung;
- automatisches Rollback bei Aktivierungsfehler;
- bestätigter, idempotenter Stop und gesperrter Target-Wechsel vor Stop-Bestätigung;
- Migration, OpenAPI, Frontend-Schemas, Typecheck, UI-Tests und Backend-Regression.

## Software- und Hardware-Gate

M3 ist `software_complete`, wenn Migration, Verträge, Builder, Controller, Agent-Admission,
Stop/Rollback, API, UI und automatisierte Tests grün sind. Die reale TensorRT-Ausführung kann ohne
bereitgestelltes Zielgerät nicht als hardwareabgenommen markiert werden.

Der M3-HIL-Nachlauf auf einem Orin Nano muss:

1. das M2-Offline-Bundle mit produktivem `trtexec` auf NVMe installieren;
2. Capability-Digest und feste IP über das isolierte VLAN abgleichen;
3. ein echtes freigegebenes ONNX-/Golden-Set als Release übertragen;
4. FP16 und – falls freigegeben – INT8 lokal bauen und Engine-Digest/-Größe erfassen;
5. Golden Outputs mit dem task-spezifischen Runner vergleichen;
6. A/B-Aktivierung, absichtlichen Aktivierungsfehler und Rollback prüfen;
7. Controller-, Server- und Netzwerkabbruch an jeder Transfer-/Commit-Grenze testen;
8. Stop-Bestätigung und blockierten Target-Wechsel bei unerreichbarem Edge nachweisen;
9. 30 Minuten Last nach fünf Minuten Warmup sowie P50/P95/P99, FPS, Drops, CPU, RAM, GPU,
   Datenträger und Engine-Buildzeit dokumentieren.

Bis zu diesem Nachlauf bleibt `docs/runtime/edge-jetson-orin-nano-m3-hil-report.md` auf
`pending_hardware`.

## Übergang zu M4

M4 liest die bereits sequenzierte M2-Outbox ab dem zentral persistierten Boot-/Sequence-Cursor,
persistiert Ergebnisse in den bestehenden Result Service und ergänzt optional Thumbnail-/Livebild-
Transport sowie Viewer-Leases über den Media-Relay. Die in M3 gespeicherte Release-ID bleibt dabei
die Herkunftsreferenz jedes Ergebnisses.
