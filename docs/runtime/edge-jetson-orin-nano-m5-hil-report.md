# Jetson Orin Nano M5 HIL Acceptance Report

- Status: `pending_hardware`
- Target: Jetson Orin Nano, JetPack 7.2 / L4T 39.2, NVMe
- Software milestone: M5 `software_complete`

## Pending evidence

- fixed-IP, isolated VLAN, ACL and Edge-firewall evidence;
- real identity, health and capability reconciliation before preflight;
- immutable release ID/digest and transferred artifact hashes;
- local ONNX-to-TensorRT build and Golden Input comparison;
- rejection after simulated pipeline or capability drift;
- successful A-to-B activation with the previous slot in standby;
- operator-confirmed rollback from B to A and idempotent retry;
- rollback behavior during response loss and VLAN interruption;
- local channel continuity during central server loss;
- persisted diagnostics for offline, storage pressure and result ACK lag;
- P50/P95/P99 latency and verification of 100 ms live / 500 ms trigger targets;
- Fleet alert and audit-event evidence;
- CPU, RAM, GPU, storage and network measurements;
- five-minute warmup plus 30-minute 1080p/30 workload run.

Hardware acceptance remains open until the physical device, representative camera, model, PLC
test endpoint and isolated plant VLAN are available. Simulator and software tests do not replace
this report.
