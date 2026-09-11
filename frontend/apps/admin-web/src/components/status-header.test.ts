import { describe, expect, it } from 'vitest';
import { shouldHideStatusHeader } from './status-header-visibility';

describe('shouldHideStatusHeader', () => {
  it('keeps the compact channel workspace at the top of list and detail routes', () => {
    expect(shouldHideStatusHeader('/channels')).toBe(true);
    expect(shouldHideStatusHeader('/channels/chn_42')).toBe(true);
  });

  it('keeps the shell header on unrelated pages', () => {
    expect(shouldHideStatusHeader('/health')).toBe(false);
  });
});
