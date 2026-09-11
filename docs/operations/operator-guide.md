# Operator Guide

This guide is the normal browser-first operating path for the platform. It assumes operators use the admin UI and do not need shell access for standard workflows.

## 1. Start Of Shift

1. Open the admin UI.
2. Check `Overview` for active alerts, deployment targets, and queue backlog.
3. Open `Operational Health` if any service, worker, disk, or GPU card is degraded.
4. Do not start new work if `admin-api`, either inference gateway, PostgreSQL, Redis, or the relevant worker queue is down.

## 2. Access And Security

1. Open `Access & Security` from `Operations` or through the shortcut on `Settings`.
2. Use `Users` to create browser users with a unique `username`, display name, optional notes, and an initial password.
3. Use the user detail panel to deactivate or reactivate accounts and to reset another user's password when needed.
4. Use `Change My Password` for self-service password changes. Password changes invalidate older sessions automatically on the next request.
5. Use `API Keys` to create inference credentials with label, description, intended usage, subject/application name, scopes, optional expiration, and optional endpoint mode hint.
6. Copy the generated API token from the one-time reveal screen immediately. The full secret is not shown again after creation.
7. Treat `bootstrap` keys as read-only inventory. They remain visible for migration and recovery, but changes to them happen through configuration and mounted secret files rather than the UI.
8. Use the audit summary on the selected user or API key to confirm who created, changed, or revoked the credential.
9. For the detailed runbook, see [access-management-guide.md](./access-management-guide.md).

## 3. Dataset Upload And Labeling

1. Open `Dataset Snapshots`.
2. Create or select the target dataset.
3. Use `Browser Upload Sessions` to upload image files directly from the browser.
4. For classification datasets with already sorted folders, switch the upload mode to `classified folder`, choose the mapped Label Studio classification project, and upload one class folder at a time. The folder name is used as the image-level class unless an operator edits the label before upload.
5. Wait for the upload session to move from `uploading` to `finalizing` to `completed`.
6. Open `Label Studio Projects`.
7. Refresh discovered projects if needed, map the correct Label Studio project to the dataset, and open Label Studio from the UI.
8. Let annotators work in Label Studio while operators monitor annotation statistics and webhook audit data from the platform UI.
9. For anomaly projects, use image-level good/bad labels. The platform asks for `Classification` vs `Anomaly` when the model is created. See [anomaly-models.md](./anomaly-models.md).

Label Studio may still use its normal image Classification/Choices template for
anomaly labeling. Label Studio records the selected choices only; it does not
decide whether the platform should train a classifier or an anomaly detector.
The platform decides that when an operator creates the project model. Set the
model type to `Anomaly` when `good`/`bad` should mean normal/anomalous samples
for PatchCore training. If the model type stays `Classification`, the same
`good`/`bad` choices are treated as ordinary classification classes.
Choice-only Label Studio projects are initially discovered as `Classification`;
after an operator creates an `Anomaly` model, subsequent Label Studio sync or
reconciliation keeps the anomaly mapping even though the Label Studio template
still looks like Classification.

### Manual Live Capture

Manual live capture is available from the live view of a runtime channel.

1. Open `Channels`, then open the channel detail page.
2. Open the live view.
3. In `Manual live capture`, choose the Label Studio project by name.
4. Click `Capture once`, or enter an interval in seconds and click `Start`.
5. Click `Stop` to end interval capture.

The browser shows the current session state, last capture time, and the last
success or error. Operators never type internal project IDs for this workflow.

For details, see [../runtime/manual-live-capture.md](../runtime/manual-live-capture.md).

## 4. Immutable Snapshot Creation

1. In `Dataset Snapshots` or `Label Studio Projects`, create a snapshot for the mapped dataset/project pair.
2. Watch the snapshot move through `requested`, `exporting`, `materializing`, `validating`, and `ready`.
3. Open snapshot details and validation before using it for training.
4. Do not train from snapshots in `failed` status.

For anomaly mappings, snapshot conversion canonicalizes accepted good labels to
`normal` and accepted bad labels to `anomalous`. The validation report is the
operator-visible readiness check: it fails conflicting labels, unknown anomaly
labels, missing normal samples, missing anomalous samples, missing normal train
samples, or missing normal/anomalous evaluation samples.

## 5. Training

