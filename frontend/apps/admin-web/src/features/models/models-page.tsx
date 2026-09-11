import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  ModelApprovalRequest,
  ModelLifecycleCommandRequest,
  RollbackTargetRequest,
  TargetPath
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import { architectureLabel, promotionPolicySummary, trainingDefaultsSummary } from '../pipelines/modeling';

type RegistryAction =
  | { kind: 'approve'; targetPath: TargetPath; approvalScope: 'staging' | 'production' }
  | { kind: 'deploy'; targetPath: TargetPath; requestedState: 'staged' | 'production' }
  | { kind: 'rollback-target'; targetPath: TargetPath; enabled: boolean }
  | { kind: 'archive' }
  | { kind: 'reject' };

export function ModelsPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [comparisonIds, setComparisonIds] = useState<string[]>([]);
  const [cleanupOpen, setCleanupOpen] = useState(false);

  const familiesQuery = useQuery({
    queryKey: ['model-families'],
    queryFn: api.registry.families
  });
  const versionsQuery = useQuery({
    queryKey: ['model-versions'],
    queryFn: () => api.registry.versions()
  });

  useEffect(() => {
    if (!selectedVersionId && versionsQuery.data?.items[0]) {
      setSelectedVersionId(versionsQuery.data.items[0].model_version_id);
    }
  }, [selectedVersionId, versionsQuery.data]);

  const detailQuery = useQuery({
    queryKey: ['model-version-detail', selectedVersionId],
    queryFn: () => api.registry.detail(selectedVersionId as string),
    enabled: Boolean(selectedVersionId)
  });

  const compareQuery = useQuery({
    queryKey: ['model-version-compare', comparisonIds.join(',')],
    queryFn: () => api.registry.compare(comparisonIds),
    enabled: comparisonIds.length > 1
  });
  const cleanupPreviewQuery = useQuery({
    queryKey: ['model-version-cleanup-preview', selectedVersionId],
    queryFn: () => api.registry.cleanupPreview(selectedVersionId as string),
    enabled: cleanupOpen && Boolean(selectedVersionId)
  });

  const actionMutation = useMutation({
    mutationFn: async (action: RegistryAction) => {
      if (!selectedVersionId) {
        throw new Error('Select a model version first.');
      }
      if (action.kind === 'approve') {
        const payload: ModelApprovalRequest = {
          target_path: action.targetPath,
          approval_scope: action.approvalScope,
          notes: `Approved for ${action.approvalScope} via admin UI.`
        };
        return api.registry.approve(selectedVersionId, payload);
      }
      if (action.kind === 'deploy') {
        return api.deployments.promote({
          model_version_id: selectedVersionId,
          target_path: action.targetPath,
          requested_state: action.requestedState
        });
      }
      if (action.kind === 'rollback-target') {
        const payload: RollbackTargetRequest = {
          target_path: action.targetPath,
          enabled: action.enabled,
          notes: action.enabled ? 'Pinned as rollback candidate from admin UI.' : 'Rollback pin removed.'
        };
        return api.registry.markRollbackTarget(selectedVersionId, payload);
      }
      if (action.kind === 'archive') {
        const payload: ModelLifecycleCommandRequest = {
          reason: 'Archived from admin UI after lifecycle completion.'
        };
        return api.registry.archive(selectedVersionId, payload);
      }
      const payload: ModelLifecycleCommandRequest = {
        reason: 'Rejected from admin UI.'
      };
      return api.registry.reject(selectedVersionId, payload);
    },
    onSuccess: async (_result, action) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['model-families'] }),
        queryClient.invalidateQueries({ queryKey: ['model-versions'] }),
        queryClient.invalidateQueries({ queryKey: ['model-version-detail', selectedVersionId] }),
        queryClient.invalidateQueries({ queryKey: ['model-version-compare'] }),
        queryClient.invalidateQueries({ queryKey: ['deployment-releases'] })
      ]);
      notify({
        title: actionLabel(action),
        tone: 'success'
      });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Registry action failed'), tone: 'error' });
    }
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: { confirmation_token: string; reason?: string | null; delete_artifacts?: boolean }) =>
      api.registry.cleanup(selectedVersionId as string, payload),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['model-families'] }),
        queryClient.invalidateQueries({ queryKey: ['model-versions'] }),
        queryClient.invalidateQueries({ queryKey: ['model-version-detail', selectedVersionId] }),
        queryClient.invalidateQueries({ queryKey: ['deployment-releases'] }),
        queryClient.invalidateQueries({ queryKey: ['deployment-targets'] })
      ]);
      notify({
        title: result.already_deleted ? 'Model version already removed' : 'Model version cleanup completed',
        description: `${result.deleted_entities.reduce((sum, item) => sum + item.count, 0)} entities affected.`,
        tone: 'success'
      });
      setCleanupOpen(false);
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Model version cleanup failed'), tone: 'error' });
    }
  });

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Model Registry</p>
          <h2>Immutable versions, approval records, and deployment-ready lineage.</h2>
          <p className="subtle-text">
            Operators stay in the admin UI while the backend keeps MLflow tracking, DVC linkage,
            rollout approvals, and rollback metadata synchronized underneath.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Families" value={String(familiesQuery.data?.items.length ?? 0)} />
          <Metric label="Versions" value={String(versionsQuery.data?.items.length ?? 0)} />
          <Metric
            label="Compared"
            value={String(comparisonIds.length)}
          />
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel title="Registry Catalog">
          <table className="data-table">
            <thead>
              <tr>
                <th>Version</th>
                <th>Family</th>
                <th>Status</th>
                <th>Metric</th>
                <th>Deployable</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {versionsQuery.data?.items.map((version) => {
                const selected = version.model_version_id === selectedVersionId;
                return (
                  <tr
                    key={version.model_version_id}
                    className={selected ? 'training-row training-row--selected' : 'training-row'}
                    onClick={() => setSelectedVersionId(version.model_version_id)}
                  >
                    <td>
                      <strong>v{version.version_no}</strong>
                    </td>
                    <td>{version.model_family_slug}</td>
                    <td>
                      <span className={`badge ${badgeTone(version.status)}`}>{version.status}</span>
                    </td>
                    <td>
                      {version.primary_metric_name ? (
                        <>
                          <strong>{version.primary_metric_name}</strong>
                          <div className="subtle-text">
                            {formatMetric(version.primary_metric_value)}
                          </div>
                        </>
                      ) : (
                        <span className="subtle-text">No metric</span>
                      )}
                    </td>
                    <td>{version.deployable_for.join(', ') || 'Awaiting approval'}</td>
                    <td onClick={(event) => event.stopPropagation()}>
                      <label className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={comparisonIds.includes(version.model_version_id)}
                          onChange={() => {
                            setComparisonIds((current) =>
                              current.includes(version.model_version_id)
                                ? current.filter((item) => item !== version.model_version_id)
                                : [...current, version.model_version_id].slice(-4)
                            );
                          }}
                        />
                        <span>Compare</span>
                      </label>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>

        <Panel
          title="Selected Version"
          action={
            selectedVersionId ? (
              <div className="actions-row">
                <button
                  type="button"
                  className="button--danger"
                  onClick={() => setCleanupOpen(true)}
                >
                  Cleanup
                </button>
                <button
                  type="button"
                  className="button--danger"
                  onClick={() => {
                    if (
                      confirmConsequentialAction({
                        title: `Archive model version ${selectedVersionId}?`,
                        consequence:
                          'Archived versions stay in lineage history but are removed from normal promotion workflows.'
                      })
                    ) {
                      actionMutation.mutate({
                        kind: 'archive'
                      });
                    }
                  }}
                >
                  Archive
                </button>
                <button
                  type="button"
                  className="button--danger"
                  onClick={() => {
                    if (
                      confirmConsequentialAction({
                        title: `Reject model version ${selectedVersionId}?`,
                        consequence:
                          'Rejected versions remain auditable but should no longer be considered deployable.'
                      })
                    ) {
                      actionMutation.mutate({
                        kind: 'reject'
                      });
                    }
                  }}
                >
                  Reject
                </button>
              </div>
            ) : null
          }
        >
          {!detailQuery.data ? (
            <EmptyState
              title="Choose a model version"
              body="Select a row to inspect immutable lineage, metrics, approvals, and rollout metadata."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <Metric label="Status" value={detailQuery.data.status} />
                <Metric label="Architecture" value={architectureLabel(detailQuery.data.architecture)} />
                <Metric label="Template" value={detailQuery.data.model_template_key ?? 'custom'} />
                <Metric
                  label="Primary Metric"
                  value={
                    detailQuery.data.primary_metric_name
                      ? `${detailQuery.data.primary_metric_name}: ${formatMetric(detailQuery.data.primary_metric_value)}`
                      : 'n/a'
                  }
                />
                <Metric label="Deployable" value={detailQuery.data.deployable_for.join(', ') || 'Awaiting approval'} />
                <Metric label="Promotion Gate" value={promotionGateLabel(detailQuery.data.evaluation_summary)} />
                <Metric label="Promotion Criteria" value={promotionPolicySummary(detailQuery.data.promotion_policy)} />
              </div>

              <details className="advanced-section">
                <summary>Version identifiers and resolved training metadata</summary>
                <div className="detail-grid">
                  <Metric label="Mode" value={detailQuery.data.trainer_mode ?? 'unknown'} />
                  <Metric label="Backend" value={detailQuery.data.trainer_backend ?? 'unknown'} />
                  <Metric label="Snapshot" value={detailQuery.data.dataset_snapshot_id ?? 'n/a'} />
                  <Metric label="Code Revision" value={detailQuery.data.code_revision ?? 'unknown'} />
                  <Metric
                    label="Resolved defaults"
                    value={trainingDefaultsSummary(detailQuery.data.resolved_training_defaults)}
                  />
                  <Metric label="Task type" value={detailQuery.data.architecture.task_type} />
                  <Metric label="Template version" value={String(detailQuery.data.model_template_version ?? 'n/a')} />
                  <Metric
                    label="Artifact Validation"
                    value={String((detailQuery.data.artifact_validation as Record<string, unknown>)?.status ?? 'pending')}
                  />
                </div>
              </details>

              <Panel title="Promotion Gate">
                <p className="subtle-text">
                  {promotionGateDescription(detailQuery.data.evaluation_summary)}
                </p>
                <table className="data-table compact-table">
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Threshold</th>
                      <th>Actual</th>
                      <th>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {promotionResultRows(detailQuery.data.evaluation_summary).map((result) => (
                      <tr key={`${result.metric_name}-${result.phase}`}>
                        <td>{result.metric_name}</td>
                        <td>
                          {result.comparison} {result.threshold}
                        </td>
                        <td>{result.actual_value ?? 'n/a'}</td>
                        <td>{result.passed ? 'passed' : 'failed'}</td>
                      </tr>
                    ))}
                    {!promotionResultRows(detailQuery.data.evaluation_summary).length ? (
                      <tr>
                        <td colSpan={4}>No promotion criteria were evaluated for this version.</td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </Panel>

              <div className="actions-row">
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'approve',
                      targetPath: 'realtime',
                      approvalScope: 'staging'
                    })
                  }
                >
                  Approve RT Staging
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'deploy',
                      targetPath: 'realtime',
                      requestedState: 'staged'
                    })
                  }
                >
                  Stage RT
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'approve',
                      targetPath: 'realtime',
                      approvalScope: 'production'
                    })
                  }
                >
                  Approve RT Production
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'deploy',
                      targetPath: 'realtime',
                      requestedState: 'production'
                    })
                  }
                >
                  Promote RT
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'approve',
                      targetPath: 'batch',
                      approvalScope: 'staging'
                    })
                  }
                >
                  Approve Batch Staging
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'deploy',
                      targetPath: 'batch',
                      requestedState: 'staged'
                    })
                  }
                >
                  Stage Batch
                </button>
              </div>
              <details className="advanced-section">
                <summary>Rollback and lifecycle markers</summary>
                <div className="actions-row">
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'rollback-target',
                      targetPath: 'realtime',
                      enabled: true
                    })
                  }
                >
                  Mark RT Rollback
                </button>
                <button
                  type="button"
                  onClick={() =>
                    actionMutation.mutate({
                      kind: 'rollback-target',
                      targetPath: 'batch',
                      enabled: true
                    })
                  }
                >
                  Mark Batch Rollback
                </button>
                </div>
              </details>

              <details className="advanced-section">
                <summary>Lineage, approvals, metrics, and manifests</summary>
                <div className="detail-grid">
                  <Panel title="Lineage">
                    <div className="meta-grid">
                      <Metric label="Training Job" value={detailQuery.data.lineage.training_job_id ?? 'n/a'} />
                      <Metric label="MLflow Run" value={detailQuery.data.lineage.mlflow_run_id ?? 'n/a'} />
                      <Metric label="Dataset Snapshot" value={detailQuery.data.lineage.dataset_snapshot_id ?? 'n/a'} />
                      <Metric
                        label="DVC Bundle"
                        value={String(detailQuery.data.lineage.dvc_references.bundle_dvc_file ?? 'n/a')}
                      />
                    </div>
                  </Panel>

                  <Panel title="Approvals">
                    {detailQuery.data.approvals.length === 0 ? (
                      <p className="subtle-text">No approval records captured yet.</p>
                    ) : (
                      <table className="data-table compact-table">
                        <thead>
                          <tr>
                            <th>Target</th>
                            <th>Scope</th>
                            <th>Status</th>
                            <th>Actor</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detailQuery.data.approvals.map((approval) => (
                            <tr key={approval.approval_record_id}>
                              <td>{approval.target_path}</td>
                              <td>{approval.approval_scope}</td>
                              <td>{approval.status}</td>
                              <td>{approval.decided_by ?? 'system'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </Panel>
                </div>

                <div className="detail-grid">
                  <Panel title="Promotions">
                    {detailQuery.data.promotions.length === 0 ? (
                      <p className="subtle-text">No target-specific promotions recorded yet.</p>
                    ) : (
                      <table className="data-table compact-table">
                        <thead>
                          <tr>
                            <th>Target</th>
                            <th>Status</th>
                            <th>Rollback Candidate</th>
                            <th>Release</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detailQuery.data.promotions.map((promotion) => (
                            <tr key={promotion.promotion_id}>
                              <td>{promotion.target_path}</td>
                              <td>{promotion.status}</td>
                              <td>{promotion.rollback_candidate ? 'Yes' : 'No'}</td>
                              <td>{promotion.deployment_release_id ?? 'n/a'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </Panel>

                  <Panel title="Metrics">
                    <table className="data-table compact-table">
                      <thead>
                        <tr>
                          <th>Name</th>
                          <th>Value</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(detailQuery.data.metrics_summary).map(([metricName, metricValue]) => (
                          <tr key={metricName}>
                            <td>{metricName}</td>
                            <td>{formatMetric(metricValue)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Panel>
                </div>

                <div className="detail-grid">
                  <Panel title="Artifact Manifest">
                    <pre className="code-block">
                      {JSON.stringify(detailQuery.data.artifact_manifest, null, 2)}
                    </pre>
                  </Panel>
                  <Panel title="Export Manifest">
                    <pre className="code-block">
                      {JSON.stringify(detailQuery.data.export_manifest, null, 2)}
                    </pre>
                  </Panel>
                </div>

                <Panel title="Immutable Metadata Fingerprint">
                  <div className="detail-grid">
                    <Metric
                      label="SHA-256"
                      value={detailQuery.data.immutable_metadata_sha256 ?? 'not recorded'}
                    />
                    <Metric label="MLflow Model" value={detailQuery.data.mlflow_registered_model ?? 'n/a'} />
                    <Metric label="MLflow Version" value={detailQuery.data.mlflow_model_version ?? 'n/a'} />
                  </div>
                  <pre className="code-block">
                    {JSON.stringify(detailQuery.data.immutable_metadata, null, 2)}
                  </pre>
                </Panel>
              </details>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Comparison Workspace">
        {compareQuery.data ? (
          <div className="panel-stack">
            <div className="detail-grid">
              {compareQuery.data.versions.map((version) => (
                <Metric
                  key={version.model_version_id}
                  label={`${version.model_family_slug} v${version.version_no}`}
                  value={version.status}
                />
              ))}
            </div>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Metric</th>
                  {compareQuery.data.versions.map((version) => (
                    <th key={version.model_version_id}>{version.model_version_id}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {compareQuery.data.metrics.map((metric) => (
                  <tr key={metric.metric_name}>
                    <td>{metric.metric_name}</td>
                    {compareQuery.data.versions.map((version) => (
                      <td key={`${metric.metric_name}-${version.model_version_id}`}>
                        {formatMetric(metric.values[version.model_version_id])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Lineage Field</th>
                  {compareQuery.data.versions.map((version) => (
                    <th key={`lineage-${version.model_version_id}`}>{version.model_version_id}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {compareQuery.data.lineage_fields.map((field) => (
                  <tr key={field.field_name}>
                    <td>{field.field_name}</td>
                    {compareQuery.data.versions.map((version) => (
                      <td key={`${field.field_name}-${version.model_version_id}`}>
                        {field.values[version.model_version_id] ?? 'n/a'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="Select versions to compare"
            body="Choose at least two model versions from the catalog table to compare metrics and lineage."
          />
        )}
      </Panel>

      <CleanupDialog
        open={cleanupOpen}
        title="Cleanup model version"
        preview={cleanupPreviewQuery.data}
        pending={cleanupMutation.isPending}
        onClose={() => setCleanupOpen(false)}
        onConfirm={(payload) => cleanupMutation.mutate(payload)}
      />
    </div>
  );
}

function Metric(props: { label: string; value: string }) {
  return (
    <div className="metric-pill">
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

function formatMetric(value: number | null | undefined) {
  return typeof value === 'number' ? value.toFixed(4) : 'n/a';
}

function promotionGateLabel(evaluationSummary: Record<string, unknown>) {
  return evaluationSummary.promotion_ready === true ? 'Passed' : 'Failed or pending';
}

function promotionGateDescription(evaluationSummary: Record<string, unknown>) {
  if (evaluationSummary.promotion_ready === true) {
    return 'This version passed the resolved model-level promotion criteria and can move through approved deployment flows.';
  }
  return 'This version did not pass the resolved promotion criteria, or evaluation has not produced gate results yet.';
}

function promotionResultRows(evaluationSummary: Record<string, unknown>) {
  const rows = evaluationSummary.promotion_results;
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows.map((row) => {
    const payload = row as Record<string, unknown>;
    return {
      metric_name: String(payload.metric_name ?? 'metric'),
      comparison: String(payload.comparison ?? 'gte'),
      threshold: Number(payload.threshold ?? 0),
      phase: String(payload.phase ?? 'evaluation'),
      actual_value: typeof payload.actual_value === 'number' ? payload.actual_value : null,
      passed: payload.passed === true
    };
  });
}

function badgeTone(status: string) {
  if (status === 'production' || status === 'validated') {
    return 'badge--success';
  }
  if (status === 'rejected' || status === 'archived') {
    return 'badge--danger';
  }
  if (status === 'staged') {
    return 'badge--warn';
  }
  return 'badge--neutral';
}

function actionLabel(action: RegistryAction) {
  if (action.kind === 'approve') {
    return `${action.targetPath} ${action.approvalScope} approved`;
  }
  if (action.kind === 'deploy') {
    return `${action.targetPath} ${action.requestedState} workflow queued`;
  }
  if (action.kind === 'rollback-target') {
    return action.enabled ? `${action.targetPath} rollback target marked` : 'Rollback target removed';
  }
  return action.kind === 'archive' ? 'Model version archived' : 'Model version rejected';
}
