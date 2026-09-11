import type { ReactNode } from 'react';
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  ApiKeyCreateRequest,
  ApiKeyCreateResponse,
  ApiKeyDetail,
  ApiKeySummary,
  PlcWriteAccessConnectionSummary,
  UserDetail
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';

type UserFormState = {
  username: string;
  displayName: string;
  description: string;
  password: string;
};

type UserEditState = {
  displayName: string;
  description: string;
};

type KeyFormState = {
  label: string;
  description: string;
  intendedUsage: string;
  subjectName: string;
  subjectType: string;
  scopes: string[];
  endpointModeHint: '' | 'realtime' | 'batch' | 'both';
  expiresAt: string;
};

const defaultUserForm = (): UserFormState => ({
  username: '',
  displayName: '',
  description: '',
  password: ''
});

const defaultUserEdit = (): UserEditState => ({
  displayName: '',
  description: ''
});

const defaultKeyForm = (): KeyFormState => ({
  label: '',
  description: '',
  intendedUsage: '',
  subjectName: '',
  subjectType: 'application',
  scopes: ['realtime.predict'],
  endpointModeHint: 'realtime',
  expiresAt: ''
});

export function AccessPage() {
  const queryClient = useQueryClient();
  const [showCreateUser, setShowCreateUser] = useState(false);
  const [showEditUser, setShowEditUser] = useState(false);
  const [showUserPassword, setShowUserPassword] = useState(false);
  const [showSelfPassword, setShowSelfPassword] = useState(false);
  const [showCreateKey, setShowCreateKey] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyCreateResponse | null>(null);
  const [selectedUserId, setSelectedUserId] = useState('');
  const [selectedApiKeyId, setSelectedApiKeyId] = useState('');
  const [userFilter, setUserFilter] = useState<'all' | 'active' | 'inactive'>('all');
  const [keyStateFilter, setKeyStateFilter] = useState<'all' | 'active' | 'revoked' | 'expired'>(
    'all'
  );
  const [keySourceFilter, setKeySourceFilter] = useState<'' | 'bootstrap' | 'managed'>('');
  const [keyScopeFilter, setKeyScopeFilter] = useState('');
  const [keySearch, setKeySearch] = useState('');
  const [plcAllowWrites, setPlcAllowWrites] = useState(false);
  const [createUserForm, setCreateUserForm] = useState<UserFormState>(defaultUserForm());
  const [editUserForm, setEditUserForm] = useState<UserEditState>(defaultUserEdit());
  const [userPassword, setUserPassword] = useState('');
  const [selfPasswordForm, setSelfPasswordForm] = useState({
    currentPassword: '',
    newPassword: ''
  });
  const [createKeyForm, setCreateKeyForm] = useState<KeyFormState>(defaultKeyForm());

  const usersQuery = useQuery({
    queryKey: ['access-users', userFilter],
    queryFn: () =>
      api.admin.users(
        userFilter === 'all' ? undefined : { active: userFilter === 'active' }
      )
  });
  const selectedUserQuery = useQuery({
    queryKey: ['access-user-detail', selectedUserId],
    queryFn: () => api.admin.userDetail(selectedUserId),
    enabled: Boolean(selectedUserId)
  });
  const userAuditQuery = useQuery({
    queryKey: ['access-user-audit', selectedUserId],
    queryFn: () =>
      api.admin.auditEvents({
        aggregateType: 'user',
        aggregateId: selectedUserId,
        limit: 10
      }),
    enabled: Boolean(selectedUserId)
  });
  const scopeQuery = useQuery({
    queryKey: ['access-api-key-scopes'],
    queryFn: api.admin.apiKeyScopes
  });
  const apiKeysQuery = useQuery({
    queryKey: ['access-api-keys', keyStateFilter, keySourceFilter, keyScopeFilter, keySearch],
    queryFn: () =>
      api.admin.apiKeys({
        state: keyStateFilter,
        sourceType: keySourceFilter || undefined,
        scope: keyScopeFilter || undefined,
        search: keySearch || undefined
      })
  });
  const selectedApiKeyQuery = useQuery({
    queryKey: ['access-api-key-detail', selectedApiKeyId],
    queryFn: () => api.admin.apiKeyDetail(selectedApiKeyId),
    enabled: Boolean(selectedApiKeyId)
  });
  const plcAccessQuery = useQuery({
    queryKey: ['access-plc-write-access'],
    queryFn: api.admin.plcWriteAccess
  });

  useEffect(() => {
    const firstUser = usersQuery.data?.items[0];
    if (!selectedUserId && firstUser) {
      setSelectedUserId(firstUser.user_id);
      return;
    }
    if (
      selectedUserId &&
      usersQuery.data &&
      !usersQuery.data.items.some((item) => item.user_id === selectedUserId)
    ) {
      setSelectedUserId(firstUser?.user_id ?? '');
    }
  }, [usersQuery.data, selectedUserId]);

  useEffect(() => {
    const firstKey = apiKeysQuery.data?.items[0];
    if (!selectedApiKeyId && firstKey) {
      setSelectedApiKeyId(firstKey.api_key_id);
      return;
    }
    if (
      selectedApiKeyId &&
      apiKeysQuery.data &&
      !apiKeysQuery.data.items.some((item) => item.api_key_id === selectedApiKeyId)
    ) {
      setSelectedApiKeyId(firstKey?.api_key_id ?? '');
    }
  }, [apiKeysQuery.data, selectedApiKeyId]);

  useEffect(() => {
    if (plcAccessQuery.data) {
      setPlcAllowWrites(plcAccessQuery.data.desired_allow_writes);
    }
  }, [plcAccessQuery.data]);

  const createUserMutation = useMutation({
    mutationFn: () =>
      api.admin.createUser({
        username: createUserForm.username.trim(),
        display_name: createUserForm.displayName.trim(),
        description: createUserForm.description.trim() || null,
        password: createUserForm.password
      }),
    onSuccess: (user) => {
      setShowCreateUser(false);
      setCreateUserForm(defaultUserForm());
      setSelectedUserId(user.user_id);
      void queryClient.invalidateQueries({ queryKey: ['access-users'] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-detail', user.user_id] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-audit', user.user_id] });
    }
  });

  const updateUserMutation = useMutation({
    mutationFn: () =>
      api.admin.updateUser(selectedUserId, {
        display_name: editUserForm.displayName.trim(),
        description: editUserForm.description.trim() || null
      }),
    onSuccess: (user) => {
      setShowEditUser(false);
      setSelectedUserId(user.user_id);
      void queryClient.invalidateQueries({ queryKey: ['access-users'] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-detail', user.user_id] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-audit', user.user_id] });
    }
  });

  const changeUserPasswordMutation = useMutation({
    mutationFn: () =>
      api.admin.changeUserPassword(selectedUserId, {
        new_password: userPassword
      }),
    onSuccess: (user) => {
      setShowUserPassword(false);
      setUserPassword('');
      setSelectedUserId(user.user_id);
      void queryClient.invalidateQueries({ queryKey: ['access-user-detail', user.user_id] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-audit', user.user_id] });
    }
  });

  const changeSelfPasswordMutation = useMutation({
    mutationFn: () =>
      api.admin.changeOwnPassword({
        current_password: selfPasswordForm.currentPassword,
        new_password: selfPasswordForm.newPassword
      }),
    onSuccess: () => {
      setShowSelfPassword(false);
      setSelfPasswordForm({ currentPassword: '', newPassword: '' });
      void queryClient.invalidateQueries({ queryKey: ['session'] });
      void queryClient.invalidateQueries({ queryKey: ['access-users'] });
      if (selectedUserId) {
        void queryClient.invalidateQueries({ queryKey: ['access-user-detail', selectedUserId] });
        void queryClient.invalidateQueries({ queryKey: ['access-user-audit', selectedUserId] });
      }
    }
  });

  const toggleUserActiveMutation = useMutation({
    mutationFn: async (user: UserDetail) => {
      if (user.is_active) {
        return api.admin.deactivateUser(user.user_id);
      }
      return api.admin.reactivateUser(user.user_id);
    },
    onSuccess: (user) => {
      setSelectedUserId(user.user_id);
      void queryClient.invalidateQueries({ queryKey: ['access-users'] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-detail', user.user_id] });
      void queryClient.invalidateQueries({ queryKey: ['access-user-audit', user.user_id] });
      void queryClient.invalidateQueries({ queryKey: ['session'] });
    }
  });

  const createKeyMutation = useMutation({
    mutationFn: () =>
      api.admin.createApiKey({
        label: createKeyForm.label.trim(),
        description: createKeyForm.description.trim() || null,
        intended_usage: createKeyForm.intendedUsage.trim() || null,
        subject_name: createKeyForm.subjectName.trim() || createKeyForm.label.trim(),
        subject_type: createKeyForm.subjectType.trim(),
        scopes: createKeyForm.scopes,
        expires_at: createKeyForm.expiresAt
          ? new Date(createKeyForm.expiresAt).toISOString()
          : null,
        endpoint_mode_hint: createKeyForm.endpointModeHint || null
      } as ApiKeyCreateRequest),
    onSuccess: (result) => {
      setShowCreateKey(false);
      setCreateKeyForm(defaultKeyForm());
      setCreatedKey(result);
      setSelectedApiKeyId(result.api_key.api_key_id);
      void queryClient.invalidateQueries({ queryKey: ['access-api-keys'] });
      void queryClient.invalidateQueries({
        queryKey: ['access-api-key-detail', result.api_key.api_key_id]
      });
    }
  });

  const revokeKeyMutation = useMutation({
    mutationFn: (apiKeyId: string) => api.admin.revokeApiKey(apiKeyId),
    onSuccess: (result) => {
      setSelectedApiKeyId(result.api_key.api_key_id);
      void queryClient.invalidateQueries({ queryKey: ['access-api-keys'] });
      void queryClient.invalidateQueries({
        queryKey: ['access-api-key-detail', result.api_key.api_key_id]
      });
    }
  });

  const updatePlcAccessMutation = useMutation({
    mutationFn: () => api.admin.updatePlcWriteAccess({ allow_writes: plcAllowWrites }),
    onSuccess: (status) => {
      setPlcAllowWrites(status.desired_allow_writes);
      void queryClient.invalidateQueries({ queryKey: ['access-plc-write-access'] });
    }
  });

  const selectedUser = selectedUserQuery.data;
  const selectedApiKey = selectedApiKeyQuery.data;
  const assignableScopes = useMemo(
    () => scopeQuery.data?.items.filter((item) => item.assignable) ?? [],
    [scopeQuery.data]
  );
  const plcPersistentSlots =
    plcAccessQuery.data?.connections.reduce(
      (sum, connection) => sum + connection.persistent_connection_limit,
      0
    ) ?? 0;
  const plcConnectionWarnings =
    plcAccessQuery.data?.connections.filter(
      (connection) => connection.connection_safety_status === 'mixed_policy'
    ).length ?? 0;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Access & Security</p>
          <h2>Manage human users and inference API keys without touching config files for normal operations.</h2>
          <p className="subtle-text">
            Browser sessions stay on the operator side, machine keys stay on the inference side,
            and bootstrap/config-managed keys remain visible as read-only migration artifacts.
          </p>
        </div>
        <div className="actions-row">
          <button type="button" onClick={() => setShowCreateUser(true)}>
            Create User
          </button>
          <button type="button" onClick={() => setShowSelfPassword(true)}>
            Change My Password
          </button>
          <button type="button" onClick={() => setShowCreateKey(true)}>
            Create API Key
          </button>
        </div>
      </section>

      <Panel
        title="PLC Write Access"
        action={
          <button type="button" onClick={() => void plcAccessQuery.refetch()}>
            Refresh
          </button>
        }
      >
        {!plcAccessQuery.data ? (
          <EmptyState
            title="Loading PLC write access"
            body="The access workspace is gathering Siemens SPS write permissions and configured runtime targets."
          />
        ) : (
          <div className="training-detail-stack">
            <div className="runtime-warning">
              Siemens SPS writes are industrial side effects. The page does not connect to a PLC or
              perform test writes; it only controls the runtime allow gate and lists configured targets.
            </div>
            <div className="detail-grid">
              <Metric
                label="Active Runtime Gate"
                value={plcAccessQuery.data.allow_writes ? 'allowed' : 'blocked'}
              />
              <Metric
                label="Deployment Env"
                value={
                  plcAccessQuery.data.runtime_env_allow_writes === null ||
                  plcAccessQuery.data.runtime_env_allow_writes === undefined
                    ? 'not set'
                    : plcAccessQuery.data.runtime_env_allow_writes
                      ? 'allowed'
                      : 'blocked'
                }
              />
              <Metric
                label="python-snap7"
                value={plcAccessQuery.data.dependency_available ? 'available' : 'missing'}
              />
              <Metric
                label="Configured Targets"
                value={String(plcAccessQuery.data.connections.length)}
              />
              <Metric label="Persistent Slots" value={String(plcPersistentSlots)} />
              <Metric label="Connection Warnings" value={String(plcConnectionWarnings)} />
              <Metric
                label="Env File"
                value={plcAccessQuery.data.runtime_env_writable ? 'writable' : 'read-only'}
              />
              <Metric
                label="Restart"
                value={plcAccessQuery.data.restart_required ? 'required' : 'not required'}
              />
            </div>
            <form
              className="inline-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (
                  plcAllowWrites &&
                  !plcAccessQuery.data?.desired_allow_writes &&
                  !confirmConsequentialAction({
                    title: 'Allow PLC writes?',
                    consequence:
                      'Runtime executions with enabled Siemens SPS write nodes and dry-run off ' +
                      'may write to configured PLC DB addresses after services reload this setting.',
                    secondary:
                      'Keep this disabled unless PLC addresses, mappings, and safety behavior have been validated.'
                  })
                ) {
                  return;
                }
                updatePlcAccessMutation.mutate();
              }}
            >
              <label>
                <input
                  type="checkbox"
                  checked={plcAllowWrites}
                  disabled={
                    !plcAccessQuery.data.update_supported || updatePlcAccessMutation.isPending
                  }
                  onChange={(event) => setPlcAllowWrites(event.target.checked)}
                />
                Allow PLC writes in deployment env
              </label>
              <button
                type="submit"
                disabled={
                  !plcAccessQuery.data.update_supported || updatePlcAccessMutation.isPending
                }
              >
                Save PLC Access
              </button>
              {plcAccessQuery.data.restart_required ? (
                <span className="badge badge--warn">restart required</span>
              ) : null}
            </form>
            {!plcAccessQuery.data.update_supported ? (
              <p className="subtle-text">
                This deployment does not expose a writable runtime env file to the admin service.
                Set `CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED` during offline export or update the
                target runtime env manually.
              </p>
            ) : null}
            <MutationError
              error={updatePlcAccessMutation.error}
              fallbackTitle="Could not update PLC write access"
            />
            <PlcConnectionsTable connections={plcAccessQuery.data.connections} />
          </div>
        )}
      </Panel>

      <div className="training-grid training-grid--details">
        <Panel
          title="Users"
          action={
            <div className="inline-form">
              <label>
                View
                <select
                  value={userFilter}
                  onChange={(event) =>
                    setUserFilter(event.target.value as 'all' | 'active' | 'inactive')
                  }
                >
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="inactive">Inactive</option>
                </select>
              </label>
            </div>
          }
        >
          {!usersQuery.data ? (
            <EmptyState
              title="Loading users"
              body="The access workspace is gathering platform accounts and their current status."
            />
          ) : usersQuery.data.items.length === 0 ? (
            <EmptyState
              title="No users found"
              body="Create the first additional operator account from this page."
            />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Display Name</th>
                  <th>Status</th>
                  <th>Last Login</th>
                </tr>
              </thead>
              <tbody>
                {usersQuery.data.items.map((item) => (
                  <tr
                    key={item.user_id}
                    className={
                      item.user_id === selectedUserId
                        ? 'training-row training-row--selected'
                        : 'training-row'
                    }
                    onClick={() => setSelectedUserId(item.user_id)}
                  >
                    <td>{item.username}</td>
                    <td>{item.display_name}</td>
                    <td>
                      <span className={`badge badge--${item.is_active ? 'success' : 'warn'}`}>
                        {item.is_active ? 'active' : 'inactive'}
                      </span>
                    </td>
                    <td>{formatDate(item.last_login_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel
          title="Selected User"
          action={
            selectedUser ? (
              <div className="actions-row">
                <button
                  type="button"
                  className="button--danger"
                  onClick={() => {
                    setEditUserForm({
                      displayName: selectedUser.display_name,
                      description: selectedUser.description ?? ''
                    });
                    setShowEditUser(true);
                  }}
                >
                  Edit Details
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setUserPassword('');
                    setShowUserPassword(true);
                  }}
                >
                  Change Password
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const confirmed = confirmConsequentialAction({
                      title: selectedUser.is_active
                        ? `Deactivate ${selectedUser.username}?`
                        : `Reactivate ${selectedUser.username}?`,
                      consequence: selectedUser.is_active
                        ? 'The user will lose browser-session access immediately.'
                        : 'The user will be allowed to log in again immediately.'
                    });
                    if (confirmed) {
                      toggleUserActiveMutation.mutate(selectedUser);
                    }
                  }}
                >
                  {selectedUser.is_active ? 'Deactivate' : 'Reactivate'}
                </button>
              </div>
            ) : null
          }
        >
          {!selectedUser ? (
            <EmptyState
              title="No user selected"
              body="Select a user from the table to inspect details, audit history, and account actions."
            />
          ) : (
            <div className="training-detail-stack">
              <div className="detail-grid">
                <Metric label="Username" value={selectedUser.username} />
                <Metric label="Display Name" value={selectedUser.display_name} />
                <Metric label="Status" value={selectedUser.is_active ? 'active' : 'inactive'} />
                <Metric label="Created" value={formatDate(selectedUser.created_at)} />
                <Metric label="Updated" value={formatDate(selectedUser.updated_at)} />
                <Metric label="Last Login" value={formatDate(selectedUser.last_login_at)} />
              </div>
              <div className="meta-grid">
                <div>
                  <strong>Description</strong>
                  <p className="subtle-text">{selectedUser.description ?? 'No operator notes recorded.'}</p>
                </div>
                <div>
                  <strong>Created By</strong>
                  <p className="subtle-text">{selectedUser.created_by ?? 'bootstrap'}</p>
                </div>
                <div>
                  <strong>Password Changed</strong>
                  <p className="subtle-text">{formatDate(selectedUser.password_changed_at)}</p>
                </div>
              </div>
              <div>
                <h3>Recent Audit Activity</h3>
                {!userAuditQuery.data ? (
                  <p className="subtle-text">Loading audit summary…</p>
                ) : userAuditQuery.data.items.length === 0 ? (
                  <p className="subtle-text">No user-scoped audit events recorded yet.</p>
                ) : (
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>When</th>
                        <th>Event</th>
                        <th>Actor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {userAuditQuery.data.items.map((item) => (
                        <tr key={item.event_id}>
                          <td>{formatDate(item.occurred_at)}</td>
                          <td>{item.event_type}</td>
                          <td>{item.actor_id}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel
          title="API Keys"
          action={
            <div className="inline-form">
              <label>
                State
                <select
                  value={keyStateFilter}
                  onChange={(event) =>
                    setKeyStateFilter(
                      event.target.value as 'all' | 'active' | 'revoked' | 'expired'
                    )
                  }
                >
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="revoked">Revoked</option>
                  <option value="expired">Expired</option>
                </select>
              </label>
              <label>
                Source
                <select
                  value={keySourceFilter}
                  onChange={(event) =>
                    setKeySourceFilter(event.target.value as '' | 'bootstrap' | 'managed')
                  }
                >
                  <option value="">All</option>
                  <option value="managed">Managed</option>
                  <option value="bootstrap">Bootstrap</option>
                </select>
              </label>
              <label>
                Scope
                <select value={keyScopeFilter} onChange={(event) => setKeyScopeFilter(event.target.value)}>
                  <option value="">All</option>
                  {(scopeQuery.data?.items ?? []).map((item) => (
                    <option key={item.scope} value={item.scope}>
                      {item.scope}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Search
                <input value={keySearch} onChange={(event) => setKeySearch(event.target.value)} />
              </label>
            </div>
          }
        >
          {!apiKeysQuery.data ? (
            <EmptyState
              title="Loading API keys"
              body="The access workspace is gathering managed and bootstrap/config-backed credentials."
            />
          ) : apiKeysQuery.data.items.length === 0 ? (
            <EmptyState
              title="No API keys match"
              body="Adjust the filters or create a new managed inference key."
            />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Scopes</th>
                  <th>Status</th>
                  <th>Expiry</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {apiKeysQuery.data.items.map((item) => (
                  <tr
                    key={item.api_key_id}
                    className={
                      item.api_key_id === selectedApiKeyId
                        ? 'training-row training-row--selected'
                        : 'training-row'
                    }
                    onClick={() => setSelectedApiKeyId(item.api_key_id)}
                  >
                    <td>{item.label}</td>
                    <td>{item.scopes.join(', ')}</td>
                    <td>{keyStatusLabel(item)}</td>
                    <td>{formatDate(item.expires_at)}</td>
                    <td>{formatDate(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel
          title="Selected API Key"
          action={
            selectedApiKey ? (
              <div className="actions-row">
                {selectedApiKey.mutable && selectedApiKey.is_active ? (
                  <button
                    type="button"
                    className="button--danger"
                    onClick={() => {
                      const confirmed = confirmConsequentialAction({
                        title: `Revoke ${selectedApiKey.label}?`,
                        consequence:
                          'Revocation takes effect immediately or within the short gateway auth cache window.',
                        secondary: 'The full secret is never shown again after creation.'
                      });
                      if (confirmed) {
                        revokeKeyMutation.mutate(selectedApiKey.api_key_id);
                      }
                    }}
                  >
                    Revoke Key
                  </button>
                ) : null}
              </div>
            ) : null
          }
        >
          {!selectedApiKey ? (
            <EmptyState
              title="No API key selected"
              body="Select a key to inspect metadata, source type, scopes, and revocation state."
            />
          ) : (
            <div className="training-detail-stack">
              <div className="detail-grid">
                <Metric label="Label" value={selectedApiKey.label} />
                <Metric label="Key ID" value={selectedApiKey.key_id} />
                <Metric label="Masked Key" value={selectedApiKey.masked_key} />
                <Metric label="Subject" value={selectedApiKey.subject_id} />
                <Metric label="Source" value={selectedApiKey.source_type} />
                <Metric label="Status" value={keyStatusLabel(selectedApiKey)} />
              </div>
              <div className="meta-grid">
                <div>
                  <strong>Description</strong>
                  <p className="subtle-text">{selectedApiKey.description ?? 'No operator description recorded.'}</p>
                </div>
                <div>
                  <strong>Intended Usage</strong>
                  <p className="subtle-text">
                    {selectedApiKey.intended_usage ?? 'No intended-usage note recorded.'}
                  </p>
                </div>
                <div>
                  <strong>Lifecycle</strong>
                  <p className="subtle-text">{selectedApiKey.status_reason}</p>
                </div>
              </div>
              <div className="detail-grid">
                <Metric label="Created" value={formatDate(selectedApiKey.created_at)} />
                <Metric label="Updated" value={formatDate(selectedApiKey.updated_at)} />
                <Metric label="Created By" value={selectedApiKey.created_by ?? 'bootstrap'} />
                <Metric label="Last Used" value={formatDate(selectedApiKey.last_used_at)} />
                <Metric label="Expires" value={formatDate(selectedApiKey.expires_at)} />
                <Metric label="Revoked" value={formatDate(selectedApiKey.revoked_at)} />
              </div>
              <div>
                <strong>Scopes</strong>
                <p className="subtle-text">{selectedApiKey.scopes.join(', ')}</p>
              </div>
            </div>
          )}
        </Panel>
      </div>

      {showCreateUser ? (
        <Modal title="Create User" onClose={() => setShowCreateUser(false)}>
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              createUserMutation.mutate();
            }}
          >
            <label>
              Username
              <input
                value={createUserForm.username}
                onChange={(event) =>
                  setCreateUserForm((current) => ({ ...current, username: event.target.value }))
                }
                required
              />
            </label>
            <label>
              Display Name
              <input
                value={createUserForm.displayName}
                onChange={(event) =>
                  setCreateUserForm((current) => ({ ...current, displayName: event.target.value }))
                }
                required
              />
            </label>
            <label className="training-form-grid__full">
              Initial Password
              <input
                type="password"
                value={createUserForm.password}
                onChange={(event) =>
                  setCreateUserForm((current) => ({ ...current, password: event.target.value }))
                }
                required
              />
            </label>
            <details className="advanced-section training-form-grid__full">
              <summary>Advanced user fields</summary>
              <label>
                Description / Notes
                <textarea
                  value={createUserForm.description}
                  onChange={(event) =>
                    setCreateUserForm((current) => ({ ...current, description: event.target.value }))
                  }
                  rows={3}
                />
              </label>
            </details>
            <MutationError error={createUserMutation.error} fallbackTitle="Could not create user" />
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={createUserMutation.isPending}>
                Create User
              </button>
              <button type="button" onClick={() => setShowCreateUser(false)}>
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {showEditUser && selectedUser ? (
        <Modal title={`Edit ${selectedUser.username}`} onClose={() => setShowEditUser(false)}>
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              updateUserMutation.mutate();
            }}
          >
            <label>
              Display Name
              <input
                value={editUserForm.displayName}
                onChange={(event) =>
                  setEditUserForm((current) => ({ ...current, displayName: event.target.value }))
                }
                required
              />
            </label>
            <label className="training-form-grid__full">
              Description / Notes
              <textarea
                value={editUserForm.description}
                onChange={(event) =>
                  setEditUserForm((current) => ({ ...current, description: event.target.value }))
                }
                rows={3}
              />
            </label>
            <MutationError error={updateUserMutation.error} fallbackTitle="Could not update user" />
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={updateUserMutation.isPending}>
                Save Changes
              </button>
              <button type="button" onClick={() => setShowEditUser(false)}>
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {showUserPassword && selectedUser ? (
        <Modal title={`Change Password: ${selectedUser.username}`} onClose={() => setShowUserPassword(false)}>
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              changeUserPasswordMutation.mutate();
            }}
          >
            <label className="training-form-grid__full">
              New Password
              <input
                type="password"
                value={userPassword}
                onChange={(event) => setUserPassword(event.target.value)}
                required
              />
            </label>
            <MutationError
              error={changeUserPasswordMutation.error}
              fallbackTitle="Could not change the user's password"
            />
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={changeUserPasswordMutation.isPending}>
                Update Password
              </button>
              <button type="button" onClick={() => setShowUserPassword(false)}>
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {showSelfPassword ? (
        <Modal title="Change My Password" onClose={() => setShowSelfPassword(false)}>
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              changeSelfPasswordMutation.mutate();
            }}
          >
            <label className="training-form-grid__full">
              Current Password
              <input
                type="password"
                value={selfPasswordForm.currentPassword}
                onChange={(event) =>
                  setSelfPasswordForm((current) => ({
                    ...current,
                    currentPassword: event.target.value
                  }))
                }
                required
              />
            </label>
            <label className="training-form-grid__full">
              New Password
              <input
                type="password"
                value={selfPasswordForm.newPassword}
                onChange={(event) =>
                  setSelfPasswordForm((current) => ({
                    ...current,
                    newPassword: event.target.value
                  }))
                }
                required
              />
            </label>
            <MutationError error={changeSelfPasswordMutation.error} fallbackTitle="Could not change your password" />
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={changeSelfPasswordMutation.isPending}>
                Rotate Password
              </button>
              <button type="button" onClick={() => setShowSelfPassword(false)}>
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {showCreateKey ? (
        <Modal title="Create API Key" onClose={() => setShowCreateKey(false)}>
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              createKeyMutation.mutate();
            }}
          >
            <label>
              Name
              <input
                value={createKeyForm.label}
                onChange={(event) =>
                  setCreateKeyForm((current) => ({ ...current, label: event.target.value }))
                }
                required
              />
            </label>
            <label>
              Expires At
              <input
                type="datetime-local"
                value={createKeyForm.expiresAt}
                onChange={(event) =>
                  setCreateKeyForm((current) => ({ ...current, expiresAt: event.target.value }))
                }
              />
            </label>
            <div className="training-form-grid__full">
              <strong>Allowed Scopes</strong>
              <div className="pipeline-grid">
                {assignableScopes.map((scope) => {
                  const checked = createKeyForm.scopes.includes(scope.scope);
                  return (
                    <label key={scope.scope} className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) =>
                          setCreateKeyForm((current) => ({
                            ...current,
                            scopes: event.target.checked
                              ? [...current.scopes, scope.scope]
                              : current.scopes.filter((item) => item !== scope.scope)
                          }))
                        }
                      />
                      <span>
                        <strong>{scope.label}</strong>
                        <small className="subtle-text">{scope.description}</small>
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>
            <details className="advanced-section training-form-grid__full">
              <summary>Advanced key fields</summary>
              <div className="training-form-grid">
                <label>
                  Subject / Application Name
                  <input
                    value={createKeyForm.subjectName}
                    onChange={(event) =>
                      setCreateKeyForm((current) => ({ ...current, subjectName: event.target.value }))
                    }
                    placeholder="Defaults to key name"
                  />
                </label>
                <label>
                  Subject Type
                  <input
                    value={createKeyForm.subjectType}
                    onChange={(event) =>
                      setCreateKeyForm((current) => ({ ...current, subjectType: event.target.value }))
                    }
                    required
                  />
                </label>
                <label>
                  Endpoint Mode Hint
                  <select
                    value={createKeyForm.endpointModeHint}
                    onChange={(event) =>
                      setCreateKeyForm((current) => ({
                        ...current,
                        endpointModeHint: event.target.value as '' | 'realtime' | 'batch' | 'both'
                      }))
                    }
                  >
                    <option value="">None</option>
                    <option value="realtime">Realtime</option>
                    <option value="batch">Batch</option>
                    <option value="both">Both</option>
                  </select>
                </label>
                <label className="training-form-grid__full">
                  Description
                  <textarea
                    value={createKeyForm.description}
                    onChange={(event) =>
                      setCreateKeyForm((current) => ({ ...current, description: event.target.value }))
                    }
                    rows={2}
                  />
                </label>
                <label className="training-form-grid__full">
                  Intended Usage
                  <textarea
                    value={createKeyForm.intendedUsage}
                    onChange={(event) =>
                      setCreateKeyForm((current) => ({
                        ...current,
                        intendedUsage: event.target.value
                      }))
                    }
                    rows={3}
                  />
                </label>
              </div>
            </details>
            <p className="subtle-text">
              The full secret is shown exactly once after creation. Only the masked token remains visible afterwards.
            </p>
            <MutationError error={createKeyMutation.error} fallbackTitle="Could not create API key" />
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={createKeyMutation.isPending}>
                Create API Key
              </button>
              <button type="button" onClick={() => setShowCreateKey(false)}>
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {createdKey ? (
        <Modal title="API Key Created" onClose={() => setCreatedKey(null)}>
          <div className="training-detail-stack">
            <p className="subtle-text">
              Copy this token now. The full secret will not be shown again after you close this dialog.
            </p>
            <label className="training-form-grid__full">
              Full API Key
              <textarea readOnly rows={3} value={createdKey.token} />
            </label>
            <div className="detail-grid">
              <Metric label="Label" value={createdKey.api_key.label} />
              <Metric label="Key ID" value={createdKey.api_key.key_id} />
              <Metric label="Masked" value={createdKey.api_key.masked_key} />
              <Metric label="Scopes" value={createdKey.api_key.scopes.join(', ')} />
            </div>
            <div className="actions-row">
              <button type="button" onClick={() => copyToClipboard(createdKey.token)}>
                Copy Key
              </button>
              <button type="button" onClick={() => setCreatedKey(null)}>
                Close
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}

function PlcConnectionsTable(props: { connections: PlcWriteAccessConnectionSummary[] }) {
  if (!props.connections.length) {
    return (
      <EmptyState
        title="No Siemens SPS targets configured"
        body="Configured Write to Siemens SPS nodes will appear here after they are added to runtime pipeline graphs."
      />
    );
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Connection</th>
          <th>Status</th>
          <th>Runtime Connection</th>
          <th>Mappings</th>
          <th>Pipelines</th>
          <th>Targets</th>
        </tr>
      </thead>
      <tbody>
        {props.connections.map((connection) => (
          <tr key={connection.connection_key}>
            <td>
              <strong>{connection.display_name}</strong>
              <p className="subtle-text">
                {connection.host || 'missing host'}:{connection.port} rack {connection.rack} slot{' '}
                {connection.slot}
              </p>
            </td>
            <td>
              <span className={`badge badge--${plcStatusTone(connection.status)}`}>
                {connection.status.replaceAll('_', ' ')}
              </span>
              <p className="subtle-text">{connection.status_reason}</p>
            </td>
            <td>
              <span
                className={`badge badge--${plcConnectionSafetyTone(connection.connection_safety_status)}`}
              >
                {formatPolicyLabel(connection.connection_safety_status)}
              </span>
              <p className="subtle-text">
                {formatPolicyLabel(connection.reuse_policy)} /{' '}
                {formatPolicyLabel(connection.reconnect_policy)}
              </p>
              <p className="subtle-text">
                persistent slots: {connection.persistent_connection_limit}
              </p>
              <p className="subtle-text">{connection.connection_safety_reason}</p>
            </td>
            <td>
              {connection.enabled_mapping_count}/{connection.mapping_count} enabled
            </td>
            <td>
              {connection.pipelines.slice(0, 3).map((pipeline) => (
                <p key={`${pipeline.pipeline_version_id}-${pipeline.node_key}`} className="subtle-text">
                  {pipeline.pipeline_name} v{pipeline.version_no} {pipeline.version_scope} /{' '}
                  {pipeline.node_key}
                  <br />
                  {formatPolicyLabel(pipeline.reuse_policy)} /{' '}
                  {formatPolicyLabel(pipeline.reconnect_policy)}
                </p>
              ))}
              {connection.pipelines.length > 3 ? (
                <p className="subtle-text">+{connection.pipelines.length - 3} more</p>
              ) : null}
            </td>
            <td>
              {connection.sample_targets.length ? (
                connection.sample_targets.map((target) => (
                  <p key={target} className="subtle-text">
                    <code>{target}</code>
                  </p>
                ))
              ) : (
                <span className="subtle-text">No mapped DB targets</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Modal(props: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
        <div className="status-card__header">
          <h3>{props.title}</h3>
          <button type="button" onClick={props.onClose}>
            Close
          </button>
        </div>
        {props.children}
      </div>
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

function MutationError(props: { error: unknown; fallbackTitle: string }) {
  if (!props.error) {
    return null;
  }
  const details = describeError(props.error, props.fallbackTitle);
  return (
    <div className="inline-alert inline-alert--danger training-form-grid__full">
      <strong>{details.title}</strong>
      {details.description ? <p>{details.description}</p> : null}
    </div>
  );
}

function formatDate(value?: string | null) {
  if (!value) {
    return 'never';
  }
  return new Intl.DateTimeFormat('en-GB', {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

function keyStatusLabel(item: ApiKeySummary | ApiKeyDetail) {
  if (item.is_revoked) {
    return 'revoked';
  }
  if (item.expires_at && !item.is_active) {
    return 'expired';
  }
  if (item.source_type === 'bootstrap') {
    return 'bootstrap';
  }
  return 'active';
}

export function plcStatusTone(status: PlcWriteAccessConnectionSummary['status']) {
  if (status === 'ready') {
    return 'success';
  }
  if (status === 'environment_blocked' || status === 'dry_run' || status === 'disabled') {
    return 'warn';
  }
  return 'danger';
}

export function plcConnectionSafetyTone(
  status: PlcWriteAccessConnectionSummary['connection_safety_status']
) {
  if (status === 'bounded') {
    return 'success';
  }
  if (status === 'ephemeral') {
    return 'info';
  }
  return 'warn';
}

function formatPolicyLabel(value: string) {
  return value.replaceAll('_', ' ');
}

async function copyToClipboard(value: string) {
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(value);
    return;
  }
  window.prompt('Copy this value', value);
}
