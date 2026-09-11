import { Link, useParams } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { statusTone } from '../datasets/presentation';

export function ValidationReportPage() {
  const { snapshotId } = useParams({ from: '/authenticated/datasets/snapshots/$snapshotId/validation' });
  const validationQuery = useQuery({
    queryKey: ['snapshot-validation', snapshotId],
    queryFn: () => api.datasets.snapshotValidation(snapshotId),
    refetchInterval: 10000
  });

  const report = validationQuery.data;

  return (
    <div className="panel-stack">
      <Panel
        title="Validation Report"
        action={
          <Link className="primary-link" to="/datasets/snapshots/$snapshotId" params={{ snapshotId }}>
            Snapshot Details
          </Link>
        }
      >
        {report ? (
          <div className="panel-stack">
            <div className="meta-grid">
              <div>
                <span className="subtle-text">Status</span>
                <strong className={`badge badge--${statusTone(report.status)}`}>{report.status}</strong>
              </div>
              <div>
                <span className="subtle-text">Checked At</span>
                <strong>{report.checked_at ?? 'Waiting for validation'}</strong>
              </div>
              <div>
                <span className="subtle-text">Errors</span>
                <strong>{report.error_count}</strong>
              </div>
              <div>
                <span className="subtle-text">Warnings</span>
                <strong>{report.warning_count}</strong>
              </div>
            </div>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Check</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(report.checks).map(([check, passed]) => (
                  <tr key={check}>
                    <td>{check}</td>
                    <td>
                      <span className={`badge badge--${passed ? 'success' : 'danger'}`}>
                        {passed ? 'pass' : 'fail'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Severity</th>
                  <th>Code</th>
                  <th>Message</th>
                  <th>Item</th>
                </tr>
              </thead>
              <tbody>
                {report.issues.map((issue, index) => (
                  <tr key={`${issue.code}-${index}`}>
                    <td>
                      <span className={`badge badge--${issue.severity === 'error' ? 'danger' : 'warn'}`}>
                        {issue.severity}
                      </span>
                    </td>
                    <td>{issue.code}</td>
                    <td>{issue.message}</td>
                    <td>{issue.item_ref ?? 'n/a'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="subtle-text">Loading validation report...</p>
        )}
      </Panel>
    </div>
  );
}
