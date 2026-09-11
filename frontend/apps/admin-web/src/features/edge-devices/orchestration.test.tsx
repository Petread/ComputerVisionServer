import { renderToStaticMarkup } from 'react-dom/server';
import type { EdgeDeviceSummary, EdgeSoftwareDevice } from '@cv-platform/schemas';
import { describe, expect, it, vi } from 'vitest';
import { EdgeOrchestration, softwareBusy, softwareUpdateBlockers } from './orchestration';

vi.mock('@tanstack/react-router', () => ({ Link: ({ children }: { children: React.ReactNode }) => <a>{children}</a> }));
vi.mock('../../components/notification-provider', () => ({ useNotifications: () => ({ notify: vi.fn() }) }));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({}),
  useMutation: () => ({ isPending: false }),
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    isPending: false, isError: false,
    data: queryKey[0] === 'edge-software' ? {
      server_runtime_digest: 'a'.repeat(64), releases: [], devices: [{
        edge_device_id: 'edge-1', runtime_code_digest: null, update_protocol: 0,
        requires_update: true, node_count: 18, platform: 'windows_11', releases: [], commands: []
      }]
    } : { items: [] }
  })
}));

const device = {
  edge_device_id: 'edge-1', name: 'Sägelinie', technical_place: 'Halle 2',
  address: { management_ip: '192.168.50.21' }, enabled: true,
  connectivity_state: 'online', assigned_channel_count: 2
} as EdgeDeviceSummary;
const state: EdgeSoftwareDevice = {
  edge_device_id: 'edge-1', runtime_code_digest: 'b'.repeat(64), update_protocol: 1,
  requires_update: true, node_count: 60, platform: 'windows_11', runtime_version: '0.8.0',
  releases: [{ package_sha256: 'package', reasons: [] }], commands: []
};

describe('central Edge orchestration', () => {
  it('only enables software updates for reachable compatible devices without active work', () => {
    expect(softwareUpdateBlockers(device, state, 'package')).toEqual([]);
    expect(softwareUpdateBlockers({ ...device, connectivity_state: 'offline' }, state, 'package')).not.toEqual([]);
    expect(softwareUpdateBlockers(device, state, 'unknown')).not.toEqual([]);
    expect(softwareUpdateBlockers(device, { ...state, releases: [{ package_sha256: 'package', reasons: ['Python-Basisversion passt nicht.'] }] }, 'package')).toContain('Python-Basisversion passt nicht.');
    const busy = { ...state, commands: [{ command_id: 'job', status: 'running', action: 'update', software: { phase: 'checking' }, created_at: '2026-09-06T10:00:00Z' }] } as EdgeSoftwareDevice;
    expect(softwareBusy(busy)).toBe(true);
    expect(softwareUpdateBlockers(device, busy, 'package')).toContain('Ein Software-Auftrag läuft bereits.');
  });

  it('makes the bootstrap requirement, channel count and missing runtime identity visible', () => {
    const html = renderToStaticMarkup(<EdgeOrchestration devices={[device]} />);
    expect(html).toContain('Sägelinie');
    expect(html).toContain('Halle 2');
    expect(html).toContain('Zugewiesene Kanäle');
    expect(html).toContain('Launcher einmalig installieren');
    expect(html).toContain('Nicht erfasst');
    expect(html).toContain('disabled="">Runtime aktualisieren');
    expect(html).toContain('Software-Rollback');
  });
});
