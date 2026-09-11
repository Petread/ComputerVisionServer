# SPEC GAPS – Edge Orchestration M0–M5

- Stand: 2026-07-22
- Betrachteter Umfang: M0, M1, M2, M3, M4, M5, E1 und E2
- Softwarestatus der bisherigen Milestones: `software_complete` innerhalb des jeweils definierten Slices
- Gesamtstatus für einen produktiven Anlagenbetrieb: `not_production_complete`

## Zweck und Bewertungsmaßstab

Diese Datei dokumentiert Punkte, die nach M0–M5 noch nicht, nur teilweise oder ausschließlich im
Simulator umgesetzt beziehungsweise noch nicht auf realer Hardware abgenommen wurden.

`software_complete` in den Milestone-Follow-ups bedeutet, dass der dort definierte Vertrags-,
Persistenz-, Simulator- oder UI-Slice implementiert und automatisiert getestet ist. Daraus folgt
nicht automatisch, dass eine reale Kamera-Pipeline mit TensorRT, SPS-Ausgabe, Offline-
Provisionierung und Produktionslast vollständig funktioniert.

Die Gap-Status bedeuten:

- `open_software`: erforderliche produktive Software fehlt;
- `partial`: Vertrag oder Teilfunktion existiert, die produktive Ausführung ist unvollständig;
- `pending_hardware`: Softwarepfad existiert, aber die verbindliche HIL-Evidenz fehlt;
- `deferred_m6`: bewusst in M6 oder spätere Hardening-Arbeit verschoben;
- `accepted_risk`: bewusste Architekturentscheidung, die betrieblich abgesichert werden muss.

Prioritäten:

- `P0`: blockiert den produktiven Einsatz als eigenständige Edge-Inferenzruntime;
- `P1`: spätestens vor einem Produktionspiloten erforderlich;
- `P2`: wichtig für Skalierung, Komfort oder langfristigen Betrieb, aber kein erster Pilot-Blocker.

## Management Summary

Die Management-, Deployment-, Result-Replay- und Operator-Verträge sind weitgehend vorhanden.
E1 hat den softwareseitigen Edge-Datenpfad ergänzt: Der Agent führt den übertragenen Graphen aus,
lädt TensorRT-Pläne, verarbeitet Task-Outputs, erzeugt Result-Envelopes und Overlays und bietet
eine begrenzte Trigger-API auf 9444. Die Ausführung ist automatisiert getestet, aber noch nicht
mit realem TensorRT-Modell, Kamera und SPS auf Zielhardware abgenommen.

E2 ergänzt einen reproduzierbaren Offline-Bundle-Builder mit nativer Windows-GUI,
Pflichtchecksummen, vorbereitete beziehungsweise erzwingbare Releasesignaturen, Golden-Image-
Profile, Basisprovisionierung, reale Capability-Probes und freigegebene Edge-Kameraadapter.
Diese Pfade sind softwareseitig abgeschlossen; die konkrete Hardware-/Treiber-/Kameramatrix ist
noch HIL-abzunehmen.

Die fünf wichtigsten offenen Blöcke sind:

1. physische E1-HIL-Abnahme mit TensorRT, Kamera, Overlay und SPS-Trigger;
2. physische HIL-Freigabe der implementierten Kameraadapter und optional sichere SPS-Schreib-Connectoren;
3. produktiv signierte Referenzbundles aus realen Plattform-Wheelhouses;
4. Plattformnachweise für Orin Nano, AGX Orin und Windows 11;
5. produktive Telemetrie, konfigurierbare SLOs und ein persistenter Alert-Lifecycle.

Insgesamt sind 32 Gaps erfasst. Nach E2 sind keine Einträge mehr `open_software`: 11 sind
`partial`, 18 `pending_hardware` und 3 `deferred_m6`.

## Konsolidierte Gap-Liste

