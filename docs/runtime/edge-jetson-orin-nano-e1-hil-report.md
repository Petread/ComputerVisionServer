# Jetson Orin Nano E1 HIL-Abnahme

- Status: `pending_hardware`
- Software-Basis: E1 Productive Edge Execution
- Zielprofil: Jetson Orin Nano, NVMe, JetPack 7.2 / L4T 39.2

## Freigabematrix

| Prüfung | Soll | Ist | Evidenz | Status |
| --- | --- | --- | --- | --- |
| Engine-Build | FP16 lokal mit `trtexec` | ausstehend | – | `pending` |
| Engine-Load | TensorRT deserialisiert Plan | ausstehend | – | `pending` |
| Warmup | 2 Läufe fehlerfrei | ausstehend | – | `pending` |
| Golden Output | innerhalb `atol`/`rtol` | ausstehend | – | `pending` |
| Kamera | Pilotauflösung und Samplingrate | ausstehend | – | `pending` |
| Task Output | fachlich gegen Golden Set geprüft | ausstehend | – | `pending` |
| Overlay | Geometrie auf Quellbild deckungsgleich | ausstehend | – | `pending` |
| Result Replay | keine doppelte zentrale Persistenz | ausstehend | – | `pending` |
| SPS-Trigger | nur Allowlist-IP, idempotent | ausstehend | – | `pending` |
| Fremd-IP | 9443/9444 durch ACL/UFW blockiert | ausstehend | – | `pending` |
| Lasttest | 5 min Warmup + 30 min Last | ausstehend | – | `pending` |
| Latenz/FPS | P50/P95/P99 und Source/Processed/Drop | ausstehend | – | `pending` |
| Ressourcen | CPU/RAM/GPU/Temperatur/Disk | ausstehend | – | `pending` |

## Abnahmeentscheidung

`pending_hardware`. Dieser Status darf erst nach Hinterlegung von Rohlogs, Modell-/Engine-Hashes,
Messwerten, Netzwerkregeln, Golden-Diff und Reviewerfreigabe auf `accepted` gesetzt werden.
