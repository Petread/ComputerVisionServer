# Edge-Orchestrierung M4 – Umsetzungs-Follow-up

- Softwarestatus: `software_complete`
- Hardwarestatus: `pending_hardware`
- Vorgänger: M1 Fleet Foundations, M2 Durable Runtime und M3 Workload Releases

## Ergebnis

M4 schließt den Rückweg vom Edge zur zentralen Plattform. Ergebnisse aus der langlebigen
M2-Outbox werden durch den Server abgeholt, innerhalb der bestehenden Result-Domäne persistiert
und erst danach lückenlos bestätigt. Ein Geräte- oder Serverneustart erzeugt keine doppelte
fachliche Persistenz. Noch nicht bestätigte Boot-Domänen bleiben auch nach einem weiteren
Edge-Neustart auffindbar.

Optional können Kanalübersicht und Kanaldetail ein Livebild anfordern. Dafür erzeugt der Browser
eine kurze zentrale Viewer-Lease. Der Result-Synchronizer fasst aktive Leases pro Kanal zusammen,
öffnet serverinitiiert eine ebenfalls ablaufende Lease auf dem Edge und holt ein gedrosseltes JPEG
ab. Ohne Viewer wird weder Bildverkehr erzeugt noch JPEG-Encoding angefordert. Das vorhandene
Runtime-Live-View-/MJPEG-API bleibt der einzige Browser-Relay-Vertrag.

## Scope

M4 implementiert:

- Discovery aller ausstehenden Edge-Boot-/Sequenzdomänen;
- serverinitiiertes, begrenztes Pulling von Result-Batches;
- transaktionale Persistenz in `result_records`;
- eindeutige Empfangsbelege über Geräte-ID, Boot-ID und Sequenz;
- Bindungsprüfung gegen feste Management-IP, Kanal, Gerät und Workload Release;
- Wiederverwendung der konfigurierbaren Kanal-Result-Policy für Sampling, Retention und Limits;
- lückenlose Cursorfortschreibung und ACK erst nach erfolgreichem Datenbank-Commit;
- lokales Löschen bestätigter Outbox-Zeilen bei Erhalt der ACK-Idempotenzdaten;
- Synchronizer-Recovery nach Abbruch vor oder nach dem Remote-ACK;
- zentrale und Edge-lokale Viewer-Leases mit automatischem Ablauf;
- bedarfsgesteuertes Edge-JPEG, Auflösungs-/FPS-/Größenlimits und zentralen Live-View-Relay;
- M4-Status und Cursor-Sichtbarkeit in Fleet- und Kanalansichten.

Nicht Teil von M4 sind ausgehende Edge-Verbindungen, Cloud-Dienste, ein dauerhaft laufender
Video-Uplink oder eine neue Ergebnisdatenbank. Produktive H.264/WebRTC-Fanout-Optimierung kann
später hinter dem bestehenden Stream-Vertrag ergänzt werden; M4 liefert den sicheren LAN-
Previewpfad und das vorhandene MJPEG-Relay.

## Result-Synchronisationsfluss

1. Der Synchronizer lädt alle aktiv registrierten Geräte und baut den Client ausschließlich aus
   fester Management-IP, Control-Port und erlaubtem CIDR auf.
2. `GET /edge/v1/results/streams` liefert die aktuelle sowie alle lokal bekannten älteren
   Boot-Domänen. Bereits vollständig bestätigte Domänen bleiben als kompakte ACK-Wasserzeichen
   sichtbar, damit ein verlorener ACK-Response auch nach Outbox-Kompaktion heilbar ist.
3. Für jede Boot-ID liest der Server seinen persistierten Cursor und fordert ab der nächsten
   Sequenz ein größen- und anzahlbegrenztes Batch an.
4. Management-IP, Boot-ID, Sequenzlücken, Kanalzuordnung und Workload-Release-Bindung werden vor
   Persistenz geprüft.
5. Ergebnis, Empfangsbeleg und `last_persisted_sequence` werden in derselben Transaktion
   geschrieben. Der Empfangsbeleg besitzt eine eindeutige Geräte-/Boot-/Sequenz-Constraint.
6. Erst nach Commit sendet der Server ein idempotentes ACK für die höchste lückenlos persistierte
   Sequenz.
7. Nach bestätigtem Remote-ACK schreibt der Server `last_acknowledged_sequence` fort. Der Edge darf
   die bestätigten Resultzeilen löschen, behält aber den ACK-Cursor und die Idempotency-Antwort.
8. Bei Abbruch vor DB-Commit wird das Batch wiederholt. Bei Abbruch nach DB-Commit wird vom
   persistierten Cursor fortgesetzt. Bei Abbruch nach Remote-ACK bleibt die Boot-Domäne über ihr
   ACK-Wasserzeichen discoverbar und beide Seiten akzeptieren das gleiche ACK erneut.

