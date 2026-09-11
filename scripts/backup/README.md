# Backup Scripts

This folder is reserved for metadata and artifact backup automation.

Initial foundation scope includes:

- PostgreSQL dump orchestration
- MLflow metadata backup
- audit export retention

The current maintenance path is coordinated through `maintenance_worker.jobs.backup_metadata`, which
creates a timestamped metadata backup root under the managed platform state tree and records a backup
manifest for the copied filesystem metadata plus the PostgreSQL dump attempt.
