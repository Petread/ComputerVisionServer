import { renderToStaticMarkup } from 'react-dom/server';
import type { EdgeDeviceSummary, EdgeSoftwareDevice, EdgeSoftwareOverview, EdgeSoftwarePackage } from '@cv-platform/schemas';
import { describe, expect, it, vi } from 'vitest';
import { EdgeOrchestration, recommendedSoftwarePackage, softwareBusy, softwareSelectionBlockers, softwareUpdateBlockers } from './orchestration';

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

function packageFixture(id: string): EdgeSoftwarePackage {
  return {
    package_sha256: id, size_bytes: 123,
    release: {
      release_id: `release-${id}`, platform: 'windows_11', architecture: 'x86_64',
      python_version: '3.12', runtime_code_digest: 'current', node_contract_versions: {}
    }
  };
}

describe('central Edge orchestration', () => {
  it('allows a verified manual selection when bundled targets are missing while retaining device and package checks', () => {
    const overview: EdgeSoftwareOverview = {
      server_runtime_digest: 'current', targets_managed: true,
      target_error: 'Mitgeliefertes Edge-Paket fehlt im Katalog.',
      releases: [packageFixture('manual')],
      devices: [{ ...state, recommended_package_sha256: null,
        recommendation_reasons: ['Mitgeliefertes Edge-Paket fehlt im Katalog.'],
        releases: [{ package_sha256: 'manual', reasons: [] }]
      }]
    };
    expect(softwareSelectionBlockers(device, overview, 'manual')).toEqual([]);
    expect(softwareSelectionBlockers(device, overview)).not.toEqual([]);
    expect(softwareSelectionBlockers(device, overview, 'missing')).not.toEqual([]);
    expect(softwareSelectionBlockers({ ...device, enabled: false }, overview, 'manual')).not.toEqual([]);
    expect(softwareSelectionBlockers({ ...device, connectivity_state: 'offline' }, overview, 'manual')).not.toEqual([]);
    for (const reason of ['Python-Basisversion passt nicht.', 'Paketdatei fehlt.']) {
      const incompatible = { ...overview, devices: [{ ...overview.devices[0], releases: [{ package_sha256: 'manual', reasons: [reason] }] }] };
      expect(softwareSelectionBlockers(device, incompatible, 'manual')).toContain(reason);
    }
  });

  it('uses the exact central target for agent-only updates and respects blocked recommendations', () => {
    const overview = {
      server_runtime_digest: 'current', targets_managed: true, central_release_id: 'central-001',
      releases: [
        packageFixture('old-agent'), packageFixture('new-agent')
      ],
      devices: [{ ...state, recommended_package_sha256: 'new-agent', releases: [
        { package_sha256: 'old-agent', reasons: [] }, { package_sha256: 'new-agent', reasons: [] }
      ] }]
    } as EdgeSoftwareOverview;
    expect(recommendedSoftwarePackage(overview, 'edge-1')).toBe('new-agent');
    expect(recommendedSoftwarePackage({ ...overview, devices: [{ ...overview.devices[0], recommended_package_sha256: null }] }, 'edge-1')).toBe('');
    expect(recommendedSoftwarePackage({ ...overview, target_error: 'Import incomplete' }, 'edge-1')).toBe('');
  });

  it('does not choose arbitrarily between legacy packages with the same runtime digest', () => {
    const overview = {
      server_runtime_digest: 'current', releases: [
        packageFixture('one'), packageFixture('two')
      ], devices: [{ ...state, releases: [{ package_sha256: 'one', reasons: [] }, { package_sha256: 'two', reasons: [] }] }]
    } as EdgeSoftwareOverview;
    expect(recommendedSoftwarePackage(overview, 'edge-1')).toBe('');
  });

  it('recommends a current compatible package per device and never falls back to old runtime code', () => {
    const overview = {
      server_runtime_digest: 'current',
      releases: [
        { package_sha256: 'old', release: { runtime_code_digest: 'old' } },
        { package_sha256: 'windows', release: { runtime_code_digest: 'current' } },
        { package_sha256: 'jetson', release: { runtime_code_digest: 'current' } }
      ],
      devices: [{ ...state, releases: [
        { package_sha256: 'old', reasons: [] },
        { package_sha256: 'windows', reasons: ['platform mismatch'] },
        { package_sha256: 'jetson', reasons: [] }
      ] }]
    } as EdgeSoftwareOverview;
    expect(recommendedSoftwarePackage(overview, 'edge-1')).toBe('jetson');
    expect(recommendedSoftwarePackage(overview, 'missing')).toBe('');
    expect(recommendedSoftwarePackage(undefined, 'edge-1')).toBe('');
    expect(recommendedSoftwarePackage({ ...overview, releases: overview.releases.slice(0, 2) }, 'edge-1')).toBe('');
    expect(softwareUpdateBlockers(device, { ...state, update_protocol: 0 }, 'package')).toContain('Launcher einmalig mit dem vorhandenen Edge-Installer einrichten.');
  });

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
