import { useCallback, useEffect, useRef, useState } from 'react';
import { DemoContext, parseRoute } from './context';
import { createDemoData, loadDemoData, storageKey, uniqueId, type Channel, type DemoData, type Graph } from './model';
import { ChannelDetail, ChannelList } from './channel-pages';
import { ProjectDetail, ProjectList } from './project-pages';
import { EdgeWorkspace, PipelineLibrary, SystemWorkspace } from './system-pages';
import { RecordsWorkspace } from './records-workspace';
import { Empty, Icon, Modal, Search, ToolButton } from './ui';

const modules = [
  { id: 'channels', title: 'Kanäle', icon: 'channels', group: 'BETRIEB' },
  { id: 'results', title: 'Ergebnisse', icon: 'results', group: '' },
  { id: 'pipelines', title: 'Projekte & Modelle', icon: 'projects', group: 'ENGINEERING' },
  { id: 'runtime-pipelines', title: 'Pipeline-Editor', icon: 'graph', group: '' },
  { id: 'edge-devices', title: 'Edge-Geräte', icon: 'devices', group: 'PLATTFORM' },
  { id: 'system', title: 'System', icon: 'settings', group: '' }
];
export function DemoApp() {
  const [data, setData] = useState(loadDemoData);
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  const collapsed = data.settings['layout:sidebar-collapsed'] === 'true';
  const [toast, setToast] = useState('');
  const [resetOpen, setResetOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [pendingPath, setPendingPath] = useState<string | null>(null);
  const [graphDrafts, setGraphDrafts] = useState<Record<string, Graph>>({});
  const [channelDrafts, setChannelDrafts] = useState<Record<string, Channel>>({});
  const [scenario, setScenario] = useState('normal');
  const toastTimer = useRef<ReturnType<typeof setTimeout>>();
  const storageWarning = useRef(false);
  const notify = useCallback((message: string) => { setToast(message); clearTimeout(toastTimer.current); toastTimer.current = setTimeout(() => setToast(''), 5000); }, []);
  const update = useCallback((updater: (data: DemoData) => DemoData, message?: string) => {
    setData(d => { const next = updater(d); return message ? { ...next, audit: [{ id: uniqueId('audit'), name: message, status: 'Bereit', detail: 'Lokaler Demo-Benutzer', time: new Date().toLocaleString('de-AT') }, ...next.audit].slice(0, 150) } : next; });
    if (message) notify(message);
  }, [notify]);
  const setCollapsed = (value: boolean) => update(d => ({ ...d, settings: { ...d.settings, 'layout:sidebar-collapsed': String(value) } }));
  const setGraphDraft = useCallback((id: string, graph: Graph | null) => setGraphDrafts(d => { const next = { ...d }; if (graph) next[id] = graph; else delete next[id]; return next; }), []);
  const setChannelDraft = useCallback((id: string, channel: Channel | null) => setChannelDrafts(d => { const next = { ...d }; if (channel) next[id] = channel; else delete next[id]; return next; }), []);
  const hasDrafts = Object.keys(graphDrafts).length + Object.keys(channelDrafts).length > 0;
  const commitNavigation = useCallback((path: string) => { history.pushState(null, '', `#${path}`); setRoute(parseRoute(`#${path}`)); setSearchOpen(false); }, []);
  const navigate = useCallback((path: string) => {
    const next = parseRoute(`#${path}`);
    if (hasDrafts && (next.module !== route.module || next.id !== route.id)) { setPendingPath(path); return; }
    commitNavigation(path);
  }, [hasDrafts, route.module, route.id, commitNavigation]);
  useEffect(() => { try { localStorage.setItem(storageKey, JSON.stringify(data)); } catch { if (!storageWarning.current) { storageWarning.current = true; notify('Browserspeicher nicht verfügbar. Änderungen gelten nur bis zum Neuladen.'); } } }, [data, notify]);
  useEffect(() => {
    const onHistory = () => { const next = parseRoute(location.hash); if (hasDrafts && (next.id !== route.id || next.module !== route.module)) { setPendingPath(location.hash.slice(1)); history.replaceState(null, '', `#/${route.module}${route.id ? `/${route.id}` : ''}?${route.params}`); } else setRoute(next); };
    const beforeUnload = (e: BeforeUnloadEvent) => { if (hasDrafts) { e.preventDefault(); e.returnValue = ''; } };
    const keys = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setSearchOpen(true); } };
    window.addEventListener('popstate', onHistory); window.addEventListener('hashchange', onHistory); window.addEventListener('beforeunload', beforeUnload); window.addEventListener('keydown', keys);
    return () => { window.removeEventListener('popstate', onHistory); window.removeEventListener('hashchange', onHistory); window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('keydown', keys); };
  }, [hasDrafts, route]);
  useEffect(() => { const timer = setInterval(() => { setData(d => {
    const active = d.channels.filter(c => d.settings[`capture:${c.id}`] === 'running');
    if (!active.length) return d;
    return { ...d, records: { ...d.records, captures: [...active.map(c => ({ id: uniqueId('capture'), name: `Intervallaufnahme · ${c.name}`, status: 'Bereit', detail: c.id, target: d.settings[`capture-target:${c.id}`] ?? d.projects[0]?.name, time: new Date().toLocaleString('de-AT') })), ...d.records.captures].slice(0, 200) } };
  }); }, 5000); return () => { clearInterval(timer); clearTimeout(toastTimer.current); }; }, []);
  const resolveDrafts = (save: boolean) => {
    if (save && Object.values(channelDrafts).some(c => c.name.trim().length < 3)) { notify('Kanalname muss mindestens drei Zeichen enthalten.'); return; }
    if (save) update(d => ({ ...d, channels: d.channels.map(c => channelDrafts[c.id] ?? c), graphs: { ...d.graphs, ...Object.fromEntries(Object.entries(graphDrafts).map(([id, graph]) => [id, { ...graph, version: (d.graphs[id]?.version ?? 0) + 1 }])) } }), 'Entwürfe lokal gespeichert');
    setGraphDrafts({}); setChannelDrafts({}); if (pendingPath) commitNavigation(pendingPath); setPendingPath(null);
  };
  const activeModule = modules.find(m => m.id === route.module);
  return <DemoContext.Provider value={{ data, update, route, navigate, notify, graphDrafts, setGraphDraft, channelDrafts, setChannelDraft }}><div className={`ws-app ${collapsed ? 'ws-app--collapsed' : ''}`}>
    <aside className="ws-sidebar"><button className="ws-brand" onClick={() => navigate('/channels')} title="Kanäle öffnen"><span className="ws-brand-mark">YB<span>●</span></span>{!collapsed && <span><strong>VISION</strong><small>YBBS / INFERENCE SERVER</small></span>}</button><nav aria-label="Hauptnavigation">{modules.map(m => <div key={m.id}>{m.group && <p className="ws-nav-group">{collapsed ? '—' : m.group}</p>}<a href={`#/${m.id}`} className={`ws-nav-link ${route.module === m.id ? 'is-active' : ''}`} aria-current={route.module === m.id ? 'page' : undefined} title={m.title} onClick={e => { if (e.ctrlKey || e.metaKey || e.shiftKey) return; e.preventDefault(); navigate(`/${m.id}`); }}><Icon name={m.icon}/>{!collapsed && <span>{m.title}</span>}{!collapsed && m.id === 'channels' && <small>{data.channels.length}</small>}</a></div>)}</nav><div className="ws-sidebar-bottom"><button onClick={() => setCollapsed(!collapsed)} title={collapsed ? 'Menü ausklappen' : 'Menü einklappen'} aria-label={collapsed ? 'Menü ausklappen' : 'Menü einklappen'}><Icon name="panel"/>{!collapsed && 'Menü einklappen'}</button><div className="ws-user"><span>OP</span>{!collapsed && <div><strong>Lokaler Benutzer</strong><small>Demo-Arbeitsplatz</small></div>}</div></div></aside>
    <main className="ws-main"><header className="ws-topbar"><div className="ws-module-title"><Icon name={activeModule?.icon ?? 'settings'} size={17}/><h1>{activeModule?.title ?? 'Arbeitsbereich'}</h1></div><span className="ws-demo-badge">LOKALE VORSCHAU</span><span className="ws-spacer"/><button className="ws-command-search" onClick={() => setSearchOpen(true)}><Icon name="search" size={15}/><span>Suchen</span><kbd>Strg K</kbd></button><select className="ws-scenario" aria-label="Demo-Szenario" value={scenario} onChange={e => { const s = e.target.value; setScenario(s); if (s === 'normal') { const base = createDemoData(); update(d => ({ ...d, channels: d.channels.map((c, i) => ({ ...c, runtime: base.channels[i % base.channels.length].runtime, stream: base.channels[i % base.channels.length].stream })) }), 'Normalbetrieb geladen'); } else if (s === 'offline') update(d => ({ ...d, channels: d.channels.map(c => ({ ...c, runtime: 'offline', stream: 'offline' })) }), 'Verbindungsunterbrechung simuliert'); else if (s === 'stopped') update(d => ({ ...d, channels: d.channels.map(c => ({ ...c, runtime: 'stopped', stream: 'running' })) }), 'Runtime gestoppt, Streams bleiben aktiv'); }}><option value="normal">Normalbetrieb</option><option value="offline">Quelle offline</option><option value="stopped">Runtime gestoppt</option></select><ToolButton icon="refresh" label="Demo zurücksetzen" onClick={() => setResetOpen(true)}/></header>
      <div className="ws-module-content">{route.module === 'channels' ? route.id ? <ChannelDetail/> : <ChannelList/> : route.module === 'pipelines' ? route.id ? <ProjectDetail key={route.id}/> : <ProjectList/> : route.module === 'runtime-pipelines' ? <PipelineLibrary/> : route.module === 'results' ? <RecordsWorkspace kind="results"/> : route.module === 'edge-devices' ? <EdgeWorkspace/> : route.module === 'system' ? <SystemWorkspace/> : <Empty title="Arbeitsbereich nicht gefunden"><button onClick={() => navigate('/channels')}>Kanäle öffnen</button></Empty>}</div>
      <footer className="ws-app-footer"><span><i className="ws-dot"/>Lokale Beispieldaten · kein Produktivzugriff</span><span>CV Ybbs <span className="ws-separator">/</span> Workspace Preview 01</span></footer>
    </main>{toast && <div className="ws-toast" role="status"><Icon name="check" size={17}/><span>{toast}</span><ToolButton icon="close" label="Meldung schließen" onClick={() => setToast('')}/></div>}
    {resetOpen && <Modal title="Lokale Demo zurücksetzen?" onClose={() => setResetOpen(false)}><p>Alle lokalen Änderungen, Zuordnungen, Overlay-Einstellungen und Entwürfe werden auf die Beispieldaten zurückgesetzt.</p><div className="ws-dialog-actions"><button onClick={() => setResetOpen(false)}>Abbrechen</button><button className="ws-danger" onClick={() => { setData(createDemoData()); setGraphDrafts({}); setChannelDrafts({}); setScenario('normal'); setResetOpen(false); commitNavigation('/channels'); notify('Demo auf Ausgangszustand zurückgesetzt'); }}>Zurücksetzen</button></div></Modal>}
    {pendingPath && <Modal title="Ungespeicherte Änderungen" onClose={() => setPendingPath(null)}><p>Vor dem Wechsel können die lokalen Kanal- und Graphentwürfe gespeichert oder verworfen werden.</p><div className="ws-dialog-actions"><button onClick={() => setPendingPath(null)}>Abbrechen</button><button onClick={() => resolveDrafts(false)}>Verwerfen</button><button className="ws-primary" onClick={() => resolveDrafts(true)}>Speichern & wechseln</button></div></Modal>}
    {searchOpen && <Modal title="Arbeitsbereich oder Objekt öffnen" onClose={() => setSearchOpen(false)}><Search value={search} onChange={setSearch} placeholder="Kanal, Projekt oder Modul suchen …"/><div className="ws-search-results">{[...modules.map(m => ({ name: m.title, path: `/${m.id}`, type: 'Modul' })), ...data.channels.map(c => ({ name: c.name, path: `/channels/${c.id}`, type: 'Kanal' })), ...data.projects.map(p => ({ name: p.name, path: `/pipelines/${p.id}`, type: 'Projekt' }))].filter(item => item.name.toLowerCase().includes(search.toLowerCase())).map(item => <button key={item.path} onClick={() => navigate(item.path)}><span>{item.name}</span><small>{item.type}</small><Icon name="chevron" size={14}/></button>)}</div></Modal>}
  </div></DemoContext.Provider>;
}