## Abbildung in die bestehende Result-Domäne

Jedes `EdgeResultEnvelope` erzeugt genau einen bestehenden `ResultRecord`. Pipeline-Version und
Runtime-Referenz stammen aus dem zugelassenen Workload Release und dem aktiven Edge Deployment.
Der vollständige Herkunftsnachweis wird in `runtime_metadata` abgelegt:

- Edge-Geräte-ID und feste Management-IP;
- Boot-ID und Sequenz;
- Workload-Release-ID;
- ursprüngliche Edge-Correlation-ID.

Bekannte Execution-Result-Felder wie normalisierte Inferenzresultate, abgeleitete Ergebnisse,
Overlays, Timing und Model Routes werden in die vorhandenen Spalten projiziert. Unbekannte Felder
bleiben im Ergebnis-Payload erhalten. Damit funktionieren Result-History und deren bestehende API
ohne parallelen Edge-spezifischen Read Store.

Die vorhandene `RuntimeResultHistoryPolicy` des Kanals gilt unverändert auch für Edge-Ergebnisse:
`enabled` entscheidet über die fachliche History, `sample_interval_seconds` über das Sampling und
der bestehende Maintenance-Worker setzt `retention_hours` und `max_records` durch. Empfangsbeleg
und Cursor werden unabhängig davon für jedes Envelope persistiert, damit auch ein nicht für die
History ausgewähltes Ergebnis exakt einmal bestätigt werden kann. Das Sampling verwendet die
Edge-Ereigniszeit; dadurch bleibt es auch beim Replay eines längeren Offline-Zeitraums korrekt.

Inline-Bilddaten werden nicht in Result-JSON oder Resultspalten übernommen. Das Thumbnail der
Kanalübersicht kommt aus demselben kurzlebigen, lease-gesteuerten Preview wie das Kanaldetail.
Damit entstehen weder unkontrollierte Datenbank-Blobs noch ein permanenter Bild-Uplink.

## Persistenz

Migration `20260721_0030` ergänzt:

### `edge_result_receipts`

Ein unveränderlicher Empfangsbeleg pro Geräte-ID, Boot-ID und Sequenz. Gespeichert werden
Envelope-Digest, ResultRecord-ID, Kanal, Release und Ereigniszeit. Ein Replay mit identischem
Inhalt ist ein No-op; gleicher Sequenzschlüssel mit anderem Inhalt wird abgelehnt.

### `edge_viewer_leases`

Kurzlebige, authentifizierte Browserabsichten mit Kanal-/Gerätebindung, Ablaufzeit, maximaler FPS,
Breite und Overlay-Wunsch. Abgelaufene Leases werden beim Lesen atomar als `expired` markiert.
Freigegebene Leases werden nicht erneut zum Edge gespiegelt.

Die bestehenden `edge_result_cursors` bleiben Eigentümer von Persisted- und ACK-Wasserzeichen.

## Viewer-Lease und Livebildfluss

1. Eine sichtbare Edge-Kanalkarte oder Kanaldetailseite erstellt eine zentrale Lease mit 30
   Sekunden TTL und erneuert sie alle zehn Sekunden.
2. Der Synchronizer aggregiert alle aktiven Leases eines Kanals. Er verwendet die höchste
   angeforderte FPS/Breite und erneuert eine stabile Relay-Lease am Edge.
3. Der Runtime-Worker prüft vor dem JPEG-Encoding, ob eine aktive Edge-Lease existiert. Ohne Lease
   bleibt die Bildpipeline aus.
4. Das neueste JPEG wird atomar in der lokalen Edge-State-Ablage aktualisiert. Größe und
   JPEG-Qualität sind begrenzt.
5. Der Server holt nur Sequenzen neuer als das zuletzt übertragene Preview, prüft Kanal, Gerät und
   Release und schreibt sie atomar in den vorhandenen Live-View-State.
6. Browser verwenden unverändert `live-view/latest` oder das vorhandene MJPEG-Relay. Direkte
   Browserzugriffe auf Edge-IP-Adressen sind nicht erforderlich.
7. Beim Schließen der Ansicht wird die zentrale Lease freigegeben. Bei Browser-, Server- oder
   Netzwerkabbruch beendet der TTL-Ablauf die Übertragung automatisch.

## Versionierte Endpunkte

Edge Agent:

- `GET /edge/v1/results/streams`;
- bestehendes `GET /edge/v1/results` und `POST /edge/v1/results/ack`;
- `POST /edge/v1/media/viewer-leases`;
- `DELETE /edge/v1/media/viewer-leases/{lease_id}`;
- `GET /edge/v1/media/preview`.

Runtime Admin API:

