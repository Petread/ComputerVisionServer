# Acceptance Report

Date: `2026-05-02`

Scope: W8 final runtime-plane consistency and completion pass against:

- [../architecture/cv-platform-spec.md](../architecture/cv-platform-spec.md)
- [../runtime/vision-runtime-spec.md](../runtime/vision-runtime-spec.md)
- [../runtime/vision-runtime-roadmap.md](../runtime/vision-runtime-roadmap.md)

## Final Deliverables

| Deliverable | Location |
| --- | --- |
| Final repository tree | [../generated/monorepo-file-tree.md](../generated/monorepo-file-tree.md) |
| Final config/env matrix | [environment-config-matrix.md](./environment-config-matrix.md) |
| Final route/navigation map | [operator-workflow-map.md](./operator-workflow-map.md) and [../architecture/endpoint-exposure-map.md](../architecture/endpoint-exposure-map.md) |
| Final runtime state model summary | [../runtime/runtime-lifecycle-core.md](../runtime/runtime-lifecycle-core.md) and [../runtime/runtime-operations-runbook.md](../runtime/runtime-operations-runbook.md) |
| Final operator workflow summary | [operator-guide.md](./operator-guide.md) and [../runtime/channel-operator-workflows.md](../runtime/channel-operator-workflows.md) |
| Final troubleshooting guide | [troubleshooting-guide.md](./troubleshooting-guide.md) |
| End-to-end runtime integration guide | [../runtime/end-to-end-runtime-integration.md](../runtime/end-to-end-runtime-integration.md) |

## Verification Summary

| Check | Result |
| --- | --- |
| Full Python test suite: `python -m pytest tests` | `318 passed` |
| Runtime-focused backend/contract suite | `55 passed` |
| Reverse-proxy and compose topology tests | `6 passed` |
| Admin UI typecheck: `.\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.json` | passed |
| Admin UI tests: `.\node_modules\.bin\vitest.cmd run` | `8 files, 30 tests passed` |
| Admin UI production build: `.\node_modules\.bin\vite.cmd build` | passed with existing chunk-size warning |
| Static whitespace check: `git diff --check` | passed with Git LF/CRLF warnings only |

`pnpm` was not installed in the Windows shell used for this pass, so frontend
verification used the checked-in workspace binaries under
`frontend/apps/admin-web/node_modules/.bin`.

## Acceptance Against Platform Spec

| Requirement | Status | Evidence |
| --- | --- | --- |
| Open-source-only stack | Met | FastAPI, React/Vite, PostgreSQL, Redis, Label Studio CE, MLflow OSS, DVC, Triton, PyTorch/OpenMMLab/Ultralytics remain the stack |
| No operator CLI for normal workflows | Met | Channel, runtime pipeline, trigger, capture, connector, result, model, deployment, and health workflows are browser/API driven |
| Control plane, runtime plane, inference plane separated | Met | `runtime-admin-api` owns config/UI flows; orchestrator/executors/capture/result/connector own execution; inference gateways/Triton own prediction |
| Realtime and batch inference isolated | Met | runtime inference nodes call realtime gateway; batch worker/gateway remain separate |
| Deployment state is PostgreSQL truth | Met | runtime model options project from deployment releases/targets/model versions/model families |
| Secrets not committed | Met | tracked local token/secret files removed; `.gitignore` blocks local secret/token material; `infra/secrets/README.md` is the only tracked secret-path file |
| Production proxy exposure controlled | Met | runtime public prefixes are mapped; direct runtime service prefixes are blocked in production Caddy config |

## Acceptance Against Runtime Spec

| Runtime Capability | Status | Evidence |
| --- | --- | --- |
| Channel management | Complete | channel create/edit selects published runtime pipeline versions in UI and persists typed runtime config |
| Pipeline definitions and node model refs | Complete | pipeline validation resolves logical model refs against deployment-derived capabilities |
| Runtime lifecycle | Complete | W1 command, instance, heartbeat, stop/restart/reconcile tests pass |
| Realtime/live/overlay | Complete | W2 live executor, latest frame, overlay, and metrics tests pass |
| Pipeline HTTP triggers | Complete | HTTP Trigger Input, Channel frame input, and Response Output derive API-key protected trigger endpoints; tests use real trigger-executor PUT with scoped token |
| Result workflows | Complete | result records separate raw, normalized, derived, overlay, trigger response, and runtime metadata |
| Capture and Label Studio delivery | Complete | manual, interval, full-frame, ROI, multi-project delivery, retry, and raw LS-ID rejection tests pass |
| Output connectors | Complete | built-in connectors execute through durable delivery rows with retry isolation |
| Observability/recovery | Complete | runtime metrics, system status, stale runtime cleanup, and recovery runbook are aligned |

## Workstream Acceptance

| Workstream | Status | Acceptance |
| --- | --- | --- |
| W1 lifecycle core | complete | persisted runtime-control path, runtime instances, supervision, stop/restart/reconcile |
| W2 realtime/live/overlay | complete | bounded frame loop, live state, overlay, FPS/latency/drop metrics |
| W3 trigger hardening | complete | authenticated PUT path, unsafe image path rejection, timeout/error taxonomy |
| W4 results | complete | result-service list/latest/detail and operator result history |
| W5 capture | complete | durable capture requests, ROI materialization, Label Studio delivery |
| W6 connectors | complete | typed connector registry, durable delivery worker, status UI |
| W7 operations hardening | complete | metrics aggregation, health cards, recovery cleanup, runbook |
| W8 integration | complete | deployment-driven model discovery, shared auth scopes, selector-driven UI, docs/tests aligned |

## Residual Production Prerequisites

These are environment sign-off activities, not repository completion gaps:

- provision real mounted secrets and TLS material outside Git
- run the production compose profile on the target WSL2 workstation
- execute camera/PLC/Triton smoke tests against real devices and deployed models
- capture go-live evidence for rollout, rollback, recovery, and runtime SLOs
