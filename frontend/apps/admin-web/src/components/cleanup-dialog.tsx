import { useEffect, useState } from 'react';
import type { CleanupExecutionRequest, CleanupImpactPreview } from '@cv-platform/schemas';

type CleanupDialogProps = {
  open: boolean;
  title: string;
  description?: string;
  preview?: CleanupImpactPreview | null;
  pending?: boolean;
  defaultDeleteArtifacts?: boolean;
  forceDeleteArtifacts?: boolean;
  extraPayload?: Partial<CleanupExecutionRequest>;
  destructiveOptionLabel?: string;
  destructiveOptionDescription?: string;
  destructiveOptionChecked?: boolean;
  onDestructiveOptionChange?: (checked: boolean) => void;
  acknowledgementLabel?: string;
  confirmLabel?: string;
  pendingLabel?: string;
  onClose: () => void;
  onConfirm: (payload: CleanupExecutionRequest) => void;
};

export function CleanupDialog(props: CleanupDialogProps) {
  const [reason, setReason] = useState('');
  const [deleteArtifacts, setDeleteArtifacts] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);

  useEffect(() => {
    setReason('');
    setDeleteArtifacts(
      props.open ? Boolean(props.defaultDeleteArtifacts || props.forceDeleteArtifacts) : false
    );
    setAcknowledged(false);
  }, [props.open, props.defaultDeleteArtifacts, props.forceDeleteArtifacts]);

  if (!props.open) {
    return null;
  }

  const acknowledgementLabel =
    props.acknowledgementLabel ?? 'I understand this cleanup removes the selected platform records.';
  const confirmLabel = props.confirmLabel ?? 'Confirm cleanup';
  const pendingLabel = props.pendingLabel ?? 'Removing...';
  const description =
    props.description ??
    'Review the cleanup impact before removing metadata or managed artifacts. Audit history is retained and affected records are marked deleted.';

  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal-panel modal-panel--wide" onClick={(event) => event.stopPropagation()}>
        <div className="panel__header">
          <div>
            <h2>{props.title}</h2>
            <p className="subtle-text">{description}</p>
          </div>
          <button type="button" onClick={props.onClose}>
            Close
          </button>
        </div>

        {!props.preview ? (
          <p className="subtle-text">Loading cleanup impact...</p>
        ) : (
          <div className="panel-stack">
            <div className="detail-grid">
              <div className="metric-pill">
                <span>Scope</span>
                <strong>{props.preview.display_name}</strong>
                <small className="subtle-text">{props.preview.scope_type}</small>
              </div>
              <div className="metric-pill">
                <span>Entities removed</span>
                <strong>{String(props.preview.will_remove.reduce((sum, item) => sum + item.count, 0))}</strong>
                <small className="subtle-text">Metadata rows and related records</small>
              </div>
              <div className="metric-pill">
                <span>Artifacts</span>
                <strong>{String(props.preview.artifact_paths.length)}</strong>
                <small className="subtle-text">Managed files eligible for cleanup</small>
              </div>
              <div className={`metric-pill metric-pill--${props.preview.blocked ? 'danger' : 'warn'}`}>
                <span>Execution</span>
                <strong>{props.preview.blocked ? 'Blocked' : 'Ready with confirmation'}</strong>
                <small className="subtle-text">
                  {props.preview.active_deployment_targets.length
                    ? `Active targets: ${props.preview.active_deployment_targets.join(', ')}`
                    : 'No active deployment target is directly impacted.'}
                </small>
              </div>
            </div>

            {props.preview.blocked_reasons.length ? (
              <div className="inline-alert inline-alert--danger">
                <strong>Cleanup is currently blocked</strong>
                <ul className="result-card__list">
                  {props.preview.blocked_reasons.map((reasonItem) => (
                    <li key={reasonItem}>{reasonItem}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {props.preview.warnings.length ? (
              <div className="inline-alert inline-alert--warn">
                <strong>Review warnings carefully</strong>
                <ul className="result-card__list">
                  {props.preview.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="training-inline-grid">
              <div className="cleanup-section">
                <h3>Will Remove</h3>
                {props.preview.will_remove.length ? (
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Scope</th>
                        <th>Name</th>
                        <th>Count</th>
                      </tr>
                    </thead>
                    <tbody>
                      {props.preview.will_remove.map((item) => (
                        <tr key={`${item.scope_type}-${item.scope_id}`}>
                          <td>{item.scope_type}</td>
                          <td>{item.display_name}</td>
                          <td>{item.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="subtle-text">No removable metadata was reported.</p>
                )}
              </div>

              <div className="cleanup-section">
                <h3>Will Remain</h3>
                {props.preview.will_remain.length ? (
                  <ul className="result-card__list">
                    {props.preview.will_remain.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="subtle-text">No retained side effects were reported.</p>
                )}
              </div>
            </div>

            {props.preview.artifact_paths.length ? (
              <div className="cleanup-section">
                <h3>Managed Artifacts</h3>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Category</th>
                      <th>Path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {props.preview.artifact_paths.map((artifact) => (
                      <tr key={`${artifact.category}-${artifact.path}`}>
                        <td>{artifact.category}</td>
                        <td>{artifact.path}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}

            <form
              className="panel-stack"
              onSubmit={(event) => {
                event.preventDefault();
                if (!props.preview || props.preview.blocked || !acknowledged) {
                  return;
                }
                props.onConfirm({
                  confirmation_token: props.preview.confirmation_token,
                  reason: reason.trim() || undefined,
                  delete_artifacts: props.forceDeleteArtifacts ? true : deleteArtifacts,
                  ...(props.extraPayload ?? {})
                });
              }}
            >
              {props.destructiveOptionLabel ? (
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={Boolean(props.destructiveOptionChecked)}
                    onChange={(event) => props.onDestructiveOptionChange?.(event.target.checked)}
                  />
                  <span>
                    {props.destructiveOptionLabel}
                    {props.destructiveOptionDescription ? (
                      <small className="subtle-text">{props.destructiveOptionDescription}</small>
                    ) : null}
                  </span>
                </label>
              ) : null}
              <label className="training-form-grid__full">
                <span>Operator note</span>
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Optional reason for the cleanup action"
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={props.forceDeleteArtifacts ? true : deleteArtifacts}
                  onChange={(event) => setDeleteArtifacts(event.target.checked)}
                  disabled={props.forceDeleteArtifacts || !props.preview.artifact_paths.length}
                />
                <span>
                  {props.forceDeleteArtifacts
                    ? 'Managed artifacts will be deleted for this cleanup.'
                    : 'Also delete managed artifacts where policy allows.'}
                </span>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                <span>{acknowledgementLabel}</span>
              </label>
              <div className="actions-row">
                <button
                  type="submit"
                  disabled={props.pending || !props.preview || props.preview.blocked || !acknowledged}
                >
                  {props.pending ? pendingLabel : confirmLabel}
                </button>
                <button type="button" onClick={props.onClose}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