- `GET /api/v1/runtime/edge-result-sync`;
- `POST /api/v1/runtime/channels/{channel_id}/edge-viewer-leases`;
- `DELETE /api/v1/runtime/channels/{channel_id}/edge-viewer-leases/{lease_id}`;
- vorhandene Result-History- und Live-View-Endpunkte.

## Fehler- und Recovery-Semantik

- Batches mit fremder IP, Boot-ID, Kanal-/Release-Bindung oder Sequenzlücke werden nicht bestätigt.
- Ein bereits belegter Sequenzschlüssel mit anderem Envelope-Digest ist ein Protokollfehler.
- Der Synchronizer hält keine Datenbanksession während Netzwerkzugriffen.
- Ein fehlerhaftes Gerät stoppt nicht die Synchronisation der restlichen Fleet.
- Mehrere Boot-Domänen werden unabhängig fortgeschrieben und bestätigt.
- Ein verlorener Response nach bereits verarbeitetem Remote-ACK wird anhand der ACK-only-
  Boot-Domäne geheilt; das zentrale Wasserzeichen kann nicht dauerhaft zurückbleiben.
- Ein nicht erreichbares Gerät behält seine lokale Outbox und laufenden Kanäle.
- Ein Preview-Fehler beeinflusst die Result-Persistenz nicht beim nächsten Zyklus; Fehler werden
  pro Gerät sichtbar.
- Remote-Leases sind stabil adressiert und laufen auch ohne explizites DELETE aus.
- Preview-Dateien werden atomar ersetzt und nur über den authentifizierten Serverendpunkt
  ausgeliefert.

## Konfiguration

Server:

- `YBB_EDGE_RESULT_SYNC_ENABLED`;
- `YBB_EDGE_RESULT_SYNC_INTERVAL_SECONDS`;
- `YBB_EDGE_RESULT_SYNC_BATCH_LIMIT`;
- `YBB_EDGE_RESULT_SYNC_MAX_BATCHES_PER_STREAM`;
- `YBB_EDGE_RESULT_SYNC_CONNECT_TIMEOUT_SECONDS`;
- `YBB_EDGE_RESULT_SYNC_READ_TIMEOUT_SECONDS`.

Edge:

- `YBB_EDGE_PREVIEW_JPEG_QUALITY`;
- `YBB_EDGE_PREVIEW_MAX_BYTES`;
- bestehende Outbox-Item-/Byte-Limits.

## Software-Gate

M4 ist `software_complete`, wenn:

- Migration und strikte Verträge vorliegen;
- mehrere Boot-Domänen entdeckt und vollständig synchronisiert werden;
- Replay keine doppelten `ResultRecord`-Einträge erzeugt;
- Result-Sampling, Retention-Markierung und Entfernung eingebetteter Bilddaten der Kanal-Policy
  entsprechen;
- Sequenzlücken und manipulierte Replays nicht bestätigt werden;
- Outbox-Zeilen erst nach ACK kompaktierbar sind;
- Viewer-Leases erneuert, freigegeben und automatisch ablaufen;
- ohne Lease kein Preview-Transfer erfolgt;
- ein Edge-Preview über die bestehende Kanal-Live-View-API sichtbar wird;
- OpenAPI, Frontend-Schemas, UI, Migration und M0–M4-Regression grün sind.

## Hardware-Gate

Der Orin-Nano-Nachlauf prüft mit echter Kamera und realer Last:

1. Ergebnisaufbau während Server-/Netzwerkausfall;
2. Replay über Neustart und mindestens zwei Boot-IDs;
3. exakt-einmalige fachliche Persistenz und lückenlose ACKs;
4. Outbox-Kompaktion und Speicherverhalten;
5. Preview-Verkehr nur mit Viewer-Lease;
6. Ablauf bei Browser-, Server- und VLAN-Unterbrechung;
7. Bandbreite, JPEG-Encode-/Relay-Latenz und CPU-/GPU-Mehrlast;
8. parallele Viewer und wirksame FPS-/Auflösungsgrenzen;
9. 30 Minuten Last nach fünf Minuten Warmup.

Bis diese Evidenz vorliegt, bleibt
`docs/runtime/edge-jetson-orin-nano-m4-hil-report.md` auf `pending_hardware`.

## Übergang zu M5

M5 baut auf den Herkunftsdaten und Sync-Metriken auf und ergänzt Fleet-weite Diagnose-/SLO-
Regeln, den Operator-Deployment-Assistenten, sichtbare Kanalziele und manuellen Rollback.
Gestaffelte Agent-/Runtime-Rollouts und automatische Rollout-Stopps gehören gemäß Haupt-Roadmap
zu M6. Ein hardwarebeschleunigter H.264/WebRTC-Fanout bleibt eine optionale spätere Optimierung
hinter dem bestehenden Stream-Vertrag.
