import { renderToStaticMarkup } from 'react-dom/server';
import type { EdgeSettingsStatus } from '@cv-platform/schemas';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EdgeSettingsForm, EdgeSettingsPanel } from './settings';

const query = vi.hoisted(() => ({ data: undefined as EdgeSettingsStatus | undefined, isError: false, isPending: false }));
vi.mock('../../components/notification-provider', () => ({ useNotifications: () => ({ notify: vi.fn() }) }));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({}), useMutation: () => ({ isPending: false }), useQuery: () => query
}));

const status: EdgeSettingsStatus = { supported: true, writable: true, phase: 'idle', revision: 'a'.repeat(64), values: {
  YBB_EDGE_PLANT_ALLOWED_IPS: '192.168.50.40,192.168.50.41', YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS: '2500',
  YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS: '4', YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND: '20', YBB_EDGE_PLANT_MAX_REQUEST_BYTES: '65536',
  YBB_EDGE_PREVIEW_JPEG_QUALITY: '70', YBB_EDGE_PREVIEW_MAX_BYTES: '1048576', YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS: '900'
} };
const render = () => renderToStaticMarkup(<EdgeSettingsPanel deviceId="edge-1" enabled />);

describe('Edge device operating settings', () => {
  beforeEach(() => { query.data = { ...status }; query.isError = false; });

  it('shows observed trigger clients, advanced values and the CIDR distinction', () => {
    const html = render();
    expect(html).toContain('192.168.50.40, 192.168.50.41');
    expect(html).toContain('unabhängig vom erlaubten Edge-Zielnetz');
    expect(html).toContain('YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS');
    expect(html).not.toContain('disabled=""');
  });

  it('explains an empty client list and the restart before applying', () => {
    const html = renderToStaticMarkup(<EdgeSettingsForm status={{ ...status, values: { ...status.values, YBB_EDGE_PLANT_ALLOWED_IPS: '' } }} saving={false} onSave={vi.fn()} onCancel={vi.fn()} />);
    expect(html).toContain('Eine leere Liste sperrt alle entfernten Trigger-Clients');
    expect(html).toContain('Edge-Agent neu');
    expect(html).toContain('Speichern und am Edge anwenden');
    expect(html).not.toContain('YBB_ALLOW_PLC_WRITES');
  });

  it.each(['queued', 'applying'] as const)('keeps %s distinct from successful confirmation and blocks edits', phase => {
    query.data = { ...status, phase };
    expect(render()).toContain('disabled=""');
    expect(render()).not.toContain('Vom neu gestarteten Gerät bestätigt');
  });

  it('disables stale reads and reports that the device has not answered', () => {
    query.isError = true;
    expect(render()).toContain('disabled=""');
    expect(render()).toContain('letzte gelesene Stand');
  });

  it('explains missing support and helper installation without offering an unusable editor', () => {
    query.data = { ...status, supported: false, writable: false, message: 'Edge-Update erforderlich.' };
    expect(render()).toContain('Edge-Update erforderlich.');
    expect(render()).not.toContain('Einstellungen bearbeiten');
    query.data = { ...status, writable: false, message: 'Offline-Installer einmalig ausführen.' };
    expect(render()).toContain('disabled=""');
    expect(render()).toContain('Offline-Installer einmalig ausführen.');
  });
});
