import { describe, expect, it } from 'vitest';
import { canAccessPath, findNavItem, navItems } from './navigation';

describe('navigation', () => {
  it('maps legacy project routes to the pipeline workspace', () => {
    expect(findNavItem('/projects').to).toBe('/pipelines');
    expect(findNavItem('/pipelines/lpr_123').label).toBe('Projekte');
  });

  it('keeps advanced subroutes grouped under operations', () => {
    expect(findNavItem('/operations/deployments').to).toBe('/operations');
    expect(findNavItem('/settings').label).toBe('System');
  });

  it('exposes runtime channels as a primary operator workspace', () => {
    expect(findNavItem('/channels/chn_123').label).toBe('Kanäle');
  });

  it('exposes edge fleet management as a primary operator workspace', () => {
    expect(findNavItem('/edge-devices/edge_123').label).toBe('Edge Devices');
  });

  it('removes the separate models and endpoints modules from primary navigation', () => {
    expect(navItems.some((item) => item.to === '/models')).toBe(false);
    expect(navItems.some((item) => item.to === '/endpoints')).toBe(false);
  });

  it('allows authenticated users through the admin navigation shell', () => {
    expect(canAccessPath('/pipelines/lpr_123', 'label_manager')).toBe(true);
    expect(canAccessPath('/channels/chn_123', 'operator')).toBe(true);
    expect(canAccessPath('/operations', 'label_manager')).toBe(true);
    expect(canAccessPath('/endpoints/demo-model', 'viewer')).toBe(true);
  });
});
