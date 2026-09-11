# Operator Workflow

## Browser-First Workflow Coverage

- Sign in to the admin UI and confirm the `Overview` and `Operational Health` pages are green enough to proceed.
- Open `Access & Security` to create users, change passwords, and manage inference API keys without editing configuration files for normal operations.
- Create a dataset, upload browser assets through upload sessions, and confirm the dataset moves to a ready state.
- Discover or map a Label Studio project, then open Label Studio from the admin UI for native annotation work.
- Monitor annotation statistics, webhook audit, and snapshot inventory from the admin UI.
- Create an immutable dataset snapshot and watch it progress through export, materialization, validation, and DVC tracking.
- Start a training job from the admin UI and monitor checkpoints, logs, metrics, and resumable state without keeping the browser tab open.
- Review model lineage, metrics, approvals, and rollback markers from the registry views without exposing raw MLflow or DVC workflows to normal operators.
- Build an immutable deployment package, stage it to realtime or batch, inspect compatibility and smoke-test evidence, then promote to production.
- Use the rollback control to restore the previous immutable release without rebuilding artifacts.
- Run realtime test requests and batch inference jobs from the admin UI while preserving strict separation between the two inference paths.
- Use `Settings`, `Overview`, `Operational Health`, and `Audit Log` as the supported operational visibility surface for normal workflows.