| ID | Milestone | Status | Priorität | Gap | Abschlusskriterium |
| --- | --- | --- | --- | --- | --- |
| M0-G01 | M0 | `pending_hardware` | P0 | Alle Orin-Nano-HIL-Messwerte stehen noch auf `pending`. | M0-HIL-Bericht vollständig mit Logs, Messwerten und Review auf `accepted`. |
| M0-G02 | M0 | `partial` | P1 | Der HIL-Harness inventarisiert und validiert eine extern erzeugte Metrics-Datei, führt Capture, TensorRT-Build, Golden Test und Lasttest aber nicht selbst aus. Sein Storage-Probe nutzt außerdem `/srv/ybb-edge`, während der Installer `/var/lib/ybb-edge` verwendet. | Ein reproduzierbarer Runner erzeugt Messwerte, Rohlogs und Report ohne manuell vorbereitete Metrics-Datei und misst den konfigurierten State-Pfad. |
| M0-G03 | M0/E1 | `pending_hardware` | P0 | Plant-API, Dual-Listener, Trigger, Status, Idempotenz und Grenzen sind implementiert; der SPS-/VLAN-Nachweis fehlt. | Trigger von erlaubter SPS-IP und Blockade fremder IP auf Hardware nachweisen. |
| M0-G04 | M0 | `partial` | P2 | Edge-Events sind in AsyncAPI beschrieben, im M0–M5-Lauf existieren aber keine Producer für `edge.device.state-changed`, `edge.deployment.state-changed` und `edge.result.received`. | Ereignisse werden nach erfolgreicher Persistenz über die bestehende Event-Infrastruktur veröffentlicht und idempotent getestet. |
| M1-G01 | M1 | `partial` | P2 | Capability-Snapshots werden historisch gespeichert, API und UI zeigen jedoch nur den letzten Snapshot. | Historienendpunkt und Diff-Ansicht für Boot-, Runtime- und Capability-Wechsel. |
| M1-G02 | M1 | `pending_hardware` | P1 | Geräteaustausch unter derselben IP ist nur in Store-/Simulator-Tests geprüft. | Physischer Austausch oder äquivalenter HIL-Test beweist Generationserhöhung, Revalidierung und blockierten Drift-Deploy. |
| M2-G01 | M2/E2 | `pending_hardware` | P0 | Reproduzierbarer Builder, native Windows-GUI, CI-Gate, Plattform-Wheelprüfung, Python/WinSW, Lock, Manifest und Pflichtchecksummen sind umgesetzt. Ein produktiv signiertes Referenzbundle je Plattform ist noch nicht freigegeben. | Je Zielplattform ein CI-Release mit realem Wheelhouse offline installieren, Hashreproduktion und Signatur nachweisen. |
| M2-G02 | M2/E2 | `pending_hardware` | P0 | Golden-Image-Manifeste und sichere Post-Flash-Provisioner konfigurieren NVMe-Mount, Benutzer, Zeit und statische IP. Flash/Imaging und physische Netzwerkumschaltung sind noch nicht abgenommen. | Golden Image je Plattform anwenden, Provisioner an lokaler Konsole ausführen und Reimage-Rollback dokumentieren. |
| M2-G03 | M2/E2 | `pending_hardware` | P1 | Der Agent probt reale OS-, L4T/JetPack-, CUDA-, TensorRT-, DeepStream-, GPU-, Treiber-, RAM-, Storage- und Kamera-Werte mit Warnungen. Die Zielhardware-Evidenz fehlt. | Capability-JSON je Plattform gegen unabhängiges Systeminventar prüfen und Driftfall nachweisen. |
| M2-G04 | M2/E2 | `pending_hardware` | P0 | Eigene Adapter für OpenCV, GStreamer, Aravis GigE, Basler Pylon und FLIR Spinnaker sowie lokale Allowlist-Admission sind implementiert. Reconnect erfolgt über Supervisor-Backoff. Echte SDK-/Kamera-HIL fehlt. | Trigger, Pixelkonvertierung, Exposure, Reconnect und 1080p/30 gemäß E2-HIL-Matrix je freigegebener Kombination abnehmen. |
| M2-G05 | M2 | `partial` | P1 | Kanal-Worker sind Threads im Agentprozess, keine isolierten Prozesse oder Container. Ein nativer Bibliotheksabsturz kann den gesamten Agenten und alle Kanäle treffen. | Pro-Deployment-Prozessisolation, Heartbeat, begrenzte Ressourcen und kontrollierter Restart ohne Beeinflussung anderer Kanäle. |
| M2-G06 | M2 | `partial` | P1 | Runtime-Health enthält Slots, Prozesse, Quoten und freien Speicher, aber keine echten CPU-, RAM-, GPU-, Temperatur-, FPS- oder Drop-Metriken. | Periodische, plattformspezifische Telemetrie mit Zeitreihen und Grenzwerten. |
| M2-G07 | M2 | `pending_hardware` | P0 | Offline-Installation, systemd/WinSW, Stromverlust, Watchdog, Quoten und 30-Minuten-Last sind nicht physisch abgenommen. | Alle Tabellenzeilen des M2-HIL-Berichts sind befüllt und akzeptiert. |
| M2-G08 | M2/E2 | `pending_hardware` | P1 | Eigene Profile, Golden-Image-Manifeste und gemeinsame E2-HIL-Matrix existieren für Windows 11, Orin Nano und AGX Orin. Referenzhardware ist noch nicht abgenommen. | Mindestens ein freigegebenes Hardware-/Treiberprofil je Zielplattform akzeptieren. |
| M3-G01 | M3/E1 | `pending_hardware` | P0 | Der Edge-Graph-Executor führt die freigegebene Handler-Registry topologisch aus; reale Anlagen-HIL fehlt. | Referenzgraph mit realer Kamera auf Orin Nano fachlich abnehmen. |
| M3-G02 | M3/E1 | `pending_hardware` | P0 | TensorRT-Session, CUDA-Buffer, dynamische Shapes und Warmup sind implementiert; reale GPU-Evidenz fehlt. | FP16/INT8-Engine auf Zielhardware laden und unter Last ausführen. |
| M3-G03 | M3/E1 | `pending_hardware` | P0 | Numerische und strukturierte Golden-Output-Vergleiche sind implementiert; Referenzartefakte müssen auf Hardware bestätigt werden. | Golden Set auf Zielhardware innerhalb der freigegebenen Toleranzen bestehen. |
| M3-G04 | M3/E1/E2 | `open_software` | P1 | `runtime_plugin` ist als Artefaktart definiert, wird aber nicht allgemein gebaut, paketiert, geladen oder ausgeführt. Der Siemens-SPS-Schreibausgang ist inzwischen als fest registrierter Edge-Handler inklusive Offline-Abhängigkeiten und Sicherheits-Gate umgesetzt; offen bleibt nur das generische Plugin-/Connector-System. | Versionierter Plugin-Katalog, ABI/API-Kompatibilitätsprüfung, Offline-Paketierung und allgemeine Executor-Integration. |
| M3-G05 | M3/E1 | `pending_hardware` | P1 | Capabilities werden mit der tatsächlich geladenen Handler-Registry geschnitten; der Start-Smoke-Test auf Zielhardware fehlt. | Jede gemeldete Node-Art auf Zielhardware laden und mindestens einmal ausführen. |
| M3-G06 | M3 | `pending_hardware` | P0 | Reale FP16-/INT8-Builds, Engine-Caching, Golden Outputs, Resume und A/B-Rollback sind nicht auf Jetson abgenommen. | M3-HIL-Bericht vollständig, einschließlich realem Modell und repräsentativer Kamera. |
| M3-G07 | M3/E2 | `partial` | P1 | `SHA256SUMS` ist verpflichtend; RSA-SHA-256-Signaturen können erzeugt und mit Policy `required` erzwungen werden. Schlüsselrotation, Widerruf und Recovery-Drill sind noch nicht definiert. | Zentrale Key-Policy mit Rotation, Dual-Trust-Übergang, Widerruf und Recovery-Test freigeben. |
| M4-G01 | M4/E1 | `pending_hardware` | P0 | Produktive Result-Payloads mit Modell-/Release-Provenienz werden erzeugt und synchronisiert; Hardware-Replay fehlt. | Reale Inferenzresultate unter Netzverlust exakt einmal zentral persistieren. |
| M4-G02 | M4/E1 | `pending_hardware` | P1 | Overlay-Primitiven werden lease-gesteuert übertragen und optional in JPEG gerendert; Geometrie-HIL fehlt. | Overlay gegen Golden-Bild auf Zielkamera prüfen. |
| M4-G03 | M4 | `deferred_m6` | P2 | Livebildtransport ist JPEG/MJPEG-basiert. Hardwarebeschleunigter H.264-/WebRTC-Fanout fehlt. | Bedarfsgesteuerter Hardwareencoder und zentraler Browser-Fanout hinter dem bestehenden Stream-Vertrag. |
| M4-G04 | M4 | `partial` | P2 | Bestätigte Resultzeilen werden kompaktierbar, ACK-Wasserzeichen alter Boot-IDs besitzen aber keine dokumentierte Aufbewahrungs-/Pruning-Policy. | Sichere Retention für vollständig bestätigte Boot-Domänen mit Replay- und Audit-Garantie. |
| M4-G05 | M4 | `pending_hardware` | P0 | Mehrere Boot-IDs, realer Offline-Backlog, Bandbreite, Preview-Leases und Ressourcenmehrlast sind nicht auf Hardware gemessen. | M4-HIL-Bericht vollständig und akzeptiert. |
| M5-G01 | M5 | `partial` | P1 | Diagnosen bewerten Datenbankzustand, Speicherpressure, ACK-Lag und Result-Latenzen, aber keine reale Edge-Auslastung, Temperatur, Source-FPS, Processed-FPS oder Drop-FPS. | Diagnosebericht enthält frische Telemetrie samt Alter, Grenzwerten und Handlungsempfehlungen. |
| M5-G02 | M5 | `partial` | P1 | Die P95-Ziele 100 ms/500 ms und das 100-Result-Fenster sind fest im Code hinterlegt. | Versionierte globale und kanalbezogene SLO-Policy mit Zeitfenster, Mindeststichprobe und P50/P95/P99. |
| M5-G03 | M5 | `partial` | P1 | Fleet-Alerts sind eine beim Lesen berechnete Projektion. Es gibt keinen persistenten Alert-Lifecycle mit `open`, `acknowledged`, `resolved`, Dedupe-Zeitfenster oder Benachrichtigung. | Hintergrundauswertung, persistente Zustände, Quittierung, Auflösung und optionaler Notification-Connector. |
| M5-G04 | M5 | `pending_hardware` | P0 | Preflight, Drift-Abbruch, A/B-Standby und manueller Rollback sind nur im Simulator/DB-Test abgenommen. | M5-HIL-Bericht vollständig, einschließlich Request-Verlust und VLAN-Unterbrechung während Rollback. |
| M5-G05 | M5 | `deferred_m6` | P1 | Fleet-weite oder gestaffelte Rollouts, Pilotgruppen und automatischer Abbruch bei Fehler-/SLO-Grenzen fehlen. | Rollout-Plan mit Wellen, Gates, Pause/Resume/Abort und automatischem Rollback. |
| M5-G06 | M5 | `deferred_m6` | P1 | Agent- und Runtime-Versionen können nicht zentral als A/B-Updatepaket ausgerollt werden. | Signiertes Offline-Updatebundle, A/B-Systemslot, Kompatibilitätsprüfung und Recovery-Drill. |

