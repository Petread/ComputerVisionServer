import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';

function matchesAuditSearch(search: string, candidate: Record<string, string | null | undefined>) {
  if (!search) {
    return true;
  }
  const normalized = search.trim().toLowerCase();
  return Object.values(candidate).some((value) => value?.toLowerCase().includes(normalized));
}

export function AuditLogPage() {
  const [eventSearch, setEventSearch] = useState('');
  const [selectedAuditId, setSelectedAuditId] = useState('');
  const [selectedWebhookId, setSelectedWebhookId] = useState('');

  const auditEventsQuery = useQuery({
    queryKey: ['audit-events'],
    queryFn: () => api.admin.auditEvents({ limit: 250 }),
    refetchInterval: 10000
  });
  const webhookEventsQuery = useQuery({
    queryKey: ['audit-webhook-events'],
    queryFn: api.admin.webhookEvents,
    refetchInterval: 10000
  });

  const filteredAuditEvents = useMemo(
    () =>
      (auditEventsQuery.data?.items ?? []).filter((item) =>
        matchesAuditSearch(eventSearch, {
          event_type: item.event_type,
          aggregate_type: item.aggregate_type,
          aggregate_id: item.aggregate_id,
          source_service: item.source_service,
          actor_id: item.actor_id,
          correlation_id: item.correlation_id ?? undefined
        })
      ),
    [auditEventsQuery.data?.items, eventSearch]
  );

  useEffect(() => {
    if (!selectedAuditId && filteredAuditEvents[0]) {
      setSelectedAuditId(filteredAuditEvents[0].event_id);
    }
  }, [filteredAuditEvents, selectedAuditId]);

  useEffect(() => {
    if (!selectedWebhookId && webhookEventsQuery.data?.items[0]) {
      setSelectedWebhookId(webhookEventsQuery.data.items[0].event_id);
    }
  }, [selectedWebhookId, webhookEventsQuery.data?.items]);

  const auditDetailQuery = useQuery({
    queryKey: ['audit-event-detail', selectedAuditId],
    queryFn: () => api.admin.auditEventDetail(selectedAuditId),
    enabled: Boolean(selectedAuditId)
  });
  const webhookDetailQuery = useQuery({
    queryKey: ['audit-webhook-detail', selectedWebhookId],
    queryFn: () => api.admin.webhookEventDetail(selectedWebhookId),
    enabled: Boolean(selectedWebhookId)
  });

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Audit And Event History</p>
          <h2>Trace operator actions, state transitions, correlation IDs, and webhook ingestion from one screen.</h2>
          <p className="subtle-text">
            Use the platform event history for support and rollback investigations, then pivot into
            raw webhook payloads when you need the upstream Label Studio evidence.
          </p>
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel
          title="Platform Event History"
          action={
            <input
              value={eventSearch}
              onChange={(event) => setEventSearch(event.target.value)}
              placeholder="Filter by event, aggregate, actor, or correlation"
            />
          }
        >
          {filteredAuditEvents.length === 0 ? (
            <EmptyState
              title="No audit events match"
              body="Adjust the filters or perform an operator action to generate new event history."
            />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Occurred</th>
                  <th>Event</th>
                  <th>Aggregate</th>
                  <th>Actor</th>
                  <th>Source</th>
                  <th>Correlation</th>
                </tr>
              </thead>
              <tbody>
                {filteredAuditEvents.map((item) => (
                  <tr
                    key={item.event_id}
                    className={
                      item.event_id === selectedAuditId ? 'training-row training-row--selected' : 'training-row'
                    }
                    onClick={() => setSelectedAuditId(item.event_id)}
                  >
                    <td>{formatDate(item.occurred_at)}</td>
                    <td>
                      <strong>{item.event_type}</strong>
                      <div className="subtle-text">seq {item.sequence_no}</div>
                    </td>
                    <td>
                      {item.aggregate_type}
                      <div className="subtle-text">{item.aggregate_id}</div>
                    </td>
                    <td>
                      {item.actor_id}
                      <div className="subtle-text">{item.actor_type}</div>
                    </td>
                    <td>{item.source_service}</td>
                    <td>{item.correlation_id ?? 'n/a'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Selected Event Detail">
          {!auditDetailQuery.data ? (
            <EmptyState
              title="Choose an event"
              body="Select a row to inspect its immutable payload, causation chain, and support identifiers."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <Metric label="Event ID" value={auditDetailQuery.data.event_id} />
                <Metric label="Request ID" value={auditDetailQuery.data.request_id ?? 'n/a'} />
                <Metric label="Correlation ID" value={auditDetailQuery.data.correlation_id ?? 'n/a'} />
                <Metric label="Causation ID" value={auditDetailQuery.data.causation_id ?? 'n/a'} />
              </div>
              <pre className="code-block">{JSON.stringify(auditDetailQuery.data.payload, null, 2)}</pre>
            </div>
          )}
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Webhook Event History">
          {webhookEventsQuery.data?.items.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Action</th>
                  <th>Project</th>
                  <th>Status</th>
                  <th>Internal Event</th>
                </tr>
              </thead>
              <tbody>
                {webhookEventsQuery.data.items.map((item) => (
                  <tr
                    key={item.event_id}
                    className={
                      item.event_id === selectedWebhookId ? 'training-row training-row--selected' : 'training-row'
                    }
                    onClick={() => setSelectedWebhookId(item.event_id)}
                  >
                    <td>{formatDate(item.received_at)}</td>
                    <td>
                      <strong>{item.action}</strong>
                      <div className="subtle-text">{item.source_event_key}</div>
                    </td>
                    <td>{item.label_studio_project_id ?? 'n/a'}</td>
                    <td>{item.processing_status}</td>
                    <td>{item.internal_event_type ?? 'pending'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No webhook history"
              body="Webhook activity will appear here after Label Studio sends project events."
            />
          )}
        </Panel>

        <Panel title="Selected Webhook Detail">
          {!webhookDetailQuery.data ? (
            <EmptyState
              title="Choose a webhook event"
              body="Inspect immutable headers, source metadata, and raw payloads for support investigations."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <Metric label="Event ID" value={webhookDetailQuery.data.event_id} />
                <Metric label="Remote Addr" value={webhookDetailQuery.data.remote_addr ?? 'unknown'} />
                <Metric label="Signature" value={String(webhookDetailQuery.data.signature_valid ?? 'n/a')} />
                <Metric label="Processed" value={formatDate(webhookDetailQuery.data.processed_at)} />
              </div>
              <pre
                className="code-block"
              >{JSON.stringify({ headers: webhookDetailQuery.data.headers, payload: webhookDetailQuery.data.payload }, null, 2)}</pre>
            </div>
          )}
        </Panel>
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

function formatDate(value: string | null | undefined) {
  if (!value) {
    return 'n/a';
  }
  return new Date(value).toLocaleString();
}
