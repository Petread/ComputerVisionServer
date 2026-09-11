# End-to-End Harness

This suite exercises cross-service workflows against the real FastAPI apps and worker jobs in-process.

Covered scenarios:

- training request through the admin API to background worker completion
- model registration with lineage and metrics
- staging and production deployment flow for realtime inference
- batch submission through the admin API to completed result retrieval

Run with:

```powershell
uv run pytest tests/e2e -ra
```
