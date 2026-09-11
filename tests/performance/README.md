# Performance Harness

This suite is a performance smoke layer, not a full benchmark harness.

It verifies:

- realtime and batch paths resolve to different active releases
- batch pressure does not block the realtime request path

Run with:

```powershell
uv run pytest tests/performance -ra
```
