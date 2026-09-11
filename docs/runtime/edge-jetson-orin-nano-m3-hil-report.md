# Jetson Orin Nano M3 HIL Acceptance Report

- Status: `pending_hardware`
- Target: Jetson Orin Nano, JetPack 7.2 / L4T 39.2, NVMe
- Software milestone: M3 `software_complete`

## Pending evidence

- device inventory and capability digest;
- fixed-IP/VLAN/ACL/firewall reachability evidence;
- release ID, pipeline version, model version and artifact digests;
- local FP16/INT8 TensorRT command, build time, engine size and digest;
- task-specific Golden input/output comparison;
- A/B commit and forced rollback evidence;
- resumed upload and controller-restart evidence;
- confirmed stop and unreachable-target move guard;
- five-minute warmup plus 30-minute 1080p/30 load metrics;
- P50/P95/P99 latency, source/processed/drop FPS, CPU, RAM, GPU and disk metrics.

Hardware acceptance remains open until the physical device and representative camera/model are
available. Software tests use the deterministic engine simulator and do not substitute for this
report.
