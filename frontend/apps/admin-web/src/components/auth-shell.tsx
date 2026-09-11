import { useEffect, useState } from 'react';
import { Link, Navigate, Outlet, useRouterState } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { SystemNavigation } from '../features/operations/operations-page';
import { Icon, Modal, Search } from './workspace-ui';
import { api } from '../app/api';
import { findNavItem, navItems } from '../app/navigation';

const modules = [
  { key: 'channels', icon: 'channels', title: 'Kanäle', group: 'BETRIEB' },
  { key: 'results', icon: 'results', title: 'Ergebnisse', group: '' },
  { key: 'pipelines', icon: 'projects', title: 'Projekte & Modelle', group: 'ENGINEERING' },
  { key: 'runtime-pipelines', icon: 'graph', title: 'Pipeline-Editor', group: '' },
  { key: 'edge-devices', icon: 'devices', title: 'Edge-Geräte', group: 'PLATTFORM' },
  { key: 'operations', icon: 'settings', title: 'System', group: '' }
].map(module => ({ ...navItems.find(item => item.key === module.key)!, ...module }));

export function AuthShell() {
  const pathname = useRouterState({ select: state => state.location.pathname });
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('workspace:sidebar-expanded') !== 'true');
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState('');
  const session = useQuery({ queryKey: ['session'], queryFn: api.admin.me, retry: false });
  const channels = useQuery({ queryKey: ['runtime-channels'], queryFn: () => api.channels.list(), enabled: searchOpen });
  const projects = useQuery({ queryKey: ['project-pipelines'], queryFn: api.pipelines.list, enabled: searchOpen });
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); setSearchOpen(value => !value);
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, []);
  useEffect(() => { setSearchOpen(false); }, [pathname]);
  if (session.isLoading) return <div className="screen-center">Sitzung wird geprüft …</div>;
  if (!session.data?.authenticated) return <Navigate to="/login" />;
  const current = modules.find(module => module.key === findNavItem(pathname).key)!;
  const name = session.data.user?.display_name ?? session.data.user?.username ?? 'Operator';
  const initials = name.split(/\s+/).slice(0, 2).map(part => part[0]?.toUpperCase()).join('');
  const matches = (value: string) => value.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
  return <div className={`ws-app production-workspace ${collapsed ? 'ws-app--collapsed' : ''}`}>
    <aside className="ws-sidebar">
      <Link to="/channels" className="ws-brand" aria-label="VISION · Kanäle"><span className="ws-brand-mark">YB<span>●</span></span>{!collapsed && <span><strong>VISION</strong><small>YBBS / INFERENCE SERVER</small></span>}</Link>
      <nav aria-label="Hauptnavigation">{modules.map(module => <div key={module.key}>
        {module.group && <div className="ws-nav-group">{collapsed ? '—' : module.group}</div>}
        <Link to={module.to} title={module.title} aria-label={module.title} className={`ws-nav-link ${current.key === module.key ? 'is-active' : ''}`}><Icon name={module.icon}/>{!collapsed && <span>{module.title}</span>}</Link>
      </div>)}</nav>
      <div className="ws-sidebar-bottom"><button aria-label={collapsed ? 'Menü ausklappen' : 'Menü einklappen'} title={collapsed ? 'Menü ausklappen' : 'Menü einklappen'} onClick={() => { localStorage.setItem('workspace:sidebar-expanded', String(collapsed)); setCollapsed(!collapsed); }}><Icon name="panel"/>{!collapsed && 'Menü einklappen'}</button><div className="ws-user" title={name}><span>{initials}</span>{!collapsed && <div><strong>{name}</strong><small>Angemeldet</small></div>}</div></div>
    </aside>
    <main className="ws-main"><header className="ws-topbar"><div className="ws-module-title"><Icon name={current.icon} size={17}/><h1>{current.title}</h1></div><span className="ws-spacer"/><button className="ws-command-search" onClick={() => setSearchOpen(true)}><Icon name="search" size={15}/><span>Suchen</span><kbd>Strg K</kbd></button></header>
      <div className="ws-module-content">{current.key === 'operations' && <SystemNavigation/>}<Outlet/></div>
      <footer className="ws-app-footer"><span>YBBS · Vision Plattform</span><span>{name}</span></footer>
    </main>
    {searchOpen && <Modal title="Arbeitsbereich öffnen" onClose={() => setSearchOpen(false)}><Search value={search} onChange={setSearch} placeholder="Kanal, Projekt oder Arbeitsbereich …"/><div className="ws-search-results">
      {modules.filter(module => matches(module.title)).map(module => <Link key={module.key} to={module.to} onClick={() => setSearchOpen(false)}><Icon name={module.icon}/><span>{module.title}</span><small>Arbeitsbereich</small></Link>)}
      {channels.data?.items.filter(channel => matches(channel.name)).map(channel => <Link key={channel.channel_id} to="/channels/$channelId" params={{ channelId: channel.channel_id }} onClick={() => setSearchOpen(false)}><Icon name="channels"/><span>{channel.name}</span><small>Kanal</small></Link>)}
      {projects.data?.items.filter(project => matches(project.title)).map(project => <Link key={project.label_project_id} to="/pipelines/$labelProjectId" params={{ labelProjectId: project.label_project_id }} onClick={() => setSearchOpen(false)}><Icon name="projects"/><span>{project.title}</span><small>Projekt</small></Link>)}
      {(channels.isError || projects.isError) && <p role="status">Ein Teil der Suchdaten konnte nicht geladen werden.</p>}
    </div></Modal>}
  </div>;
}
