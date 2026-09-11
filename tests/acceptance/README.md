# Acceptance Harness

This suite mirrors go-live drills and operational recovery checks from the architecture spec and the go-live checklist.

Covered drills:

- operator UI restart during active training
- failed deployment attempt preserving the current production release
- rollback restoring the previous immutable release
- runtime integration acceptance is covered by the focused cross-service runtime
  tests listed in `docs/runtime/end-to-end-runtime-integration.md`

Run with:

```powershell
uv run pytest tests/acceptance -ra
```
