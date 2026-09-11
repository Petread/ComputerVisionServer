import { z } from 'zod';

export type State = 'running' | 'stopped' | 'degraded' | 'offline';
export type Place = { id: string; name: string; areaId?: string };
export type Channel = { id: string; name: string; facilityId: string; source: string; address: string; target: string; pipeline: string; runtime: State; stream: State; description: string };
export type Project = { id: string; name: string; type: string; images: number; readiness: number; models: number; training: string; deployments: number };
export type GraphNode = { id: string; type: string; title: string; x: number; y: number; enabled: boolean; threshold: number; view?: string; referencePoints?: { x: number; y: number }[] };
export type Graph = { nodes: GraphNode[]; edges: { from: string; to: string }[]; version: number; activeVersion: number };
export type RecordRow = { id: string; name: string; status: string; detail: string; time: string; target?: string };
export type DemoData = { schema: 1; areas: Place[]; facilities: Place[]; channels: Channel[]; projects: Project[]; graphs: Record<string, Graph>; records: Record<string, RecordRow[]>; overlays: Record<string, string[]>; settings: Record<string, string>; audit: RecordRow[] };

export const nodeTypes = [
  { type: 'source', title: 'Kamerabild', group: 'Quelle', detail: 'Bildquelle des Kanals', color: '#5895cb' },
  { type: 'transform', title: 'Bild entzerren', group: 'Vorverarbeitung', detail: 'Kalibrierung und Bildraum', color: '#ad91d7' },
  { type: 'model', title: 'Segmentierung', group: 'Inferenz', detail: 'Objekte und Konturen erkennen', color: '#58b89d' },
  { type: 'measure', title: 'Geometrie messen', group: 'Analyse', detail: 'Abmessungen und Referenzen', color: '#d8b066' },
  { type: 'overlay', title: 'Overlay zeichnen', group: 'Ausgabe', detail: 'Geometrie im Livebild anzeigen', color: '#7ba2b7' },
  { type: 'result', title: 'Ergebnis speichern', group: 'Ausgabe', detail: 'Messwerte in Historie ablegen', color: '#7ba2b7' }
];
export const layerDefinitions = [
  { id: 'contour', label: 'Objektkontur', group: 'Erkennung', color: '#55e6bb' },
  { id: 'boxes', label: 'Begrenzungsrahmen', group: 'Erkennung', color: '#65bbff' },
  { id: 'axis', label: 'Mittellinie & Referenz', group: 'Geometrie', color: '#f2cf6f' },
  { id: 'measure', label: 'Messlinien', group: 'Geometrie', color: '#dca4ff' },
  { id: 'labels', label: 'Beschriftungen', group: 'Information', color: '#ffffff' },
  { id: 'roi', label: 'Prüfbereich', group: 'Information', color: '#97afc3' }
];
export const defaultLayers = ['contour', 'axis', 'measure', 'labels'];
export function seedGraph(): Graph {
  return { nodes: nodeTypes.map((n, i) => ({ id: `node-${i}`, type: n.type, title: n.title, x: 45 + (i % 3) * 250, y: 90 + Math.floor(i / 3) * 210, enabled: true, threshold: 0.85 })), edges: nodeTypes.slice(1).map((_, i) => ({ from: `node-${i}`, to: `node-${i + 1}` })), version: 4, activeVersion: 4 };
}
const names = ['Stammausrichtung · Einlauf', 'Stammkontur · Vorzentrierung', 'Schnittbild · Hauptsäge', 'Brettinspektion · Oberseite', 'Brettinspektion · Unterseite', 'Kantenkontrolle · Linie 2', 'Sortierung · Qualität', 'Paketkontrolle · Auslauf', 'Versuchskamera · Technikum', 'Referenzkamera · Kalibrierung', 'Fördertechnik · Übergabe', 'Materialfluss · Rückführung'];
const projectNames = ['Stammausrichtung vor dem ersten Vorzentrierungs-Walzenpaar', 'Brettoberfläche – Defekte und Aststellen', 'Stirnflächen und Jahresringstruktur', 'Schnittkantenprüfung Linie 2', 'Paketvollständigkeit', 'Rindenanteil und Fremdkörper', 'Vermessung – Stammkontur', 'Sortiermerkmale Qualität A/B', 'Oberflächenanomalien', 'Werkzeugverschleiß', 'Materialflussprüfung', 'Referenzmarken und Kalibrierung', 'Fehlstellen an sägerauen Oberflächen', 'Holzartenerkennung'];
export function createDemoData(): DemoData {
  const channels: Channel[] = names.map((name, i) => ({ id: `chn-demo-${i + 1}`, name, facilityId: i === 9 ? '' : ['fac-1', 'fac-1', 'fac-2', 'fac-3', 'fac-3', 'fac-3', 'fac-4', 'fac-4', 'fac-5', '', 'fac-2', 'fac-4'][i], source: i === 8 ? 'Testvideo' : 'GigE Vision', address: `192.0.2.${20 + i}`, target: i % 3 === 0 ? 'Edge · YB-EDGE-01' : 'Server · YB-CV-01', pipeline: i < 3 ? 'Stammgeometrie' : i < 7 ? 'Oberflächenprüfung' : 'Qualitätskontrolle', runtime: i === 5 ? 'degraded' : i === 8 || i === 9 ? 'stopped' : i === 10 ? 'offline' : 'running', stream: i === 10 ? 'offline' : i === 9 ? 'stopped' : 'running', description: 'Optische Prüfung im laufenden Produktionsprozess.' }));
  const records: Record<string, RecordRow[]> = {};
  for (const key of ['results', 'captures', 'training', 'deployments', 'snapshots', 'registry', 'templates', 'batch', 'access', 'integrations', 'workers', 'audit']) {
    records[key] = Array.from({ length: key === 'results' ? 36 : 7 }, (_, i) => ({ id: `${key}-${i + 1}`, name: key === 'results' ? channels[i % channels.length].name : `${({ training: 'Trainingslauf', deployments: 'Release', captures: 'Aufnahme', snapshots: 'Dataset-Snapshot', registry: 'Modellversion', templates: 'Modellvorlage', batch: 'Batchauftrag', access: 'Benutzer', integrations: 'Integration', workers: 'Worker', audit: 'Konfiguration gespeichert' } as Record<string, string>)[key]} ${String(i + 1).padStart(3, '0')}`, status: i === 2 ? 'Prüfen' : key === 'training' && i === 0 ? 'Läuft' : 'Bereit', detail: key === 'results' || key === 'captures' ? channels[i % channels.length].id : ['Stammgeometrie', 'Oberflächenprüfung', 'Qualitätskontrolle'][i % 3], time: `07.09.2026, 14:${String(38 - i % 30).padStart(2, '0')}:12` }));
  }
  return { schema: 1, channels, areas: [{ id: 'area-1', name: 'Rundholzplatz' }, { id: 'area-2', name: 'Sägewerk' }, { id: 'area-3', name: 'Weiterverarbeitung' }, { id: 'area-4', name: 'Technikum' }], facilities: [{ id: 'fac-1', name: 'Stammeinzug', areaId: 'area-1' }, { id: 'fac-2', name: 'Hauptsäge', areaId: 'area-2' }, { id: 'fac-3', name: 'Besäumlinie', areaId: 'area-2' }, { id: 'fac-4', name: 'Sortieranlage', areaId: 'area-3' }, { id: 'fac-5', name: 'Versuchsstand', areaId: 'area-4' }], projects: projectNames.map((name, i) => ({ id: `prj-${i + 1}`, name, type: ['Segmentierung', 'Objekterkennung', 'Klassifikation', 'Anomalie'][i % 4], images: 2480 + i * 317, readiness: i % 4 === 2 ? 64 : 98, models: 2 + i % 3, training: i === 1 ? 'Läuft · 62 %' : i % 4 === 2 ? 'Daten prüfen' : 'Abgeschlossen', deployments: i % 4 === 2 ? 0 : 1 })), graphs: Object.fromEntries([...channels.map(c => c.id), 'template-1', 'template-2', 'template-3'].map(id => [id, seedGraph()])), records, overlays: {}, settings: {}, audit: [] };
}
export const storageKey = 'cv-ybbs-workspace-demo-v1';
const placeSchema = z.object({ id: z.string(), name: z.string(), areaId: z.string().optional() });
const stateSchema = z.enum(['running', 'stopped', 'degraded', 'offline']);
const rowSchema = z.object({ id: z.string(), name: z.string(), status: z.string(), detail: z.string(), time: z.string(), target: z.string().optional() });
const storedDataSchema = z.object({
  schema: z.literal(1), areas: z.array(placeSchema), facilities: z.array(placeSchema),
  channels: z.array(z.object({ id: z.string(), name: z.string(), facilityId: z.string(), source: z.string(), address: z.string(), target: z.string(), pipeline: z.string(), runtime: stateSchema, stream: stateSchema, description: z.string() })),
  projects: z.array(z.object({ id: z.string(), name: z.string(), type: z.string(), images: z.number().nonnegative(), readiness: z.number().min(0).max(100), models: z.number().int().nonnegative(), training: z.string(), deployments: z.number().int().nonnegative() })),
  graphs: z.record(z.object({ nodes: z.array(z.object({ id: z.string(), type: z.enum(['source', 'transform', 'model', 'measure', 'overlay', 'result']), title: z.string(), x: z.number().finite(), y: z.number().finite(), enabled: z.boolean(), threshold: z.number().min(0).max(1), view: z.string().optional(), referencePoints: z.array(z.object({ x: z.number().min(0).max(100), y: z.number().min(0).max(100) })).max(4).optional() })), edges: z.array(z.object({ from: z.string(), to: z.string() })), version: z.number().int().nonnegative(), activeVersion: z.number().int().nonnegative() })),
  records: z.record(z.array(rowSchema)), overlays: z.record(z.array(z.string())), settings: z.record(z.string()), audit: z.array(rowSchema)
});
export function loadDemoData(): DemoData {
  try { const parsed = storedDataSchema.safeParse(JSON.parse(localStorage.getItem(storageKey) ?? 'null')); if (parsed.success) return parsed.data; } catch { /* A fresh fixture is safe when browser storage is unavailable. */ }
  return createDemoData();
}
export function placeName(data: DemoData, facilityId: string) {
  const facility = data.facilities.find(f => f.id === facilityId);
  return facility ? `${data.areas.find(a => a.id === facility.areaId)?.name ?? 'Bereich'} / ${facility.name}` : 'Nicht zugeordnet';
}
export function filterChannels(data: DemoData, place: string, search: string, status = '') {
  return data.channels.filter(c => (!place || (place === 'unassigned' ? !c.facilityId : c.facilityId === place || data.facilities.some(f => f.areaId === place && f.id === c.facilityId))) && (!status || c.runtime === status) && `${c.name} ${placeName(data, c.facilityId)} ${c.pipeline} ${c.target}`.toLocaleLowerCase('de').includes(search.toLocaleLowerCase('de')));
}
export function validateGraph(graph: Graph): string[] {
  const enabled = graph.nodes.filter(n => n.enabled);
  const issues: string[] = [];
  if (!enabled.some(n => n.type === 'source')) issues.push('Eine aktive Bildquelle fehlt.');
  if (!enabled.some(n => n.type === 'result' || n.type === 'overlay')) issues.push('Eine aktive Ausgabe fehlt.');
  for (const node of enabled) if (node.type !== 'source' && !graph.edges.some(e => e.to === node.id && enabled.some(n => n.id === e.from))) issues.push(`${node.title}: Eingang ist nicht verbunden.`);
  const activeIds = new Set(enabled.map(n => n.id));
  if (graph.edges.some(e => !graph.nodes.some(n => n.id === e.from) || !graph.nodes.some(n => n.id === e.to))) issues.push('Eine Verbindung verweist auf einen fehlenden Node.');
  const visiting = new Set<string>(), visited = new Set<string>();
  const visit = (id: string): boolean => {
    if (visiting.has(id)) return true;
    if (visited.has(id)) return false;
    visiting.add(id);
    if (graph.edges.filter(e => e.from === id && activeIds.has(e.to)).some(e => visit(e.to))) return true;
    visiting.delete(id); visited.add(id); return false;
  };
  if (enabled.some(n => visit(n.id))) issues.push('Der Graph enthält einen Zyklus.');
  return [...new Set(issues)];
}
export function deletionIssue(data: DemoData, id: string) {
  if (data.areas.some(a => a.id === id) && data.facilities.some(f => f.areaId === id)) return 'Der Bereich enthält noch Anlagen.';
  if (data.channels.some(c => c.facilityId === id)) return 'Dieser Anlage sind noch Kanäle zugeordnet.';
  return null;
}
export function uniqueId(prefix: string) { return `${prefix}-${crypto.randomUUID()}`; }