## Detailbewertung nach Milestone

### M0 – Architektur, Verträge und Simulator

M0 hat die festen IP-Verträge, CIDR-Restriktion, Artefaktübertragung, Deployment-Kommandos und
Result-Replay als ausführbaren Simulator-Slice geliefert. Offen sind die physische Beweisführung
und zwei funktionale Grenzen:

- `scripts/benchmark/edge_hil_probe.py` ist ein Inventar-/Validierungswerkzeug. Es steuert keine
  Kamera, führt keinen Lasttest aus und erzeugt die geforderten Runtime-Metriken nicht selbst.
  Zusätzlich prüft es standardmäßig `/srv/ybb-edge`; der M2-Installer verwendet
  `/var/lib/ybb-edge`, wodurch aktuell gegebenenfalls nur das Root-Dateisystem gemessen wird.
- E1 ergänzt den separaten Plant-Listener 9444, IP-Allowlist, Grenzen und durable Trigger-
  Idempotenz. Offen ist die physische SPS-/Firewall-Abnahme.
- Die AsyncAPI-Kanäle sind Verträge; ein tatsächlicher Eventproducer ist im Edge-Slice nicht
  angeschlossen.

Referenz: `docs/runtime/edge-jetson-orin-nano-hil-report.md`.

### M1 – Fleet Foundations

