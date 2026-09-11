# Jetson Orin Nano M2 HIL-Abnahme

- Status: `pending_hardware`
- Softwarestand: M2
- Zielplattform: Jetson Orin Nano, NVMe, JetPack 7.2 / L4T 39.2

## Voraussetzungen

- isoliertes Anlagen-VLAN und feste Server-/Edge-IP eingerichtet;
- Offline-Bundle einschließlich Wheelhouse und `SHA256SUMS` bereitgestellt;
- Referenz-RTSP-/GStreamer-Kamera mit 1080p/30 erreichbar;
- Referenzmanifest und Golden-Input-Satz aus dem M0-HIL-Paket vorhanden.

## Abnahmeprotokoll

| Prüfung | Erwartung | Status | Messwert/Notiz |
| --- | --- | --- | --- |
| Offline-Installation auf NVMe | Kein Internet-/Repository-Zugriff | pending | Hardware fehlt |
| Firewall | TCP 9443 nur von Server-IP | pending | Hardware fehlt |
| Runtime API | WAL, Slots und Quoten werden gemeldet | pending | Hardware fehlt |
| A/B Commit | Neuer Workload in inaktivem Slot | pending | Hardware fehlt |
| Rollback | Vorheriger Slot wird wieder aktiv | pending | Hardware fehlt |
| Agent-Neustart | Letztes Deployment wird recovered | pending | Hardware fehlt |
| Stromverlust | SQLite/Blob-Zustand bleibt konsistent | pending | Hardware fehlt |
| Serverausfall | Edge Worker läuft lokal weiter | pending | Hardware fehlt |
| Result-Replay | Lückenlos und ohne doppelte Persistenz | pending | Hardware fehlt |
| RTSP/GStreamer | 1080p/30 Capture und Sampling | pending | Hardware fehlt |
| Supervisor | Backoff, Crash-Loop und Watchdog-Recovery | pending | Hardware fehlt |
| Quoten | 507/Degraded ohne Abbruch des aktiven Slots | pending | Hardware fehlt |
| 30-Minuten-Last | Keine unkontrollierte Speicherzunahme | pending | Hardware fehlt |

## Abschlusskriterien

Nach Eintragung von Gerätedaten, Messwerten, Logs und Abweichungen darf der Status erst dann auf
`accepted` wechseln, wenn alle sicherheits- und recoveryrelevanten Prüfungen bestanden wurden.
Bis dahin ist M2 ausschließlich `software_complete`, nicht hardwareabgenommen.
