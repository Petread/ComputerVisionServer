# Jetson Orin Nano M0 HIL Report

## Status

`pending_hardware`

The M0 software protocol is implemented, but no physical Jetson Orin Nano was available for this
repository run. Do not mark M0 hardware-accepted until every required observation below is filled
from the benchmark harness and signed off.

## Target

- Profile: `jetson-orin-nano-jp72`
- Storage: NVMe, at least 128 GiB
- Management identity: fixed IPv4 in isolated machine VLAN
- Server access: TCP 9443 only from the fixed orchestration-server IP

## Required Evidence

| Check | Required result | Observed |
| --- | --- | --- |
| JetPack / L4T | 7.2 / 39.2 | pending |
| CUDA / TensorRT / DeepStream | profile-compatible | pending |
| Fixed-IP reachability | server succeeds; non-server client blocked | pending |
| ONNX to TensorRT | engine built locally and digest recorded | pending |
| TensorRT build | build time and engine size recorded | pending |
| Golden input | result within declared tolerance | pending |
| Capture | stable 1080p/30 input | pending |
| Frame sampling | configured interval recorded | pending |
| Live latency | P95 <= 100 ms for reference workload | pending |
| Trigger latency | P95 <= 500 ms for reference workload | pending |
| Resource telemetry | CPU/RAM/GPU/disk recorded | pending |
| Network loss | workload continues and results buffer | pending |
| Reconnect | buffered results arrive without duplicate persistence | pending |
| Reboot | new boot ID; last committed workload recovers | pending |

## Procedure

1. Install the approved golden image to NVMe.
2. Apply device-specific fixed-IP configuration and VLAN firewall policy.
3. Run `scripts/benchmark/edge_hil_probe.py` on the Jetson and retain its JSON output.
4. Build the reference ONNX model with the pinned TensorRT runtime.
5. Execute golden comparison and the configured runtime benchmark after five minutes warmup.
6. Run for 30 minutes while collecting latency, FPS, drops, temperature, and resource metrics.
7. Disconnect the server network, continue processing, reconnect, and verify result replay.
8. Reboot the Jetson and confirm a new boot ID plus workload recovery.
9. Replace every `pending` cell with evidence links and change status only after review.

The optional metrics JSON passed to the harness must contain capture dimensions/FPS, frame
sampling interval, golden-input outcome, P50/P95/P99 latency, source/processed/drop FPS, average
CPU/GPU utilization, peak RAM/GPU-memory use, disk growth, TensorRT build time and engine size,
plus at least 300 seconds warmup and 1,800 seconds measurement.
