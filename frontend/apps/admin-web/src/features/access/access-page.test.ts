import { describe, expect, it } from 'vitest';

import { plcConnectionSafetyTone, plcStatusTone } from './access-page';

describe('access page PLC helpers', () => {
  it('maps PLC write access statuses to operator-visible severity tones', () => {
    expect(plcStatusTone('ready')).toBe('success');
    expect(plcStatusTone('environment_blocked')).toBe('warn');
    expect(plcStatusTone('dry_run')).toBe('warn');
    expect(plcStatusTone('disabled')).toBe('warn');
    expect(plcStatusTone('dependency_missing')).toBe('danger');
    expect(plcStatusTone('invalid_config')).toBe('danger');
  });

  it('maps PLC connection safety to operator-visible severity tones', () => {
    expect(plcConnectionSafetyTone('bounded')).toBe('success');
    expect(plcConnectionSafetyTone('ephemeral')).toBe('info');
    expect(plcConnectionSafetyTone('mixed_policy')).toBe('warn');
  });
});
