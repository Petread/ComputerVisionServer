# E2 HIL-Abnahme – Plattformen, Provisionierung und Kameras

- Status: `pending_hardware`
- Stand: 2026-07-22
- Freigabe erforderlich durch: Software, OT/Netzwerk, Betrieb

## Zielmatrix

| Ziel | Golden Image | Offline-Install | Capability Drift | Neustart/Service | Ergebnis |
|---|---|---|---|---|---|
| Jetson Orin Nano / NVMe | pending | pending | pending | pending | pending |
| Jetson AGX Orin / NVMe | pending | pending | pending | pending | pending |
| Windows 11 / NVIDIA x86-64 | pending | pending | pending | pending | pending |

## Kamera-Matrix

Jede tatsächlich für den Kunden freigegebene Zeile benötigt 30 Minuten Laufzeit nach fünf
Minuten Warmup, 1080p/30-Capture, konfigurierte Frame-Abtastung, Reconnect, P50/P95/P99,
Source-/Processed-/Drop-FPS sowie CPU/RAM/GPU/Temperatur.

| Plattform | Adapter | Kamera/SDK/Treiber | Free Run | Trigger | Reconnect | Golden Output | Ergebnis |
|---|---|---|---|---|---|---|---|
| Orin Nano | RTSP/OpenCV | pending | pending | n/a | pending | pending | pending |
| Orin Nano | Aravis GigE | pending | pending | pending | pending | pending | pending |
| Orin Nano | Basler Pylon | pending | pending | pending | pending | pending | pending |
| Orin Nano | FLIR Spinnaker | pending | pending | pending | pending | pending | pending |
| AGX Orin | freigegebene Auswahl | pending | pending | pending | pending | pending | pending |
| Windows 11 | freigegebene Auswahl | pending | pending | pending | pending | pending | pending |

## Integritäts- und Provisioning-Nachweis

- [ ] Zwei CI-Builds aus identischen Releaseinputs haben denselben Archiv-SHA-256.
- [ ] Produktionsbundle besitzt `SIGNATURE_POLICY=required` und gültige Signatur.
- [ ] Manipuliertes Bundle wird vor jeder Installation abgelehnt.
- [ ] Installation funktioniert bei physisch getrennter Internetverbindung.
- [ ] Feste IP, ACL und lokale Firewall erlauben ausschließlich die definierten Paare.
- [ ] Capability-Snapshot stimmt mit OS-/NVIDIA-/SDK-Inventar überein.
- [ ] Abweichender Golden-Image-Stand erzeugt sichtbare Drift/Warnung.
- [ ] Agent, Runtime, Outbox und aktive Kanäle überstehen Neustart und Serverausfall.
- [ ] Vorheriges Release kann gemäß Runbook wiederhergestellt werden.

## Evidenz

Für jede Zeile sind Bundle-Hash, Signaturfingerprint, Golden-Image-Manifest, Capability-JSON,
Treiber-/SDK-Versionen, Kamera-Seriennummer, Rohlogs, Messdatei, Screenshots und Reviewer zu
archivieren. Erst nach vollständiger Evidenz wechselt der Gesamtstatus auf `accepted`.
