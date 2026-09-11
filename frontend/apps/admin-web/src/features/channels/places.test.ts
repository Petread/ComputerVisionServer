import { describe, expect, it } from 'vitest';
import { matchesPlace } from './places';

describe('structured channel filtering', () => {
  const places = {areas: [{id: 'area', name: 'Werk'}], facilities: [{id: 'facility', area_id: 'area', name: 'Linie'}]};
  it('filters facilities and their parent area by persistent IDs', () => {
    expect(matchesPlace({facility_id: 'facility'}, 'area', places)).toBe(true);
    expect(matchesPlace({facility_id: 'facility'}, 'facility', places)).toBe(true);
    expect(matchesPlace({facility_id: 'facility'}, 'unassigned', places)).toBe(false);
    expect(matchesPlace({facility_id: 'facility'}, 'other', places)).toBe(false);
  });
  it('keeps legacy channels visible without guessing an area from names', () => {
    expect(matchesPlace({}, 'unassigned', places)).toBe(true);
    expect(matchesPlace({}, '', places)).toBe(true);
    expect(matchesPlace({}, 'area', places)).toBe(false);
  });
});
