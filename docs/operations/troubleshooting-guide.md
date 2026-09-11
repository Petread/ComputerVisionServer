# Troubleshooting Guide

Use the admin UI first. `Overview`, `Operational Health`, `Deployment`, `Training Jobs`, `Batch Inference`, and `Audit Log` are the supported investigation surfaces.

| Symptom | Likely Cause | Where To Look | First Action |
| --- | --- | --- | --- |
| Admin UI actions fail broadly | `admin-api`, PostgreSQL, or Redis outage | `Overview`, `Operational Health` | Restore the failed dependency before retrying commands |
| Upload session stuck in `finalizing` | `control-worker` or `dataset` queue backlog | `Operational Health`, `Audit Log` | Check worker heartbeat and dataset queue depth |
| Snapshot stuck in `exporting` or `materializing` | Label Studio export or control-worker issue | snapshot detail, webhook audit, dataset queue | Verify Label Studio reachability and queue progress |
| Snapshot failed validation | bad labels, missing classes, or split/asset issues | snapshot validation report | Fix labeling/data issue and create a new snapshot |
| Training job stopped updating | training worker crash or GPU contention | `Training Jobs`, `Operational Health` | Check heartbeat, checkpoint age, and GPU pressure |
| Training fails immediately with recipe/task-type mismatch | incompatible model template/recipe selected for the project task type | training job detail, project pipeline model/template settings | Use `classifier-*` templates for classification projects and `object-detection-*` templates for detection projects, then retry training |
| Training or worker jobs fail immediately with `DuplicatePreparedStatement` | a forked worker reused a PostgreSQL connection pool created in the parent process | worker log, training job detail, recent worker restart history | Restart the affected worker and deploy the process-aware DB session factory fix so each forked process creates its own SQLAlchemy engine |
| Training reaches `100%` and then fails | post-training registration failed because required runtime tooling such as `git` or `dvc` is missing, or MLflow artifact logging fell back to `file:///...` instead of the running tracking server | training job detail, `training-worker` log, runtime image contents | Sync `/workspace/.venv`, confirm `which git`, `which dvc`, and `mlflow.get_tracking_uri()` inside `training-worker`, restart the worker, then rerun the job |
| Training resume unavailable | no resume-safe checkpoint | training job detail | Requeue or restart from the same `training_job_id` path |
| Package build failed | missing export artifacts or package filesystem issue | `Deployment` package detail | Inspect package error and model export manifests |
| Stage failed before promotion | compatibility or smoke-test failure | deployment release detail | Keep production unchanged; resolve the failed check before restaging |
| Realtime request latency spikes | gateway overload or GPU pressure | realtime status, operational GPU view | Reduce parallel work or pause non-critical load |
| Runtime channel stuck in `start_requested` | runtime-control worker/orchestrator has not consumed the command | `Channels`, `Operational Health`, runtime audit | Check runtime-control heartbeat and reconcile runtime commands |
| Realtime live view has no frame | source unreachable, executor reconnecting, or no active realtime runtime instance | channel detail live view, source health, runtime metrics | Check camera URL/secret refs and restart only after source reachability is confirmed |
| Trigger endpoint returns `401` | missing or invalid machine API key | pipeline/deployment trigger detail, `Access & Security -> API Keys` | Use a token with `runtime.trigger.invoke`; rotate the key if the secret was exposed |
| Trigger endpoint returns `503` | triggerable pipeline is not published/ready, channel frame acquisition is unavailable, or trigger executor admission is saturated | pipeline/deployment trigger readiness, channel source health, runtime metrics | Publish/fix the pipeline, restore channel frame acquisition, or reduce trigger concurrency |
| Capture delivery stays `queued` or `retrying` | capture-service drain worker down, Label Studio unavailable, duplicate/rate limit active | channel capture history, capture-service health | Restore capture-service/Label Studio and let the durable request retry |
| ROI capture fails | ROI geometry invalid or source frame unavailable | capture history detail, result record, live frame state | Confirm detections/ROI fields and that the result includes a frame or ROI asset |
| Connector delivery failed | connector config invalid, destination unavailable, or retries exhausted | channel output connector panel, connector delivery history | Fix config/dependency and re-enable or save the connector to resume delivery |
| Batch job incomplete | chunk failures or batch worker interruption | batch job detail | Rerun only incomplete chunks once the worker is healthy |
| Rollback unavailable | no previous healthy release present | deployment targets view | restore or restage a rollback candidate before future promotions |
| Audit gaps suspected | database or audit export issue | `Audit Log`, audit JSONL export path | verify PostgreSQL and audit filesystem health immediately |

## Escalate To Engineering When

- production rollback is missing or unhealthy
- a queue grows while its worker heartbeat is healthy
- Triton repository integrity mismatches the serving package
- mounted secrets or TLS material fail validation
- PostgreSQL or Redis require coordinated restart
- runtime model options are missing even though a release is active
- runtime capture or connector queues grow while their service heartbeats are healthy
