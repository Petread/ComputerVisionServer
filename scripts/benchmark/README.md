# Benchmark Scripts

This folder holds benchmark runners for:

- realtime latency qualification
- batch throughput qualification
- training plus inference coexistence drills

`stem_rotation_optimizer_benchmark.py` measures the registered production stem
priority/runtime/optimizer/HMI path for 3–6 m stems and configurable 25–100 mm slice
spacing. It starts no service and enforces no production SLA.
