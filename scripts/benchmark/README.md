# Benchmark Scripts

This folder holds benchmark runners for:

- realtime latency qualification
- batch throughput qualification
- training plus inference coexistence drills

`stem_rotation_optimizer_benchmark.py` measures the registered production stem
priority/runtime/optimizer/HMI path for 3–6 m stems and configurable 25–100 mm slice
spacing. It starts no service and enforces no production SLA.

`stem_geometry_benchmark.py` compares the existing geometry kernels with one/two
concurrent frame jobs and one/two native threads, checks numerical fingerprints,
and reports latency percentiles. Input preparation and the separate server
qualification are described in
[the implementation report](../../docs/Stammoptimierung%20und%203D%20Messung/stem_pipeline_implementation_2026-09-14.md).

The geometry runner also emits `stage_p50_ms` for support preparation, axis work,
cross-sections and curvature. The live probe preserves `stage_timings_ms`, including
phase recovery/deadline counters and the effective OpenCV thread count. See the
[recovery implementation and qualification procedure](../../docs/Stammoptimierung%20und%203D%20Messung/stem_pipeline_recovery_implementation_2026-09-15.md).
