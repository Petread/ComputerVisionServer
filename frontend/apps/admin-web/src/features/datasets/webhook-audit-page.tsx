import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { formatDate, statusTone } from './presentation';

export function WebhookAuditPage() {
  const eventsQuery = useQuery({
    queryKey: ['webhook-events'],
    queryFn: api.webhooks.list,
    refetchInterval: 10000
  });
  const [selectedEventId, setSelectedEventId] = useState('');

  useEffect(() => {
    if (!selectedEventId && eventsQuery.data?.items[0]) {
      setSelectedEventId(eventsQuery.data.items[0].event_id);
    }
  }, [selectedEventId, eventsQuery.data]);

  const detailQuery = useQuery({
    queryKey: ['webhook-event', selectedEventId],
    queryFn: () => api.webhooks.detail(selectedEventId),
    enabled: Boolean(selectedEventId)
  });

  return (
    <div className="panel-stack">
      <Panel title="Webhook Event Audit">
        <table className="data-table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Project</th>
              <th>Status</th>
              <th>Internal Event</th>
              <th>Signature</th>
              <th>Received</th>
            </tr>
          </thead>
          <tbody>
            {eventsQuery.data?.items.map((event) => (
              <tr key={event.event_id} onClick={() => setSelectedEventId(event.event_id)}>
                <td>
                  <strong>{event.action}</strong>
                  <div className="subtle-text">{event.source_event_key}</div>
                </td>
                <td>{event.label_studio_project_id ?? 'Not mapped'}</td>
                <td>
                  <span className={`badge badge--${statusTone(event.processing_status)}`}>
                    {event.processing_status}
                  </span>
                </td>
                <td>{event.internal_event_type ?? 'Not translated yet'}</td>
                <td>{event.signature_valid === false ? 'Invalid' : 'Valid / not required'}</td>
                <td>{formatDate(event.received_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Webhook Detail">
        {detailQuery.data ? (
          <div className="panel-stack">
            <div className="meta-grid">
              <div>
                <span className="subtle-text">Event Id</span>
                <strong>{detailQuery.data.event_id}</strong>
              </div>
              <div>
                <span className="subtle-text">Remote Address</span>
                <strong>{detailQuery.data.remote_addr ?? 'Unknown'}</strong>
              </div>
              <div>
                <span className="subtle-text">Processed</span>
                <strong>{formatDate(detailQuery.data.processed_at)}</strong>
              </div>
            </div>
            {detailQuery.data.processing_error ? (
              <div className="inline-alert inline-alert--danger">{detailQuery.data.processing_error}</div>
            ) : null}
            <pre className="code-block">
              {JSON.stringify(
                {
                  headers: detailQuery.data.headers,
                  payload: detailQuery.data.payload
                },
                null,
                2
              )}
            </pre>
          </div>
        ) : (
          <p className="subtle-text">Select an event to inspect its immutable payload and audit metadata.</p>
        )}
      </Panel>
    </div>
  );
}
