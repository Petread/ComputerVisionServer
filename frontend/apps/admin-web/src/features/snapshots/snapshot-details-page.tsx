import { Link, useParams } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import type { CleanupExecutionRequest, CleanupExecutionResult } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import { formatDate, statusTone, stringifyClassDistribution } from '../datasets/presentation';
import { useState } from 'react';

export function SnapshotDetailsPage() {
  const { snapshotId } = useParams({ from: '/authenticated/datasets/snapshots/$snapshotId' });
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [cleanupOpen, setCleanupOpen] = useState(false);

  const snapshotQuery = useQuery({
    queryKey: ['snapshot-detail', snapshotId],
    queryFn: () => api.datasets.snapshotDetail(snapshotId),
    refetchInterval: 10000
  });

  const cleanupPreviewQuery = useQuery({
    queryKey: ['snapshot-cleanup-preview', snapshotId],
    queryFn: () => api.datasets.snapshotCleanupPreview(snapshotId),
    enabled: cleanupOpen
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: CleanupExecutionRequest) => api.datasets.cleanupSnapshot(snapshotId, payload),
    onSuccess: async (result: CleanupExecutionResult) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['snapshot-detail', snapshotId] }),
        queryClient.invalidateQueries({ queryKey: ['all-snapshots'] }),
        queryClient.invalidateQueries({ queryKey: ['project-pipeline'] })
      ]);
      notify({
        title: result.already_deleted ? 'Snapshot already removed' : 'Snapshot cleanup completed',
        description: `${result.deleted_entities.reduce((sum, item) => sum + item.count, 0)} entities affected.`,
        tone: 'success'
      });
      setCleanupOpen(false);
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Snapshot cleanup failed'), tone: 'error' });
    }
  });

  const snapshot = snapshotQuery.data;

  return (
    <div className="panel-stack">
      <Panel
        title="Snapshot Details"
        action={
          snapshot ? (
            <div className="actions-row">
              <button type="button" onClick={() => setCleanupOpen(true)}>
                Cleanup snapshot
              </button>
              <Link
                className="primary-link"
                to="/datasets/snapshots/$snapshotId/validation"
                params={{ snapshotId }}
              >
                Validation Report
              </Link>
            </div>
          ) : null
        }
      >
        {snapshot ? (
          <div className="panel-stack">
            <div className="detail-grid">
              <div className="metric-pill">
                <span>Snapshot</span>
                <strong>#{snapshot.snapshot_no}</strong>
                <small className="subtle-text">{snapshot.snapshot_id}</small>
              </div>
              <div className="metric-pill">
                <span>Status</span>
                <strong className={`badge badge--${statusTone(snapshot.status)}`}>{snapshot.status}</strong>
                <small className="subtle-text">{snapshot.validation_status ?? 'pending validation'}</small>
              </div>
              <div className="metric-pill">
                <span>Export format</span>
                <strong>{snapshot.export_format}</strong>
                <small className="subtle-text">{snapshot.label_schema_version}</small>
              </div>
              <div className="metric-pill">
                <span>Created</span>
                <strong>{formatDate(snapshot.created_at)}</strong>
                <small className="subtle-text">Source captured {formatDate(snapshot.source_captured_at)}</small>
              </div>
            </div>

            <div className="detail-grid">
              <div className="metric-pill">
                <span>Total uploaded</span>
                <strong>{snapshot.item_count}</strong>
                <small className="subtle-text">Raw image count observed during export</small>
              </div>
              <div className="metric-pill">
                <span>Eligible finalized</span>
                <strong>{snapshot.eligible_item_count}</strong>
                <small className="subtle-text">Training-ready submitted items only</small>
              </div>
              <div className="metric-pill">
                <span>Submitted labeled</span>
                <strong>{snapshot.submitted_labeled_item_count}</strong>
                <small className="subtle-text">Normal positive samples</small>
              </div>
              <div className="metric-pill">
                <span>Background images</span>
                <strong>{snapshot.submitted_background_item_count}</strong>
                <small className="subtle-text">Submitted without labels, retained intentionally</small>
              </div>
              <div className="metric-pill">
                <span>Draft / unsubmitted</span>
                <strong>{snapshot.draft_item_count}</strong>
                <small className="subtle-text">Ignored during dataset export and training</small>
              </div>
              <div className="metric-pill">
                <span>Invalid / excluded</span>
                <strong>{snapshot.invalid_item_count}</strong>
                <small className="subtle-text">Broken items reported in validation output</small>
              </div>
            </div>

            {snapshot.processing_error ? (
              <div className="inline-alert inline-alert--danger">{snapshot.processing_error}</div>
            ) : null}

            <div className="detail-grid">
              <article className="status-card">
                <h3>Checksum</h3>
                <p className="subtle-text">{snapshot.content_checksum ?? 'Not computed yet'}</p>
              </article>
              <article className="status-card">
                <h3>Source Project</h3>
                <p className="subtle-text">
                  {snapshot.source_project_title ?? 'Unknown'} (LS #{snapshot.source_label_studio_project_id ?? 'n/a'})
                </p>
              </article>
              <article className="status-card">
                <h3>DVC Revision</h3>
                <p className="subtle-text">{snapshot.dvc_rev ?? 'Pending tracking'}</p>
              </article>
              <article className="status-card">
                <h3>Split Integrity</h3>
                <p className="subtle-text">
                  train {snapshot.split_metadata.train_count}, val {snapshot.split_metadata.val_count}, test{' '}
                  {snapshot.split_metadata.test_count}
                </p>
              </article>
            </div>

            <div className="detail-grid">
              <article className="status-card">
                <h3>Validation Summary</h3>
                <p className="subtle-text">
                  {snapshot.validation_report.error_count} errors, {snapshot.validation_report.warning_count} warnings
                </p>
                <strong>{snapshot.validation_report.status}</strong>
              </article>
              <article className="status-card">
                <h3>Class Distribution</h3>
                <p className="subtle-text">
                  {stringifyClassDistribution(
                    (snapshot.statistics.class_distribution as Record<string, number>) ?? {}
                  )}
                </p>
              </article>
              <article className="status-card">
                <h3>Comparison</h3>
                <p className="subtle-text">
                  Previous snapshot: {snapshot.comparison?.previous_snapshot_id ?? 'None'}
                </p>
                <strong>
                  Delta annotations {snapshot.comparison?.annotation_delta ?? 0}, Delta items {snapshot.comparison?.item_delta ?? 0}
                </strong>
              </article>
            </div>
          </div>
        ) : (
          <p className="subtle-text">Loading snapshot metadata...</p>
        )}
      </Panel>

      <CleanupDialog
        open={cleanupOpen}
        title="Cleanup dataset snapshot"
        preview={cleanupPreviewQuery.data}
        pending={cleanupMutation.isPending}
        onClose={() => setCleanupOpen(false)}
        onConfirm={(payload) => cleanupMutation.mutate(payload)}
      />
    </div>
  );
}