Registrierung, feste-IP-Identität, Reconciliation, Command-Idempotenz, Placement und Fleet-UI sind
vorhanden. Noch nicht geschlossen sind:

- eine bedienbare Capability-Historie und der Vergleich zwischen Generationen;
- der physische Geräteaustausch unter gleicher IP;
- der Nachweis, dass VLAN-/Firewallregeln Zugriffe von anderen Clients tatsächlich blockieren.

Der fehlende Hintergrund-Reconciler aus M1 wurde in M2 umgesetzt und ist deshalb kein offener
M1-Gap mehr. Ziel-Badges, Diagnose und Operator-Workflow wurden in M5 ergänzt.

### M2 – Durable Edge Runtime und Provisionierung

SQLite-WAL, A/B-State, Supervisor-Zustände, Quoten, Result-Outbox, Installer und Watchdog sind
implementiert. E2 ergänzt reproduzierbare Bundle-Builds, native GUI, Pflichtintegrität,
Golden-Image-/Provisioningprofile, reale Inventarisierung und dedizierte Kameraadapter.
Produktionsrelevant offen bleiben:

- ein real gebautes, verpflichtend signiertes Referenzbundle je Plattform;
- physische Golden-Image-, Netzwerk-, Service- und Recovery-Abnahme;
- HIL jeder freigegebenen Kamera-/SDK-/Treiberkombination;
- Prozessisolation der Kanalworker und produktive Ressourcen-/FPS-/Temperaturtelemetrie.

