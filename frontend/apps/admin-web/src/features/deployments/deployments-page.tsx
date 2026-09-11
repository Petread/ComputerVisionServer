import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type { TargetPath } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import { architectureLabel, promotionPolicySummary } from '../pipelines/modeling';

export function DeploymentsPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [selectedModelVersionId, setSelectedModelVersionId] = useState<string | null>(null);
  const [selectedReleaseId, setSelectedReleaseId] = useState<string | null>(null);
  const [targetPath, setTargetPath] = useState<TargetPath>('realtime');
  const [cleanupOpen, setCleanupOpen] = useState(false);

  const versionsQuery = useQuery({
    queryKey: ['model-versions'],
    queryFn: () => api.registry.versions()
  });
  const packagesQuery = useQuery({
    queryKey: ['deployment-packages'],
    queryFn: api.deployments.packages
  });
  const targetsQuery = useQuery({
    queryKey: ['deployment-targets'],
    queryFn: api.deployments.targets
  });
  const releasesQuery = useQuery({
    queryKey: ['deployment-releases'],
    queryFn: api.deployments.releases
  });

  const approvedVersions = useMemo(
    () => versionsQuery.data?.items.filter((version) => version.deployable_for.length > 0) ?? [],
    [versionsQuery.data?.items]
  );

  useEffect(() => {
    if (!selectedModelVersionId && approvedVersions[0]) {
      setSelectedModelVersionId(approvedVersions[0].model_version_id);
    }
  }, [approvedVersions, selectedModelVersionId]);

  useEffect(() => {
    if (!selectedReleaseId && releasesQuery.data?.items[0]) {
      setSelectedReleaseId(releasesQuery.data.items[0].deployment_release_id);
    }
  }, [releasesQuery.data, selectedReleaseId]);

  const packageDetailQuery = useQuery({
    queryKey: ['deployment-package-detail', selectedModelVersionId],
    queryFn: () => api.deployments.packageDetail(selectedModelVersionId as string),
    enabled: Boolean(selectedModelVersionId)
  });
  const selectedVersionDetailQuery = useQuery({
    queryKey: ['model-version-detail', selectedModelVersionId],
    queryFn: () => api.registry.detail(selectedModelVersionId as string),
    enabled: Boolean(selectedModelVersionId)
  });
  const releaseDetailQuery = useQuery({
    queryKey: ['deployment-release-detail', selectedReleaseId],
    queryFn: () => api.deployments.releaseDetail(selectedReleaseId as string),
    enabled: Boolean(selectedReleaseId)
  });
  const cleanupPreviewQuery = useQuery({
    queryKey: ['deployment-release-cleanup-preview', selectedReleaseId],
    queryFn: () => api.deployments.cleanupPreview(selectedReleaseId as string),
    enabled: cleanupOpen && Boolean(selectedReleaseId)
  });

  const invalidateDeploymentViews = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['deployment-packages'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-package-detail'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-releases'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-release-detail'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-targets'] }),
      queryClient.invalidateQueries({ queryKey: ['model-versions'] })
    ]);
  };

  const buildPackageMutation = useMutation({
    mutationFn: () =>
      api.deployments.buildPackage({
        model_version_id: selectedModelVersionId as string
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: 'Deployment package build queued', tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Deployment package build failed'), tone: 'error' });
    }
  });

  const stageMutation = useMutation({
    mutationFn: () =>
      api.deployments.promote({
        model_version_id: selectedModelVersionId as string,
        target_path: targetPath,
        requested_state: 'staged'
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: `${targetPath} staging queued`, tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Stage request failed'), tone: 'error' });
    }
  });

  const promoteMutation = useMutation({
    mutationFn: () =>
      api.deployments.promote({
        model_version_id: selectedModelVersionId as string,
        target_path: targetPath,
        requested_state: 'production'
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: `${targetPath} production promotion queued`, tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Promotion failed'), tone: 'error' });
    }
  });

  const smokeMutation = useMutation({
    mutationFn: () =>
      api.deployments.smokeTest(selectedReleaseId as string, {
        reason: 'Smoke test triggered from deployment UI.'
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: 'Smoke test queued', tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Smoke test failed'), tone: 'error' });
    }
  });

  const deactivateModelMutation = useMutation({
    mutationFn: () =>
      api.deployments.deactivateModel(selectedReleaseId as string, {
        reason: 'Model serving deactivated from deployment UI.'
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: 'Model deactivation queued', tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Model deactivation failed'), tone: 'error' });
    }
  });

  const activateModelMutation = useMutation({
    mutationFn: () =>
      api.deployments.activateModel(selectedReleaseId as string, {
        reason: 'Model serving activated from deployment UI.'
      }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: 'Model activation queued', tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Model activation failed'), tone: 'error' });
    }
  });

  const rollbackMutation = useMutation({
    mutationFn: (rollbackTargetPath: TargetPath) =>
      api.deployments.rollback(rollbackTargetPath, { reason: 'Rollback requested from deployment UI.' }),
    onSuccess: async () => {
      await invalidateDeploymentViews();
      notify({ title: 'Rollback queued', tone: 'success' });
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Rollback failed'), tone: 'error' });
    }
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: { confirmation_token: string; reason?: string | null; delete_artifacts?: boolean }) =>
      api.deployments.cleanup(selectedReleaseId as string, payload),
    onSuccess: async (result) => {
      await invalidateDeploymentViews();
      notify({
        title: result.already_deleted ? 'Release already removed' : 'Release cleanup completed',
        description: `${result.deleted_entities.reduce((sum, item) => sum + item.count, 0)} entities affected.`,
        tone: 'success'
      });
      setCleanupOpen(false);
    },
    onError: (error: Error) => {
      notify({ ...describeError(error, 'Release cleanup failed'), tone: 'error' });
    }
  });

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Deployments</p>
          <h2>Build, stage, promote, or roll back an approved model version.</h2>
          <p className="subtle-text">
            The standard flow is package build, target staging, production promotion, and clear rollback.
            Manifests, Triton names, and reports remain available in Advanced details.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Approved Versions" value={String(approvedVersions.length)} />
          <Metric label="Packages" value={String(packagesQuery.data?.items.length ?? 0)} />
          <Metric label="Releases" value={String(releasesQuery.data?.items.length ?? 0)} />
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel title="Approved Model Versions">
          {approvedVersions.length === 0 ? (
            <EmptyState
              title="No approved versions"
              body="Approve a model for staging or production in the registry before building deployment packages."
            />
          ) : (
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Family</th>
                  <th>Quality Gate</th>
                  <th>Deployable</th>
                  <th>Package</th>
                  <th>Built</th>
                </tr>
              </thead>
              <tbody>
                {approvedVersions.map((version) => {
                  const packageSummary = packagesQuery.data?.items.find(
                    (item) => item.model_version_id === version.model_version_id
                  );
                  const selected = version.model_version_id === selectedModelVersionId;
                  return (
                    <tr
                      key={version.model_version_id}
                      className={selected ? 'training-row training-row--selected' : 'training-row'}
                      onClick={() => setSelectedModelVersionId(version.model_version_id)}
                    >
                      <td>
                        <strong>v{version.version_no}</strong>
                      </td>
                      <td>{version.model_family_slug}</td>
                      <td>{version.deployable_for.length ? 'passed' : 'pending'}</td>
                      <td>{version.deployable_for.join(', ')}</td>
                      <td>{packageSummary?.status ?? 'missing'}</td>
                      <td>{packageSummary?.built_at ?? 'n/a'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel
          title="Deployment Package"
          action={
            selectedModelVersionId ? (
              <div className="actions-row">
                <select
                  value={targetPath}
                  onChange={(event) => setTargetPath(event.target.value as TargetPath)}
                >
                  <option value="realtime">Realtime</option>
                  <option value="batch">Batch</option>
                </select>
                <button type="button" onClick={() => buildPackageMutation.mutate()}>
                  Build Package
                </button>
                <button type="button" onClick={() => stageMutation.mutate()}>
                  Stage {targetPath === 'realtime' ? 'RT' : 'Batch'}
                </button>
                <button
                  type="button"
                  className="button--primary"
                  onClick={() => {
                    if (
                      confirmConsequentialAction({
                        title: `Promote ${targetPath} to production?`,
                        consequence:
                          'This switches the active production pointer to the selected immutable release and changes live traffic routing.',
                        secondary:
                          'Use rollback only if the previously active release is still present and healthy.'
                      })
                    ) {
                      promoteMutation.mutate();
                    }
                  }}
                >
                  Promote {targetPath === 'realtime' ? 'RT' : 'Batch'}
                </button>
              </div>
            ) : null
          }
        >
          {!packageDetailQuery.data ? (
            <EmptyState
              title="Choose an approved version"
              body="Select an approved model version to build its immutable deployment package and inspect the generated manifests."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <Metric label="Status" value={packageDetailQuery.data.status} />
                <Metric label="Architecture" value={architectureLabel(packageDetailQuery.data.architecture)} />
                <Metric label="Targets" value={packageDetailQuery.data.compatible_targets.join(', ') || 'n/a'} />
                <Metric label="Built" value={packageDetailQuery.data.built_at ?? 'n/a'} />
                <Metric
                  label="Promotion Gate"
                  value={promotionGateLabel(selectedVersionDetailQuery.data?.evaluation_summary)}
                />
                <Metric
                  label="Promotion Criteria"
                  value={promotionPolicySummary(selectedVersionDetailQuery.data?.promotion_policy)}
                />
              </div>

              {selectedVersionDetailQuery.data ? (
                <div className="inline-alert inline-alert--info">
                  <strong>Deployment readiness</strong>
                  <p className="subtle-text">
                    {promotionGateDescription(selectedVersionDetailQuery.data.evaluation_summary)}
                  </p>
                </div>
              ) : null}

              {packageDetailQuery.data.error ? (
                <p className="subtle-text">Last package error: {packageDetailQuery.data.error}</p>
              ) : null}

              <details className="advanced-section">
                <summary>Package manifests and technical metadata</summary>
                <div className="detail-grid">
                  <Metric label="Manifest URI" value={packageDetailQuery.data.serving_manifest_uri ?? 'n/a'} />
                  <Metric
                    label="Trainer Mode"
                    value={String((packageDetailQuery.data.export_manifest as Record<string, unknown>)?.trainer_mode ?? 'unknown')}
                  />
                  <Metric
                    label="Trainer Backend"
                    value={String((packageDetailQuery.data.export_manifest as Record<string, unknown>)?.trainer_backend ?? 'unknown')}
                  />
                  <Metric
                    label="Artifact Validation"
                    value={String(((packageDetailQuery.data.export_manifest as Record<string, unknown>)?.artifact_validation as Record<string, unknown>)?.status ?? 'pending')}
                  />
                </div>
                <div className="detail-grid">
                  <Panel title="Serving Manifest">
                    <pre className="code-block">
                      {JSON.stringify(packageDetailQuery.data.serving_manifest, null, 2)}
                    </pre>
                  </Panel>
                  <Panel title="Export Manifest">
                    <pre className="code-block">
                      {JSON.stringify(packageDetailQuery.data.export_manifest, null, 2)}
                    </pre>
                  </Panel>
                </div>
                <Panel title="Artifact Manifest">
                  <pre className="code-block">
                    {JSON.stringify(packageDetailQuery.data.artifact_manifest, null, 2)}
                  </pre>
                </Panel>
              </details>
            </div>
          )}
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Deployment Targets">
          <table className="data-table compact-table">
            <thead>
              <tr>
                <th>Target</th>
                <th>Status</th>
                <th>Updated</th>
                <th>Rollback</th>
              </tr>
            </thead>
            <tbody>
              {targetsQuery.data?.items.map((target) => (
                <tr key={target.target_path}>
                  <td>{target.target_path}</td>
                  <td>{target.active_release_status ?? 'n/a'}</td>
                  <td>{target.updated_at ?? 'n/a'}</td>
                  <td>
                    <button
                      type="button"
                      className="button--danger"
                      onClick={() => {
                        if (
                          confirmConsequentialAction({
                            title: `Rollback ${target.target_path}?`,
                            consequence:
                              'Traffic will switch back to the previously active immutable release for this target if it is still available.'
                          })
                        ) {
                          rollbackMutation.mutate(target.target_path);
                        }
                      }}
                    >
                      Rollback
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <details className="advanced-section">
            <summary>Release identifiers</summary>
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Active Release</th>
                  <th>Previous Release</th>
                  <th>Active Model</th>
                </tr>
              </thead>
              <tbody>
                {targetsQuery.data?.items.map((target) => (
                  <tr key={`${target.target_path}-ids`}>
                    <td>{target.target_path}</td>
                    <td>{target.active_release_id ?? 'n/a'}</td>
                    <td>{target.previous_release_id ?? 'n/a'}</td>
                    <td>{target.active_model_version_id ?? 'n/a'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </Panel>

        <Panel
          title="Deployment Releases"
          action={
            selectedReleaseId ? (
              <div className="actions-row">
                <button type="button" onClick={() => smokeMutation.mutate()}>
                  Run Smoke Test
                </button>
                {releaseDetailQuery.data?.serving_enabled ? (
                  <button
                    type="button"
                    className="button--danger"
                    disabled={deactivateModelMutation.isPending}
                    onClick={() => {
                      if (
                        confirmConsequentialAction({
                          title: `Deactivate ${releaseDetailQuery.data.triton_model_name}?`,
                          consequence:
                            'All versions under this stable Triton model name will be unloaded. Deployment packages, reports, and history remain available.',
                          secondary:
                            'Active production, rollback, batch, or running channel references block this action.'
                        })
                      ) {
                        deactivateModelMutation.mutate();
                      }
                    }}
                  >
                    Deactivate model
                  </button>
                ) : releaseDetailQuery.data?.status === 'deactivated' ? (
                  <button
                    type="button"
                    className="button--primary"
                    disabled={activateModelMutation.isPending}
                    onClick={() => activateModelMutation.mutate()}
                  >
                    Activate model
                  </button>
                ) : null}
                <button type="button" className="button--danger" onClick={() => setCleanupOpen(true)}>
                  Cleanup release
                </button>
              </div>
            ) : null
          }
        >
          {releasesQuery.data?.items.length ? (
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Status</th>
                  <th>Serving</th>
                  <th>Activated</th>
                  <th>Version</th>
                </tr>
              </thead>
              <tbody>
                {releasesQuery.data.items.map((release) => {
                  const selected = release.deployment_release_id === selectedReleaseId;
                  return (
                    <tr
                      key={release.deployment_release_id}
                      className={selected ? 'training-row training-row--selected' : 'training-row'}
                      onClick={() => {
                        setSelectedReleaseId(release.deployment_release_id);
                        setSelectedModelVersionId(release.model_version_id);
                        setTargetPath(release.target_path);
                      }}
                    >
                      <td>{release.target_path}</td>
                      <td>{release.status}</td>
                      <td>{release.serving_enabled ? 'enabled' : 'disabled'}</td>
                      <td>{release.activated_at ?? 'not active'}</td>
                      <td>{release.model_version_id}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No deployment releases yet"
              body="Build a package and stage it to create the first immutable Triton release."
            />
          )}
        </Panel>
      </div>

      <Panel title="Selected Release Detail">
        {!releaseDetailQuery.data ? (
          <EmptyState
            title="Choose a release"
            body="Select a deployment release to inspect the package manifest used, compatibility checks, and smoke report."
          />
        ) : (
          <div className="panel-stack">
            <div className="detail-grid">
              <Metric label="Target" value={releaseDetailQuery.data.target_path} />
              <Metric label="Status" value={releaseDetailQuery.data.status} />
              <Metric
                label="Serving"
                value={releaseDetailQuery.data.serving_enabled ? 'enabled' : 'disabled'}
              />
              <Metric label="Architecture" value={architectureLabel(releaseDetailQuery.data.architecture)} />
              <Metric label="Activated" value={releaseDetailQuery.data.activated_at ?? 'n/a'} />
            </div>

            <details className="advanced-section">
              <summary>Release reports and identifiers</summary>
              <div className="detail-grid">
                <Metric label="Release" value={releaseDetailQuery.data.deployment_release_id} />
                <Metric label="Model Version" value={releaseDetailQuery.data.model_version_id} />
                <Metric
                  label="Triton"
                  value={`${releaseDetailQuery.data.triton_model_name}:${releaseDetailQuery.data.triton_model_version}`}
                />
              </div>
              <div className="detail-grid">
                <Panel title="Compatibility Report">
                  <pre className="code-block">
                    {JSON.stringify(releaseDetailQuery.data.compatibility_report, null, 2)}
                  </pre>
                </Panel>
                <Panel title="Smoke Report">
                  <pre className="code-block">
                    {JSON.stringify(releaseDetailQuery.data.smoke_report, null, 2)}
                  </pre>
                </Panel>
              </div>
              <Panel title="Release Artifact Manifest">
                <pre className="code-block">
                  {JSON.stringify(releaseDetailQuery.data.artifact_manifest, null, 2)}
                </pre>
              </Panel>
            </details>
          </div>
        )}
      </Panel>

      <CleanupDialog
        open={cleanupOpen}
        title="Cleanup deployment release"
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

function promotionGateLabel(evaluationSummary?: Record<string, unknown> | null) {
  if (!evaluationSummary) {
    return 'Loading';
  }
  return evaluationSummary.promotion_ready === true ? 'Passed' : 'Failed or pending';
}

function promotionGateDescription(evaluationSummary: Record<string, unknown>) {
  if (evaluationSummary.promotion_ready === true) {
    return 'This package is tied to a version that passed the model-level promotion criteria.';
  }
  return 'This version did not pass the resolved promotion criteria, so normal production deployment remains blocked.';
}