1. Open the project pipeline or `Training Jobs`.
2. Select a backend-supported model/template. The visible model list comes from the backend capability matrix, not from frontend notes.
3. Review the inherited deployment promotion criteria. If the template permits overrides, adjust the typed criteria in the form.
4. Queue the training job.
5. Monitor current status, epoch/step progress, checkpoints, evaluation summary, resolved trainer backend, resolved architecture, resolved promotion criteria, and structured logs.
6. If needed, use `Request Cancel` or `Resume`. These remain control-plane commands; browser refresh does not stop the worker.
7. For anomaly models, select the `PatchCore Anomaly Detector` template. Other anomaly methods are intentionally hidden until their full training, deployment, inference, and overlay path exists.

## 6. Model Review

1. Open `Experiments & Registry`.
2. Review dataset snapshot lineage, primary metrics, resolved model identity, promotion gate pass/fail result, immutable metadata fingerprint, rollout approvals, and rollback target markers.
3. Approve staging or production only after metrics, lineage, artifact validation, and the configured promotion gate are acceptable.

## 7. Deployment, Stage, Promote, Roll Back

1. Open `Deployment`.
2. Build the immutable deployment package for an approved model version.
3. Stage the package to `realtime`, `batch`, or both.
4. Review compatibility report, smoke report, release detail, and promotion criteria results.
5. Promote to production only after the staged release is healthy.
6. If a production issue appears, use `Rollback` on the affected target. Rollback restores the previous immutable release without rebuilding artifacts.
7. For an unused release, use `Deactivate model` to unload every Triton version under the stable model name while retaining deployment packages, reports, and history. The platform blocks deactivation while the model is active production, reserved for rollback, bound to a running channel, or used by an active batch job.
8. Use `Activate model` on the retained release when it is needed again. The existing immutable package is staged into the repository and loaded without retraining or creating a new release.

## 8. Inference Workflows

### Runtime Channels

1. Open `Channels`.
2. Use search and filters to find channels by technical place, source type, live runtime mode, or status.
3. Create or edit a channel as a camera/source connection. Do not create channels just to represent triggers. Pipelines should use `Channel frame input`; camera/RTSP/file details belong to the channel form.
4. If a running channel is saved with a different pipeline or source binding, the platform queues a runtime restart automatically so the active executor binds to the saved channel configuration.
5. For RTSP sources, enter the stream address without inline credentials and use the RTSP username/password fields when authentication is required. If a credentialed RTSP URL is pasted, the API strips credentials into managed secret refs and stores only the sanitized source config.
6. For GigE Vision cameras temporarily bridged through Aravis, choose
   `GigE Vision (Aravis)` and enter the Aravis camera name, serial number,
   device ID, or IP address. The runtime starts the local bridge service in
   the background when the channel starts.
7. For `Test file` sources, choose an image or short video in the file picker. The platform stores it as a managed runtime test artifact and feeds it to realtime or triggerable pipelines as a channel frame.
8. For realtime channels, set `Max processing FPS` and `Sample every N` on the channel form when a camera or test file delivers more frames than the runtime should process.
9. Use `Start runtime`, `Stop runtime`, `Restart runtime`, and `Open live view` from the card or detail page.
10. Treat `start_requested` and `stop_requested` as command intent, not proof that the runtime is ready or stopped.
11. Trust the runtime state fields: `validating`, `loading_pipeline`, `starting`, `running`, `degraded`, `stopping`, `stopped`, and `failed`.
12. Open live view when the channel shows a `running` or `degraded` realtime runtime instance, or after a triggerable pipeline has produced a latest trigger frame.
13. Use the live view frame and overlay to confirm that the active runtime instance is actually receiving frames before collecting samples.
14. Use `Manual live capture` in the live view to capture full-frame samples into one of the synced Label Studio projects shown by name.
15. Configure PLC/web trigger behavior in `Runtime Pipelines` with `HTTP Trigger Input -> Channel frame input -> Response Output`. Publish the pipeline, assign it to the channel whose source should be captured, then copy the derived endpoint URL and response contract from pipeline/deployment detail. Channel detail shows the derived triggerable pipeline and latest trigger display state.
16. Use `Output Connectors` to enable, disable, and inspect channel output delivery.
17. Treat `Retrying` as a temporary connector delivery failure and `Failed` as a connector that needs configuration or dependency attention.
18. Use `Recent Results` on the channel detail page to inspect the latest persisted trigger or realtime results.
19. Open `Results` to browse historical results by channel, pipeline, time range, mode, status, or result type.
20. Treat start, stop, restart, triggerable pipeline publication, trigger PUT calls, connector changes, and manual interval capture start/stop as audited runtime operations; execution happens in runtime services.