Referenz: `docs/runtime/edge-jetson-orin-nano-m2-hil-report.md`.

### M3 – Workload Releases und lokale Admission

Release-Builder, content-addressed Artefakte, Transfer, lokaler `trtexec`-Build, A/B-Commit und
Rollbacksteuerung sind vorhanden. E1 ergänzt Graphausführung, TensorRT-Sessions, Warmup,
task-spezifische Pre-/Postprozessoren und echte Golden-Output-Vergleiche. Offen bleiben:

- die physische TensorRT-/Kamera-/Golden-Abnahme;
- fremde `runtime_plugin`-Artefakte und nicht freigegebene Server-Nodes;
- physische Kamera-SDK-Abnahme und SPS-Schreib-Connectoren.

Referenzen: `docs/runtime/edge-orchestration-e1-follow-up-plan.md` und
`docs/runtime/edge-jetson-orin-nano-e1-hil-report.md`.

### M4 – Result- und Media-Integration

Result-Pulling, Cursor, ACK, Deduplizierung, Boot-Domänen und lease-gesteuerte JPEG-Previews sind
implementiert. E1 liefert nun produktive CV-Payloads und optional gerenderte Overlays. Offen sind:

- reale Result-Replay- und Overlay-Geometrie-HIL;
- der JPEG/Base64-Pfad ist für Diagnosebilder geeignet, aber nicht als effizienter Multi-Viewer-
  Videotransport ausgelegt;
- reale Backlogs, Bandbreite, Bootwechsel und Lastverhalten sind nicht HIL-abgenommen;
- für langfristig gespeicherte ACK-Wasserzeichen fehlt eine explizite Retention.

Referenz: `docs/runtime/edge-jetson-orin-nano-m4-hil-report.md`.

### M5 – Operator Workflows

Deployment-Preflight, gepinnte Releases, Drift-Prüfung, Diagnoseberichte, Fleet-Health, Ziel-Badges
und manueller A/B-Rollback sind umgesetzt. Für einen Produktionsbetrieb fehlen:

- echte Ressourcen-/FPS-/Temperaturtelemetrie in den Diagnosen;
- konfigurierbare SLO-Policies und belastbare Zeitfenster;
- persistente Alertzustände und Benachrichtigungen;
- physische Rollback-/Netzverlustabnahme;
- Fleet-Rolloutwellen und Agent-/Runtime-Updates, die gemäß Roadmap M6 gehören.

Referenz: `docs/runtime/edge-jetson-orin-nano-m5-hil-report.md`.

## Plattformübergreifende offene Punkte

### Hardware- und Plattformmatrix

Die vorhandenen HIL-Vorlagen fokussieren den Jetson Orin Nano. Für folgende Ziele fehlen eigene
verbindliche Profile:

- Jetson AGX Orin: JetPack-/L4T-Version, Leistungsmodus, Speicherprofil und Thermik;
- Windows 11 mit NVIDIA-GPU: Treiber, CUDA/TensorRT, Python/Wheel-Kompatibilität, WinSW-Recovery;
- Kamera-/Treiberkombinationen je Plattform;
- maximale Zahl paralleler Kanäle und Modelle je Gerätetyp.

