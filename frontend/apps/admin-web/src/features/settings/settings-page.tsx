import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { useNotifications } from '../../components/notification-provider';

export function SettingsPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [labelStudioToken, setLabelStudioToken] = useState('');
  const settingsQuery = useQuery({
    queryKey: ['integration-status'],
    queryFn: api.admin.integrationStatus,
    refetchInterval: 15000
  });
  const updateLabelStudioTokenMutation = useMutation({
    mutationFn: () => api.admin.updateLabelStudioToken({ token: labelStudioToken }),
    onSuccess: async (response) => {
      setLabelStudioToken('');
      notify({
        title: 'Label Studio token saved',
        description: response.credentials.message,
        tone: 'success'
      });
      await queryClient.invalidateQueries({ queryKey: ['integration-status'] });
    },
    onError: (error) => {
      notify({
        title: 'Label Studio token rejected',
        description: error instanceof Error ? error.message : 'The token could not be saved.',
        tone: 'error'
      });
    }
  });
  const refreshProjectsMutation = useMutation({
    mutationFn: api.pipelines.refresh,
    onSuccess: async () => {
      notify({
        title: 'Project refresh queued',
        description: 'The dataset worker will reconcile Label Studio projects.',
        tone: 'success'
      });
      await queryClient.invalidateQueries({ queryKey: ['project-pipelines'] });
    },
    onError: (error) => {
      notify({
        title: 'Project refresh failed',
        description: error instanceof Error ? error.message : 'The refresh request was not accepted.',
        tone: 'error'
      });
    }
  });

  if (!settingsQuery.data) {
    return (
      <Panel title="Settings And Integration Status">
        <EmptyState
          title="Loading platform settings"
          body="The control plane is gathering integration reachability, storage posture, and deployment context."
        />
      </Panel>
    );
  }

  const data = settingsQuery.data;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Settings And Integration Status</p>
          <h2>Inspect the environment, dependency connectivity, storage posture, and operator access model.</h2>
          <p className="subtle-text">
            This page is designed for both operators and engineers: it highlights what is broken,
            why it matters operationally, and which release targets are currently active.
          </p>
        </div>
      </section>

      <div className="detail-grid">
        <Metric label="Environment" value={data.environment} />
        <Metric label="Public Base URL" value={data.public_base_url} />
        <Metric label="Session Cookie" value={data.auth.cookie_name} />
        <Metric
          label="Current User"
          value={data.auth.current_user?.display_name ?? data.auth.current_user?.username ?? 'unknown'}
        />
      </div>

      <Panel title="Integration Reachability">
        <table className="data-table">
          <thead>
            <tr>
              <th>Integration</th>
              <th>Category</th>
              <th>Status</th>
              <th>Endpoint</th>
              <th>Detail</th>
              <th>Remediation</th>
            </tr>
          </thead>
          <tbody>
            {data.integrations.map((item) => (
              <tr key={item.name}>
                <td>{item.name}</td>
                <td>{item.category}</td>
                <td>
                  <span className={`badge badge--${tone(item.state)}`}>{item.state}</span>
                </td>
                <td>{item.endpoint ?? 'n/a'}</td>
                <td>{item.detail}</td>
                <td>{item.remediation_hint ?? 'None'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Label Studio Credentials">
        <div className="detail-grid">
          <Metric
            label="Token Configured"
            value={data.label_studio_credentials.configured ? 'Yes' : 'No'}
          />
          <Metric
            label="Secret Writable"
            value={data.label_studio_credentials.writable ? 'Yes' : 'No'}
          />
          <Metric
            label="Last Verification"
            value={
              data.label_studio_credentials.last_verified === null ||
              data.label_studio_credentials.last_verified === undefined
                ? 'n/a'
                : data.label_studio_credentials.last_verified
                  ? 'passed'
                  : 'failed'
            }
          />
          <Metric
            label="Project Count"
            value={
              data.label_studio_credentials.project_count === null ||
              data.label_studio_credentials.project_count === undefined
                ? 'n/a'
                : String(data.label_studio_credentials.project_count)
            }
          />
        </div>
        <form
          className="training-form-grid"
          style={{ marginTop: '1rem' }}
          onSubmit={(event) => {
            event.preventDefault();
            updateLabelStudioTokenMutation.mutate();
          }}
        >
          <label className="training-form-grid__full">
            <span>API Token</span>
            <input
              type="password"
              value={labelStudioToken}
              autoComplete="off"
              onChange={(event) => setLabelStudioToken(event.target.value)}
            />
          </label>
          <div className="actions-row training-form-grid__full">
            <button
              type="submit"
              className="button--primary"
              disabled={
                !labelStudioToken.trim() ||
                !data.label_studio_credentials.writable ||
                updateLabelStudioTokenMutation.isPending
              }
            >
              {updateLabelStudioTokenMutation.isPending ? 'Saving...' : 'Save & Test'}
            </button>
            <button
              type="button"
              onClick={() => refreshProjectsMutation.mutate()}
              disabled={!data.label_studio_credentials.configured || refreshProjectsMutation.isPending}
            >
              {refreshProjectsMutation.isPending ? 'Refreshing...' : 'Refresh Projects'}
            </button>
          </div>
        </form>
        <p className="subtle-text" style={{ marginTop: '0.75rem' }}>
          {data.label_studio_credentials.message}
        </p>
      </Panel>

      <div className="training-grid training-grid--details">
        <Panel title="Active Deployment Context">
          <table className="data-table">
            <thead>
              <tr>
                <th>Target</th>
                <th>Active Release</th>
                <th>Previous Release</th>
                <th>Active Model</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.deployment_targets.map((target) => (
                <tr key={target.target_path}>
                  <td>{target.target_path}</td>
                  <td>{target.active_release_id ?? 'none'}</td>
                  <td>{target.previous_release_id ?? 'none'}</td>
                  <td>{target.active_model_version_id ?? 'none'}</td>
                  <td>{target.active_release_status ?? 'n/a'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Storage Paths">
          <table className="data-table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Path</th>
                <th>Exists</th>
                <th>Writable</th>
              </tr>
            </thead>
            <tbody>
              {data.storage_paths.map((item) => (
                <tr key={item.key}>
                  <td>{item.label}</td>
                  <td>{item.path}</td>
                  <td>{item.exists ? 'Yes' : 'No'}</td>
                  <td>{item.writable ? 'Yes' : 'No'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <Panel title="Auth And Feature Flags">
        <div className="detail-grid">
          <Metric label="Secure Cookie" value={String(data.auth.secure_cookie)} />
          <Metric label="Session TTL" value={`${data.auth.session_max_age_seconds}s`} />
          <Metric label="Admin Access" value="authenticated users" />
          <Metric label="API Key Auth" value={data.auth.api_key_auth_enabled ? 'enabled' : 'disabled'} />
          <Metric label="Bootstrap Keys" value={String(data.auth.api_key_bootstrap_count)} />
          <Metric label="Webhook Auth" value={String(data.feature_flags.webhook_auth_enabled)} />
          <Metric label="Training Start Allowed" value={String(data.feature_flags.training_start_allowed)} />
          <Metric
            label="Pause On SLO Pressure"
            value={String(data.feature_flags.training_pause_on_slo_pressure)}
          />
        </div>
        <div className="actions-row" style={{ marginTop: '1rem' }}>
          <Link to="/operations/access" className="nav-link">
            Open Access &amp; Security
          </Link>
        </div>
      </Panel>
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

function tone(state: 'ok' | 'degraded' | 'down') {
  if (state === 'ok') {
    return 'success';
  }
  if (state === 'degraded') {
    return 'warn';
  }
  return 'danger';
}
