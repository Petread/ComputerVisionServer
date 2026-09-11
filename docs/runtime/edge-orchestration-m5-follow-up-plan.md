# Edge-Orchestrierung M5 – Umsetzungs-Follow-up

- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`
- Vorgänger: M1 Fleet Foundations, M2 Durable Runtime, M3 Workload Releases und M4 Result/Media Sync

## Ergebnis

M5 macht die technischen Edge-Funktionen aus M0–M4 als zusammenhängenden Operator-Workflow
nutzbar. Geräte- und Kanalansichten zeigen das Ausführungsziel, den Fleet-Zustand und
kanalbezogene SLO-Verletzungen. Ein Deployment wird nicht mehr nur indirekt durch einen
Kanalstart ausgelöst: Der Assistent baut zuerst ein unveränderliches Workload Release, zeigt alle
Admission- und Betriebsprüfungen und übergibt beim Bestätigen genau dieses Release an den
langlebigen Deployment Controller.

Ein laufendes A/B-Deployment behält seinen Vorgänger als `standby`. Der Operator kann einen
Rollback explizit bestätigen. Edge-Umschaltung, zentrale Deployment-Historie und Kanalzeiger
werden idempotent aktualisiert. Diagnoseberichte werden historisiert; die Fleet-Übersicht erzeugt
aus denselben Checks reproduzierbare Warnungen.

## Scope

M5 implementiert:

- Fleet-Health-Übersicht mit gesunden, beeinträchtigten und kritischen Geräten;
- Ziel-Badges für Server- und Edge-Kanäle in Kanalübersicht und Detailansicht;
- Deployment-Assistent mit Kanalwahl, TensorRT-Präzision, Preflight und explizitem Commit;
- unveränderliche Bindung eines Runtime-Kommandos an Release-ID und Release-Digest;
- erneute Prüfung von Platzierung, Pipeline, Gerät, Capabilities und Release vor dem Transfer;
- langlebige Diagnoseberichte für Konnektivität, Capability-Inventar, Speicher und Result-ACK-Lag;
- Kanaldiagnose für Runtime-Zustand und P95-Latenz;
- SLO-Ziele von 100 ms für Live-Kanäle und 500 ms für Trigger-Kanäle;
- Fleet-Alerts mit stabilen IDs und konkreten Handlungshinweisen;
- A/B-Status `standby` für das vorherige Deployment;
- manuellen, serverinitiierten und idempotenten Rollback mit zentraler Zustandsreparatur;
- Audit-Ereignisse für Deployment, Diagnose und Rollback;
- OpenAPI-, Frontend-Schema-, Client- und Operator-UI-Erweiterungen.

Nicht Teil von M5 sind Agent-/Runtime-Softwareupdates, Pilotwellen, automatische Rollout-Stopps,
ein H.264/WebRTC-Fanout oder die physische Jetson-Abnahme. Diese Punkte bleiben M6 bzw. dem
Hardware-Gate vorbehalten.

## Deployment-Workflow

1. Der Operator wählt einen bereits auf das Gerät platzierten Kanal und die gewünschte
   TensorRT-Präzision.
2. `deployments:preflight` baut oder findet ein content-addressed Workload Release.
3. Admission-Checks und M5-Checks prüfen Pipeline-/Gerätebindung, feste IP, Online-Zustand,
   Capability-Digest und konkurrierende Kanaloperationen.
4. Der Browser zeigt Artefaktanzahl, Gesamtgröße, alle Checks und Remediation-Hinweise.
5. Nur ein freigegebenes Preflight mit Release-ID kann bestätigt werden.
6. Der Server schreibt Release-ID, Release-Digest und Preflight-Zeit in das bestehende langlebige
   Runtime-Control-Kommando. Ein wiederverwendeter Idempotency-Key mit anderem Payload wird
   abgelehnt.
7. Der Deployment Controller lädt ausschließlich das gepinnte Release und prüft vor jeglichem
   Transfer erneut Ziel, Pipeline, Online-Zustand, Digest und Capability-Snapshot.
8. Nach erfolgreichem Commit wird das neue Deployment `running` und sein direkter Vorgänger
   `standby`. Ein fehlgeschlagener Commit nutzt weiterhin den M3-Rollbackpfad.

Der Capability-Revalidierungsmarker blockiert ein Preflight nicht allein. Das ist für das erste
Deployment und nach einem Geräteaustausch notwendig: Das neue Release ist bereits an den aktuell
beobachteten Capability-Digest gebunden; erst sein erfolgreicher Commit löscht den Marker.

## Diagnose- und Alerting-Modell

Ein Diagnosebericht enthält strikt typisierte Checks mit Kategorie, Ergebnis, Beschreibung,
Remediation und strukturierten Details. Bewertet werden:

- Management-Erreichbarkeit und aktivierter Gerätestatus;
- aktueller Capability-Snapshot und Revalidierungsbedarf;
- Edge-Speicherdruck aus Runtime-Quoten;
- Differenz zwischen persistierter und am Edge bestätigter Result-Sequenz;
- gewünschter und beobachteter Kanalzustand;
- P95-Latenz aus bis zu 100 jüngsten Result-Timings;
- Live-SLO 100 ms oder Trigger-SLO 500 ms.

Ein fehlgeschlagener Check ergibt `critical`, eine reine Warnung `degraded`, andernfalls
`healthy`. Fleet-Alerts werden deterministisch aus Geräte-ID, Kanal-ID und Check-Key abgeleitet,
damit Polling keine Alert-Duplikate erzeugt. Diagnoseberichte werden persistiert; die Fleet-
Übersicht bewertet den aktuellen Datenbankzustand ohne unnötige Remote-Verbindung.

## Manueller A/B-Rollback

Rollback ist nur erlaubt, wenn:

- das angeforderte Deployment der aktive Kanalzeiger ist;
- es zentral und remote läuft;
- ein direkter Vorgänger auf demselben Gerät und Kanal als `standby` existiert;
- der Operator einen Grund und einen Idempotency-Key übermittelt.

Der Server erzeugt ein bestehendes langlebiges Fleet-Kommando vom Typ `deployment_rollback` und
ruft den Edge ausschließlich über feste IP und erlaubtes CIDR auf. Erst wenn der Edge das aktive
Deployment als `rolled_back` und den Vorgänger als `running` bestätigt, werden beide zentralen
Versuche und die Kanalzeiger in einer Transaktion aktualisiert. Ein wiederholter identischer
Request liefert dasselbe erfolgreiche Kommando. Ein verlorener Response kann deshalb ohne
doppelte Umschaltung geheilt werden.

## Persistenz

Migration `20260722_0031` ergänzt `edge_diagnostic_reports` mit:

- Geräte-ID, Gesamtstatus und Erzeugungszeit;
- optionalem Operator;
- vollständiger Check-Liste;
- kanalbezogenen Diagnosewerten;
- Indizes für Gerät/Zeit sowie Status/Zeit.

Die bestehenden Tabellen bleiben maßgeblich für Runtime-Kommandos, Fleet-Kommandos,
Deployment-Versuche, Result-Cursor und Kanäle. Das Payload-Feld der bestehenden
`runtime_control_commands` trägt die gepinnte M5-Releasebindung; es entsteht keine parallele
Deployment-Queue.

## Versionierte Endpunkte

Runtime Admin API:

- `POST /api/v1/runtime/edge-operator/deployments:preflight`;
- `POST /api/v1/runtime/edge-operator/deployments`;
- `POST /api/v1/runtime/edge-deployments/{edge_deployment_id}:rollback`;
- `POST /api/v1/runtime/edge-devices/{edge_device_id}/diagnostics`;
- `GET /api/v1/runtime/edge-devices/{edge_device_id}/diagnostics/latest`;
- `GET /api/v1/runtime/edge-fleet/health`.

Der Edge-Agent benötigt für M5 keinen neuen Endpunkt. Rollback, Deployment-Status, Health und
Capabilities verwenden die bereits versionierten M0–M3-Verträge.

## Fehler- und Recovery-Semantik

- Ein Preflight mit mindestens einem Fehler ist nicht ausführbar.
- Änderungen an Kanal, Pipeline, Release-Digest oder Capability-Digest nach dem Preflight
  stoppen den Controller vor dem Artefakttransfer.
- Ein Initialdeployment oder Geräteaustausch darf den Revalidierungsmarker durch einen exakt
  gebundenen, erfolgreichen Commit auflösen.
- Pending- und idempotente Runtime-Kommandos dürfen nicht mit abweichender Releasebindung
  wiederverwendet werden.
- Diagnosefehler beeinflussen keine laufenden Kanäle und keine Result-Synchronisation.
- Ein nicht erreichbares Gerät liefert einen sichtbaren Konnektivitätsfehler im Bericht.
- Ein Rollbackfehler lässt die zentrale aktive Deployment-Zuordnung unverändert.
- Ein erfolgreiches Remote-Rollback wird erst nach dessen Statusbestätigung zentral committed.
- Wiederholte erfolgreiche Rollbacks liefern dieselbe Kommandohistorie statt einer zweiten
  Umschaltung.

## Operator-UI

- Die Fleet-Liste zeigt aggregierte Zustände, Kanalanzahl und die wichtigsten aktiven Alerts.
- Die Gerätedetailseite enthält Deployment-Assistent, Diagnosechecks, Kanal-SLOs,
  Deployment-Historie und kontextbezogenen Rollback.
- Kanalansichten markieren das Ziel sichtbar als `Server` oder `Edge Device`.
- Destruktive Rollbacks erfordern eine Browserbestätigung und bleiben serverseitig nochmals
  vollständig validiert.

## Software-Gate

M5 ist `software_complete`, weil:

- strikte Backend- und Frontend-Verträge implementiert sind;
- Preflight und Controller dasselbe unveränderliche Release verwenden;
- Digest-/Capability-/Placement-Drift vor Transfer fehlschlägt;
- der vorherige A/B-Slot zentral als `standby` erhalten bleibt;
- manueller Rollback einschließlich identischem Retry getestet ist;
- Diagnose, P95-SLO, Result-Lag, Persistenz und Fleet-Alerts getestet sind;
- Migration, API, Client, Operator-UI und Ziel-Badges vorhanden sind;
- OpenAPI und relevante M0–M5-Regressionen erfolgreich laufen.

## Hardware-Gate

Der Orin-Nano-Nachlauf prüft:

1. Preflight gegen reale JetPack-/CUDA-/TensorRT-Capabilities;
2. Deployment eines realen ONNX-Modells und lokalen TensorRT-Build;
3. Drift-Abbruch nach Capability- oder Pipelinewechsel;
4. A/B-Slotwechsel und manuellen Rollback unter realer Last;
5. Weiterlauf des Kanals bei Server-/VLAN-Ausfall;
6. Diagnose von Netzwerk-, Speicher- und Result-ACK-Störungen;
7. P50/P95/P99 sowie Live-/Trigger-SLOs mit Golden Inputs;
8. Audit- und Fleet-Alert-Nachweis;
9. fünf Minuten Warmup plus 30 Minuten 1080p/30-Last.

Bis diese Evidenz vorliegt, bleibt
`docs/runtime/edge-jetson-orin-nano-m5-hil-report.md` auf `pending_hardware`.

## Übergang zu M6

M6 baut auf den langlebigen Fleet-Kommandos, A/B-Slots, Diagnosechecks und Fleet-Alerts auf. Es
ergänzt signierte Offline-Updatebundles für Agent/Runtime, Pilotwellen, automatische Rollout-
Stopps, Recovery-Drills und den Produktionspiloten. Die Identität bleibt feste IPv4 im isolierten
VLAN; Updatepakete werden in diesem Vertrauensmodell serverinitiiert übertragen und lokal vor
Aktivierung inhaltlich geprüft.