### Security und Trust Boundary

Folgende Punkte sind bewusste Risiken des gewählten Vertrauensmodells und keine versehentlich
fehlenden Features:

- Geräteidentität ausschließlich über feste IPv4-Adresse;
- HTTP ohne Geräte-Zertifikat, API-Key oder Transportverschlüsselung;
- Secrets und Workload-Konfiguration können im Management-VLAN unverschlüsselt übertragen werden;
- Geräteaustausch unter derselben IP besitzt keine Hardware-Attestation.

Diese Risiken sind nur akzeptabel, wenn VLAN-Isolation, Switch-ACL, Edge-Firewall, physischer
Zugriffsschutz und Serverhärtung nachweisbar umgesetzt und regelmäßig geprüft werden. Das offene
HIL-Gate muss diese Evidenz enthalten. Signierte Offline-Update-/Releasepakete bleiben trotzdem
sinnvoll, weil VLAN-Isolation keine Supply-Chain-Integrität ersetzt.

### Produktionsbetrieb und Skalierung

Noch nicht nachgewiesen sind:

- der in M6 vorgesehene Produktionspilot bis zur vereinbarten 50-Geräte-/acht-Kanal-Grenze;
- Verhalten bei gleichzeitigem Reconcile, Deployment, Result-Replay und Preview aller Geräte;
- PostgreSQL-Locking und Worker-Failover unter mehreren Runtime-Admin-Instanzen;
- Backup/Restore von Fleet-, Release-, Deployment-, Cursor- und Diagnosezustand;
- Zeitabgleich/NTP-Ausfall und dessen Einfluss auf SLO-, Result- und Audit-Zeitstempel;
- längere Soak-Tests über 30 Minuten hinaus sowie thermisches Throttling;
- Kapazitätsplanung für Artefakte, Engines, Outbox, Result-History und Previewbandbreite.

## Bewusst nicht als Gap bewertet

Die folgenden Punkte entsprechen dem vereinbarten Design und sollten nicht versehentlich als
Fehler behandelt werden:

- keine ausgehende Verwaltungsverbindung vom Edge;
- kein Cloud-Zwang und kein Internetzugriff für Installation oder Betrieb;
- keine freie URL-Eingabe für Edge-Control-Ziele;
- getrennte Anzeige von Connectivity und zuletzt gemeldetem Runtime-Zustand;
- lokales Weiterlaufen bei Serverausfall;
- Result-ACK erst nach zentralem Commit;
- kein permanenter Livebild-Uplink ohne Viewer-Lease;
- keine automatische SPS-Schreiboperation im bisher abgenommenen Simulator-Scope.

Lokale Trigger über Port 9444 und der fest registrierte Siemens-SPS-Schreibausgang sind vorhanden. Die physische SPS-/VLAN-Abnahme bleibt ein
funktionaler Gap, sobald ein konkreter Anlagenprozess sie benötigt.

## Empfohlene Schließungsreihenfolge

### Paket E1 – Productive Edge Execution (`P0`, `software_complete`)

1. Edge-Graph-Executor und Handler-Registry implementieren.
2. TensorRT-Engine laden und echte Inferenz ausführen.
3. Task-spezifisches Pre-/Postprocessing und Golden-Output-Vergleich implementieren.
4. Reale Result-Envelopes und optionale Overlays erzeugen.
5. SPS-/Trigger-Service auf Port 9444 mit sicheren Grenzen ergänzen.

Softwareumsetzung: abgeschlossen. Hardware-Gate: `pending_hardware`.

### Paket E2 – Provisioning und Plattformen (`P0/P1`)

1. Reproduzierbaren Offline-Bundle-Builder in CI erstellen.
2. Checksummen verpflichtend und Release-Signaturen vorbereiten.
3. Golden-Image-/Basisprovisionierung für Orin Nano, AGX Orin und Windows definieren.
4. Reale Capability-Inventarisierung implementieren.
5. Freigegebene Kameraadapter je Plattform implementieren und testen.

Softwareumsetzung: abgeschlossen. Plattform-/Kamera-Hardware-Gate: `pending_hardware`.

### Paket E3 – HIL und Betriebsfreigabe (`P0`)

