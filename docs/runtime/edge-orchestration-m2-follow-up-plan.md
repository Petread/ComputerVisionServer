# Edge-Orchestrierung M2 – Umsetzungs-Follow-up

- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`

## Ergebnis

M2 ersetzt den flüchtigen M0-Agent-Zustand durch eine dauerhaft installierbare Edge Runtime. Ein
Jetson oder Windows-Edge-PC kann ohne Internet betrieben werden, hält das zuletzt aktivierte
Deployment bei Ausfall des Servers am Leben, startet es nach einem Agent-/Geräteneustart erneut
und puffert Ergebnisse transaktional. Der zentrale Runtime Admin Service gleicht alle aktivierten
Geräte fortlaufend ab; die Fleet-Ansicht zeigt den gemeldeten M2-Runtime-Zustand.

Die in M1 getroffene feste-IP-/VLAN-Vertrauensentscheidung bleibt unverändert. M2 führt weder
Zertifikate noch Schlüssel ein und öffnet keine ausgehende Verwaltungsverbindung vom Edge.

## Bezug zu M1 und Abgrenzung

M2 setzt die M1-Datenbank, Fleet-API, Geräte-IP/CIDR-Prüfung, Kanalplatzierung und manuellen
Reconcile-Vertrag voraus. Es implementiert die im Abschnitt „Follow-Up To M2“ genannten Punkte:

- kontinuierliche serverinitiierte Fleet-Reconciliation;
- Edge-lokales SQLite-WAL für Deployments, Idempotenz, Slots, Prozesse und Result-Outbox;
- transaktionale A/B-Aktivierung und Wiederherstellung des letzten aktiven Workloads;
- interne Worker-Aufsicht mit Restart, Backoff, Crash-Loop-Zustand und Heartbeats;
- Artefakt-, Outbox- und freie-Datenträger-Quoten;
- reale Datei-, USB-, RTSP- und GStreamer-Quellen über das Plattform-OpenCV;
- vollständig offline ausführbare Installationspakete für Jetson/Linux und Windows 11;
- M2-Runtime-Diagnose in API und Fleet-Detailansicht.

M2 erstellt noch keine zentral versionierten Workload-Releases, baut keine TensorRT Engines und
führt keine Golden-Input-Kompatibilitätsfreigabe durch. Diese Release-/Admission-Funktionen bleiben
M3. Deshalb bleibt der zentrale Start eines lediglich Edge-platzierten Kanals weiterhin gesperrt,
bis M3 aus Kanal und Pipeline ein unveränderliches Manifest erzeugen kann. M2 kann bereits über den
M0-Control-Vertrag bereitgestellte Manifeste robust ausführen.

## Versionierte Verträge

Der bestehende Vertrag bleibt additiv kompatibel. Neu sind:

- `EdgeRuntimeSlot` mit Slot `A|B` und Zustand `empty|prepared|active|standby|failed`;
- `EdgeRuntimeProcess` mit `starting|running|backoff|crash_loop|stopped`, Restart-Zähler,
  Heartbeat und letztem Fehler;
- `EdgeStorageQuotaStatus` für Artefakte, Outbox, freien Speicher und Pressure-Zustand;
- `EdgeRuntimeStatus` (`edge.runtime.status.v1`) einschließlich aktivem/vorherigem Slot,
  Prozessen, WAL-Modus und wiederhergestellten Deployments;
- `EdgeFleetReconcilerStatus` (`edge.fleet.reconciler.v1`) mit Zyklus- und Fehlerzählern;
- additive `runtime_slot`- und `recovery_count`-Felder im Deployment-Zustand;
- additiver Runtime-Snapshot im Health-Vertrag.

Neue HTTP-Lesepfade:

- `GET /edge/v1/runtime` am Edge Agent;
- `GET /api/v1/runtime/edge-reconciler` am zentralen Runtime Admin Service.

## Edge-lokale Persistenz

Die Datei `${YBB_EDGE_DATA_ROOT}/state/edge-agent.sqlite3` wird mit
`journal_mode=WAL`, `synchronous=FULL`, Fremdschlüsseln und Busy-Timeout geöffnet. Schema-Upgrades
laufen beim Agent-Start idempotent. Die Tabellen enthalten:

- `deployments`: Manifest, Zustand, Slot und Idempotency-IDs;
- `runtime_slots`: genau die Slots A und B einschließlich Release-Zuordnung;
- `runtime_processes`: letzter persistierter Supervisor-Zustand je Deployment;
- `results`: `(boot_id, sequence)`, vollständiges Envelope und Byte-Größe;
- `result_acks` und `result_ack_requests`: monotone Cursor und idempotente Bestätigungen;
- `metadata`: Schema-Version, aktiver und vorheriger Slot.

Blob-Inhalte verbleiben im content-addressed Dateispeicher. Uploads werden weiterhin erst nach
vollständiger SHA-256-Prüfung atomar übernommen. Deployment- und Result-Metadaten wechseln nicht
mehr verloren, wenn der Prozess zwischen zwei REST-Aufrufen beendet wird.

## A/B-Aktivierung und Recovery

1. `prepare` prüft Plattform, Node-Typen und Blob-Vollständigkeit und schreibt immer in den nicht
   aktiven Slot.
2. `commit` persistiert das neue Deployment als `running`, den bisherigen Slot als `standby` und
   beide Slot-Zeiger in einer Transaktion.
3. Erst danach startet der Runtime Supervisor den neuen Worker; der vorherige Worker wird beendet.
4. `rollback` markiert den fehlerhaften Slot und stellt den vorherigen Standby-Slot samt Worker
   wieder her.
5. Beim Agent-Start werden alle als `running` persistierten Deployments geladen, der
   `recovery_count` erhöht und deren Worker erneut gestartet.
6. Wiederholte Prepare-/Commit-/Rollback-Aufrufe liefern bei identischer Idempotency-ID denselben
   gespeicherten Zustand; eine Wiederverwendung mit anderem Inhalt wird abgelehnt.

## Worker, Quellen und Watchdog

Der interne Supervisor besitzt je aktivem Deployment einen isolierten Worker-Thread. Er schreibt
Start, Heartbeat, Backoff und Fehler in SQLite, startet nach Fehlern exponentiell verzögert neu und
wechselt nach der konfigurierten Grenze in `crash_loop`. Passive/synthetische Quellen erlauben
deterministische Tests. Produktive Quellen verwenden OpenCV `VideoCapture` und unterstützen:

- RTSP-URI;
- lokale Video-/Bilddatei;
- USB-Kameraindex;
- explizite GStreamer-Pipeline auf Jetson.

`frame_sample_every_n` begrenzt die Verarbeitung. Der M2-Quellenpfad kann sequenzierte
Frame-Telemetrie in die lokale Outbox schreiben. Modell-Pre-/Postprocessing und TensorRT-Admission
werden durch das M3-Release festgelegt.

Zusätzlich überwacht systemd beziehungsweise WinSW den Agent-Prozess. Auf Jetson prüft ein lokaler
systemd-Timer den Health-Endpunkt; nach drei Fehlern startet eine Recovery-Unit den Agent neu. Die
interne Runtime-Ausführung und der zentrale Serverausfall sind davon getrennt.

## Quoten und Fehlerverhalten

Konfigurierbar sind:

- `YBB_EDGE_ARTIFACT_QUOTA_BYTES`;
- `YBB_EDGE_OUTBOX_QUOTA_BYTES`;
- `YBB_EDGE_OUTBOX_MAX_ITEMS`;
- `YBB_EDGE_MINIMUM_DISK_FREE_BYTES`.

Vor jedem Blob-Chunk und Ergebnis wird die betroffene Grenze geprüft. Ein Verstoß liefert HTTP 507
mit reproduzierbarem Fehlercode und setzt den Health-Snapshot auf `degraded`. Bereits aktive Worker
werden nicht pauschal gestoppt. Bestätigte Resultate zählen nicht mehr zur Outbox-Quote; ihre Cursor
bleiben dauerhaft gespeichert.

## Kontinuierliche zentrale Reconciliation

Der Runtime Admin Service startet mit seinem FastAPI-Lifecycle einen `EdgeFleetReconciler`. Er lädt
alle aktivierten Geräte aus M1, begrenzt parallele Probes und ruft ausschließlich registrierte
Adressen über deren gespeichertes CIDR auf. Jeder Zyklus erzeugt persistierte, idempotente
`reconcile`-Kommandos. Erfolg aktualisiert Identity, Health einschließlich Runtime und Capability;
Fehler setzen ausschließlich die Connectivity auf offline und erhalten den letzten Runtime-Stand.

Konfiguration:

- `YBB_EDGE_RECONCILE_ENABLED` (Standard `true`);
- `YBB_EDGE_RECONCILE_INTERVAL_SECONDS` (Standard `15`);
- `YBB_EDGE_RECONCILE_MAX_PARALLEL` (Standard `4`).

Shutdown setzt ein Stop-Event und wartet auf den laufenden Zyklus. Der Diagnose-Endpunkt meldet
Laufzustand, Zykluszahl, Geräte-/Erfolgs-/Fehlerzahlen und den letzten Loop-Fehler.

## Offline-Installation

`deployment/edge` enthält Bundle-Layout, Beispielkonfiguration und Installer:

- `install-jetson-offline.sh`: Benutzer, Verzeichnisse, venv, `pip --no-index`, systemd Agent und
  Watchdog;
- `configure-ufw.sh`: Control-Port ausschließlich von der festen Server-IP;
- `Install-WindowsOffline.ps1`: offline venv/Wheelhouse, WinSW-Autostart und Windows-Firewall;
- systemd Units mit gehärtetem Dateisystemzugriff und NVMe-Statepfad.

Die Installer prüfen Pflichtdateien und optional `SHA256SUMS`. Netzwerk- und SSH-Konfiguration wird
nicht blind überschrieben; die Firewall-Hilfe wird bewusst separat nach Prüfung des
Wartungszugangs ausgeführt.

## Automatisierte Abnahme

- strikte Serialisierung der neuen Verträge und OpenAPI-Export;
- SQLite-Datei läuft tatsächlich in WAL und übersteht eine neue Agent-Instanz;
- Prepare/Commit verwendet den inaktiven Slot und hält den vorherigen als Standby;
- Rollback stellt den vorherigen Slot wieder her;
- Agent-Neustart startet das letzte laufende Deployment und erhöht den Recovery-Zähler;
- Result-Sequenzen und ACK-Idempotenz überstehen Neustarts;
- Artefakt- und Outbox-Quoten liefern deterministische 507-Fehler;
- Worker-Fehler führen über Backoff in `crash_loop`;
- automatische Reconciliation persistiert Erfolg/Offline-Fehler und bleibt nach einem
  Gerätefehler aktiv;
- Offline-Installer verwenden kein Online-Paket-Repository und enthalten die Supervisor-/Firewall-
  Bausteine;
- Backend-Tests, relevante Contract-Tests, Ruff, Frontend-Typecheck und Frontend-Tests sind grün.

## Software- und Hardware-Gate

M2 ist `software_complete`, wenn die genannten Tests grün sind, OpenAPI exportiert wurde und ein
Restart-Integrationstest Deployment plus Outbox nachweislich wiederfindet. Der Orin-Nano-HIL-Status
bleibt bis zur Gerätebereitstellung `pending_hardware`.

Das spätere M2-Hardware-Gate prüft zusätzlich: Offline-Installation auf NVMe, systemd Recovery,
RTSP/GStreamer 1080p/30, Frame-Sampling, Quoten unter realem Datenträgerdruck, 30 Minuten Last,
Stromverlust/Boot-Recovery sowie serverseitigen Netzwerkausfall bei weiterlaufendem Edge-Worker.

## Übergang zu M3

M3 baut aus veröffentlichten Pipeline-Versionen unveränderliche Workload-Releases, ordnet alle
Artefakte zu, erzeugt ONNX/TensorRT-Varianten, führt Capability-/Golden-Input-Admission aus und nutzt
anschließend genau die in M2 implementierten Transfer-, Prepare-, A/B-Commit- und Rollback-Verträge.
