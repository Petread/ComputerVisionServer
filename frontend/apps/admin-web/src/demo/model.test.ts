import { describe, expect, it, vi, afterEach } from 'vitest';
import { createDemoData, deletionIssue, filterChannels, loadDemoData, placeName, seedGraph, validateGraph } from './model';
import { parseRoute } from './context';

afterEach(() => vi.unstubAllGlobals());
describe('local workspace data', () => {
  it('filters an area including every facility without mixing neighbouring areas', () => {
    const data = createDemoData();
    const rows = filterChannels(data, 'area-2', '');
    expect(rows).toHaveLength(5);
    expect(new Set(rows.map(c => c.facilityId))).toEqual(new Set(['fac-2', 'fac-3']));
    expect(filterChannels(data, 'fac-3', '', 'degraded').map(c => c.name)).toEqual(['Kantenkontrolle · Linie 2']);
  });
  it('keeps unassigned channels discoverable and searches the structural path', () => {
    const data = createDemoData();
    expect(filterChannels(data, 'unassigned', '')).toHaveLength(1);
    expect(filterChannels(data, '', 'BESÄUMLINIE')).toHaveLength(3);
    expect(filterChannels(data, 'area-1', 'Besäumlinie')).toHaveLength(0);
  });
  it('moves the visible path with an installation while preserving its channel references', () => {
    const data = createDemoData();
    data.facilities[0].areaId = 'area-2';
    expect(filterChannels(data, 'area-1', '')).toHaveLength(0);
    expect(filterChannels(data, 'area-2', '')).toHaveLength(7);
    expect(placeName(data, 'fac-1')).toBe('Sägewerk / Stammeinzug');
  });
  it('rejects deletion of referenced installations and nonempty areas', () => {
    const data = createDemoData();
    expect(deletionIssue(data, 'area-1')).toBeTruthy();
    expect(deletionIssue(data, 'fac-1')).toBeTruthy();
    data.channels = data.channels.filter(c => c.facilityId !== 'fac-1');
    expect(deletionIssue(data, 'fac-1')).toBeNull();
  });
  it('falls back to fixtures on malformed or inaccessible browser storage', () => {
    vi.stubGlobal('localStorage', { getItem: () => '{invalid' });
    expect(loadDemoData().channels).toHaveLength(12);
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('denied'); } });
    expect(loadDemoData().areas).toHaveLength(4);
  });
  it('restores local state without requests to production', () => {
    const data = createDemoData(); data.channels[0].name = 'Lokaler Test';
    vi.stubGlobal('localStorage', { getItem: () => JSON.stringify(data) });
    vi.stubGlobal('fetch', vi.fn(() => { throw new Error('network prohibited'); }));
    expect(loadDemoData().channels[0].name).toBe('Lokaler Test');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('recovers from structurally incomplete stored data', () => {
    const value = { ...createDemoData(), projects: undefined };
    vi.stubGlobal('localStorage', { getItem: () => JSON.stringify(value) });
    expect(loadDemoData().projects).toHaveLength(14);
  });
});
describe('demo graph structure checks', () => {
  it('accepts the provided connected example', () => expect(validateGraph(seedGraph())).toEqual([]));
  it('rejects missing sources, outputs and disconnected inputs', () => {
    const graph = seedGraph(); graph.nodes = graph.nodes.filter(n => !['source', 'result', 'overlay'].includes(n.type)); graph.edges = [];
    const issues = validateGraph(graph);
    expect(issues).toContain('Eine aktive Bildquelle fehlt.');
    expect(issues).toContain('Eine aktive Ausgabe fehlt.');
    expect(issues.some(i => i.includes('Eingang ist nicht verbunden'))).toBe(true);
  });
  it('detects cycles without entering an endless traversal', () => {
    const graph = seedGraph(); graph.edges.push({ from: 'node-5', to: 'node-0' });
    expect(validateGraph(graph)).toContain('Der Graph enthält einen Zyklus.');
  });
  it('does not mutate a saved graph during validation', () => {
    const graph = seedGraph(), original = structuredClone(graph);
    validateGraph(graph); expect(graph).toEqual(original);
  });
  it('rejects dangling edges and ignores cycles confined to disabled nodes', () => {
    const graph = seedGraph();
    graph.edges.push({ from: 'missing', to: 'node-1' });
    expect(validateGraph(graph)).toContain('Eine Verbindung verweist auf einen fehlenden Node.');
    graph.edges = graph.edges.filter(e => e.from !== 'missing');
    graph.nodes[3].enabled = false;
    graph.edges.push({ from: 'node-3', to: 'node-3' });
    expect(validateGraph(graph)).not.toContain('Der Graph enthält einen Zyklus.');
  });
});
describe('local deep links', () => {
  it('defaults to channels and keeps filters alongside the detail tab', () => {
    expect(parseRoute('').module).toBe('channels');
    const route = parseRoute('#/channels/chn-demo-1?tab=Pipeline&place=area-1&q=Stamm');
    expect(route.id).toBe('chn-demo-1'); expect(route.tab).toBe('Pipeline');
    expect(route.params.get('place')).toBe('area-1'); expect(route.params.get('q')).toBe('Stamm');
  });
});