1. M0–M5-HIL als eine zusammenhängende Testkampagne ausführen.
2. Orin Nano zuerst vollständig abnehmen.
3. Danach AGX-Orin- und Windows-Matrix ergänzen.
4. Netzwerkverlust, Stromverlust, Rollback, Replay, SPS und 30-Minuten-Last prüfen.
5. Abweichungen als konkrete Issues mit Messwert und Owner dokumentieren.

### Paket E4 – M6 Fleet Updates und Hardening (`P1/P2`)

1. Signierte A/B-Agent-/Runtime-Updates.
2. Pilotgruppen, Wellen, automatische Gates und Rollout-Stopp.
3. Persistenter Alert-Lifecycle und Notification-Integration.
4. Skalierungs-, Failover-, Backup-/Restore- und Recovery-Drills.
5. Optional hardwarebeschleunigter H.264-/WebRTC-Fanout.

## Definition für „Edge-Orchestrierung vollständig produktionsbereit“

Die M0–M5-Gesamtlösung darf erst als produktionsbereit gelten, wenn:

- ein veröffentlichtes Server-Pipelineprojekt auf realer Edge-Hardware ohne manuelle
  Codeinstallation ausgerollt wird;
- der Edge den Graphen mit realem TensorRT-Modell und freigegebener Kamera ausführt;
- Ergebnisse, Overlays und konfigurierte lokale Ausgänge fachlich korrekt sind;
- Offline-Betrieb, Replay, A/B-Deployment und Rollback unter Netz-/Stromfehlern nachgewiesen sind;
- alle HIL-Berichte die sicherheits- und recoveryrelevanten Punkte bestanden haben;
- Orin Nano, AGX Orin und Windows nur in ausdrücklich freigegebenen Hardware-/Treiberprofilen
  eingesetzt werden;
- Telemetrie, SLOs und Alerts den Betreiber vor Ressourcen-, Kamera-, Runtime- und Syncfehlern
  zuverlässig warnen;
- Update-, Backup-, Restore- und Recovery-Verfahren dokumentiert und praktisch erprobt sind.

## E1-Statusdelta (2026-07-22)

Paket E1 ist softwareseitig als `software_complete` umgesetzt. Maßgeblich ist
`docs/runtime/edge-orchestration-e1-follow-up-plan.md`.

Softwareseitig geschlossen sind M0-G03, M3-G01, M3-G02, M3-G03, M3-G05, M4-G01 und M4-G02:
Graph-Executor, tatsächlich registrierte Handler, TensorRT-Session, Task-Pre/Postprocessing,
Golden-Output-Vergleich, echte Result-Envelopes, optionale Overlays und die begrenzte Plant-API
auf Port 9444 sind vorhanden und automatisiert getestet.

Diese Einträge wechseln bis zur physischen Abnahme in `pending_hardware`; sie sind nicht mehr
`open_software`. M3-G04 bleibt `partial`, weil E1 absichtlich keine fremden `runtime_plugin`-
Artefakte lädt und noch nicht alle Server-Nodes beziehungsweise SPS-Schreib-Connectoren auf dem
Edge freigibt. Prozessisolation, Telemetrie und sämtliche HIL-Nachweise bleiben in E3 offen. Der
Gesamtstatus bleibt daher `not_production_complete`.

## E2-Statusdelta (2026-07-22)

Paket E2 ist softwareseitig als `software_complete` umgesetzt. Maßgeblich sind
`docs/runtime/edge-orchestration-e2-follow-up-plan.md` und ADR 0008.

Softwareseitig geschlossen sind M2-G01, M2-G02, M2-G03, M2-G04 und M2-G08: Der native
Windows-Bundle-Builder, der deterministische CI-Buildkern, Pflichtchecksummen, Signaturprüfung,
Plattform-/Golden-Image-Profile, Post-Flash-Provisioner, reale Capability-Probes und dedizierte
Kameraadapter sind vorhanden und automatisiert getestet. M3-G07 wechselt von `deferred_m6` auf
`partial`, weil Signaturen erzeugt und erzwungen werden können; Key-Rotation und Recovery bleiben
offen.

Die M2-Einträge wechseln bis zur physischen Plattform-/Kameraabnahme in `pending_hardware`.
Prozessisolation und Telemetrie bleiben eigenständige Software-Gaps. Der Gesamtstatus bleibt
`not_production_complete`.