For the detailed channel workflow, see [../runtime/channel-operator-workflows.md](../runtime/channel-operator-workflows.md).
For lifecycle states and recovery behavior, see [../runtime/runtime-lifecycle-core.md](../runtime/runtime-lifecycle-core.md).
For realtime live view, RTSP reconnects, overlays, and drop policy, see [../runtime/realtime-live-runtime.md](../runtime/realtime-live-runtime.md).
For PLC pipeline trigger endpoints, see [../runtime/trigger-runtime-plc-contract.md](../runtime/trigger-runtime-plc-contract.md).
For runtime result semantics, see [../runtime/result-semantics-guide.md](../runtime/result-semantics-guide.md).
For connector behavior, see [../runtime/output-connector-architecture.md](../runtime/output-connector-architecture.md).
For the end-to-end runtime integration contract, see [../runtime/end-to-end-runtime-integration.md](../runtime/end-to-end-runtime-integration.md).

### Output Connectors

1. Open a channel detail page and review `Output Connectors`.
2. A `Ready` connector has delivered successfully or is waiting for its first matching output.
3. `Retrying` means connector-service will try again according to the connector policy.
4. `Failed` means retries are exhausted or the failure is not retryable. Read the last error, fix configuration or the dependency, then save or re-enable the connector.
5. Disable a connector to stop future deliveries for that configured output path. Disabled connectors are not silently re-created by runtime output nodes.
6. Connector delivery failures do not invalidate the runtime result. Inspect `Results` for the produced result and the connector delivery history for output delivery state.

### Siemens SPS Writes

1. Configure Siemens PLC writes inside the runtime pipeline editor with the `Write to Siemens SPS` output node.
2. Keep the node in dry-run until the PLC address, datatype, mapping, fallback, and failure behavior have been validated.
3. Use the structured source field picker for upstream result fields such as `classification.top1.label`, `classification.top1.score`, `measurement.distance_mm`, or `decision`.
4. Publish warnings for enabled, non-dry-run PLC writes are expected. Preview and validation do not write to PLC hardware.
5. Open `Operations -> Access & Security` to inspect configured Siemens SPS targets and enable the deployment PLC write gate when commissioning is complete.
6. Real PLC writes require the active runtime gate to be allowed; restart the runtime services if the access page shows `restart required`. See [../runtime/siemens-sps-write-node.md](../runtime/siemens-sps-write-node.md).

### Realtime Live View

1. Start a realtime channel that has a published realtime pipeline assigned.
2. Open `Live View` when the runtime state is `running` or `degraded`.
3. Confirm the latest frame is visible. If no frame is visible, use the source status and recovery hint shown in the panel.
4. Toggle overlay layers such as detections, segmentation, ROIs, zones, motion, and calculated values as needed.
5. Watch FPS, drop rate, p95 latency, reconnect count, source health, and last frame time.
6. If source FPS is much higher than processed FPS but latency and source health are good, lower `Max processing FPS` or increase `Sample every N` instead of restarting the channel.
7. If reconnect count or source errors keep increasing, check camera reachability, RTSP path, secret references, and network path before restarting the channel.

### Realtime

1. Open `Realtime Testing`.
2. Select the active or staged release.
3. Submit a test image.
4. Confirm the returned route, latency, and prediction structure.

### Result Browsing

1. Open `Results`.
2. Filter by channel name, published pipeline, mode, status, result type, and time range.
3. Select a result to inspect final response fields, calculated values, normalized inference outputs, overlay primitives, and runtime metadata.
4. Use advanced raw sections only for support investigation.

### Batch

1. Open `Batch Inference`.
2. Select the batch release.
3. Upload test images or reference an input manifest.
4. Submit the batch job.
5. Monitor chunk progress, recoverability, and published artifacts.
6. Download the result bundle if needed.

## 9. Audit, Settings, And Escalation

1. Use `Audit Log` for operator actions, state transitions, and webhook history.
2. Use `Settings` to confirm storage paths, integration reachability, deployment target state, and auth posture.
3. As a platform administrator, use `Settings` to save and test the Label Studio API token when project sync reports authentication problems.
4. Escalate to the engineer guide or troubleshooting guide if a worker heartbeat is stale, a snapshot or deployment is stuck, queue depth keeps growing, or a production target has no healthy rollback candidate.

Primary escalation references:

- [troubleshooting-guide.md](./troubleshooting-guide.md)
- [supported-model-matrix.md](./supported-model-matrix.md)
- [go-live-checklist.md](./go-live-checklist.md)
- [../engineering/engineer-guide.md](../engineering/engineer-guide.md)
