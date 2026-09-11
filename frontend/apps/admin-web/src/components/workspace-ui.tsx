import { useEffect, useRef, type PropsWithChildren, type ReactNode } from 'react';

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    channels: <><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="12" cy="12" r="4"/><path d="M6 3v2m12-2v2"/></>,
    projects: <><path d="M3 7V5a1 1 0 0 1 1-1h6l2 3h8a1 1 0 0 1 1 1v11H3Z"/><path d="M3 10h18"/></>,
    graph: <><rect x="2" y="8" width="5" height="7" rx="1"/><rect x="17" y="2" width="5" height="7" rx="1"/><rect x="17" y="15" width="5" height="7" rx="1"/><path d="M7 11h5V5h5m-5 6v7h5"/></>,
    results: <><path d="M4 3v18h17M8 16v-4m5 4V6m5 10v-7"/></>,
    devices: <><rect x="4" y="3" width="16" height="7" rx="1"/><rect x="4" y="14" width="16" height="7" rx="1"/><path d="M7 6h1m3 0h6M7 17h1m3 0h6"/></>,
    settings: <><circle cx="12" cy="12" r="4"/><path d="m10 3 4 0 1 3 3 1 3 3v4l-3 1-1 3-3 3h-4l-1-3-3-1-3-3v-4l3-1 1-3Z"/></>,
    chevron: <path d="m9 5 7 7-7 7"/>, close: <path d="m6 6 12 12M6 18 18 6"/>,
    minus: <path d="M5 12h14"/>, fullscreen: <path d="M9 3H3v6m12-6h6v6M3 15v6h6m12-6v6h-6"/>,
    plus: <path d="M12 5v14M5 12h14"/>, search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,
    eyeOff: <><path d="m3 3 18 18M10 5a11 11 0 0 1 12 7s-4 7-10 7S2 12 2 12a18 18 0 0 1 4-5"/></>,
    eye: <><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>,
    hidden: <><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12ZM3 3l18 18"/></>,
    layers: <><path d="m12 3 10 5-10 5L2 8Zm-10 9 10 5 10-5M2 16l10 5 10-5"/></>,
    play: <path d="m7 4 13 8-13 8Z"/>, stop: <rect x="5" y="5" width="14" height="14"/>, pause: <path d="M8 4v16M16 4v16"/>,
    expand: <path d="M9 3H3v6m12-6h6v6M3 15v6h6m12-6v6h-6"/>, fit: <><rect x="4" y="5" width="16" height="14"/><path d="M1 9V2h7m8 0h7v7M1 15v7h7m8 0h7v-7"/></>,
    check: <path d="m4 12 5 5L20 6"/>, alert: <><path d="m12 3 10 18H2Z"/><path d="M12 8v6m0 3v1"/></>,
    refresh: <><path d="M20 10A8 8 0 1 0 19 17M20 3v7h-7"/></>, back: <path d="m10 5-7 7 7 7M3 12h18"/>,
    tree: <><path d="M6 3v14h5M6 7h5"/><rect x="11" y="4" width="9" height="6" rx="1"/><rect x="11" y="14" width="9" height="6" rx="1"/></>,
    grid: <><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></>,
    list: <path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M15 4v16"/></>,
    download: <path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>, user: <><circle cx="12" cy="7" r="4"/><path d="M4 21v-3a8 8 0 0 1 16 0v3"/></>,
    trash: <path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>,
    link: <><path d="m9 15 6-6m-7 9-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-3 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/></>
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.settings}</svg>;
}
export function ToolButton({ icon, label, onClick, active, disabled }: { icon: string; label: string; onClick?: () => void; active?: boolean; disabled?: boolean }) {
  return <button className={`ws-icon-button ${active ? 'is-active' : ''}`} title={label} aria-label={label} aria-pressed={active} onClick={onClick} disabled={disabled}><Icon name={icon}/></button>;
}
export function Badge({ status }: { status: string }) {
  const names: Record<string, string> = { running: 'Läuft', stopped: 'Gestoppt', degraded: 'Warnung', offline: 'Offline' };
  const tone = /running|Bereit|Abgeschlossen|Aktiv|Gespeichert/.test(status) ? 'ok' : /degraded|Prüfen|Daten prüfen/.test(status) ? 'warn' : /offline|Fehler/.test(status) ? 'bad' : 'muted';
  return <span className={`ws-badge ws-badge--${tone}`}><i/>{names[status] ?? status}</span>;
}
export function Tabs({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (tab: string) => void }) {
  return <nav className="ws-tabs" aria-label="Arbeitsansichten">{tabs.map(tab => <button key={tab} aria-current={tab === value ? 'page' : undefined} className={tab === value ? 'is-active' : ''} onClick={() => onChange(tab)}>{tab}</button>)}</nav>;
}
export function Search({ value, onChange, placeholder = 'Suchen …' }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <label className="ws-search"><Icon name="search" size={16}/><input aria-label={placeholder} placeholder={placeholder} value={value} onChange={e => onChange(e.target.value)}/>{value && <button aria-label="Suche löschen" onClick={() => onChange('')}><Icon name="close" size={14}/></button>}</label>;
}
export function Empty({ title, children }: PropsWithChildren<{ title: string }>) { return <div className="ws-empty"><Icon name="search" size={28}/><strong>{title}</strong><p>{children}</p></div>; }
export function Field({ label, children, hint }: PropsWithChildren<{ label: string; hint?: string }>) { return <label className="ws-field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Modal({ title, onClose, children, wide = false }: PropsWithChildren<{ title: string; onClose: () => void; wide?: boolean }>) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement; ref.current?.showModal(); return () => { previous?.focus(); }; }, []);
  return <dialog ref={ref} className={`ws-dialog ${wide ? 'ws-dialog--wide' : ''}`} onCancel={e => { e.preventDefault(); onClose(); }}><header><h2>{title}</h2><ToolButton icon="close" label="Dialog schließen" onClick={onClose}/></header><div className="ws-dialog-body">{children}</div></dialog>;
}
export function Section({ title, children }: PropsWithChildren<{ title: string }>) { return <section className="ws-section"><h3>{title}</h3>{children}</section>; }

export function ResizeHandle({ label, width, onChange }: { label: string; width: number; onChange: (width: number) => void }) {
  const start = useRef<{ x: number; width: number } | null>(null);
  const change = (next: number) => onChange(Math.max(230, Math.min(520, Math.round(next))));
  return <div role="separator" aria-label={label} aria-orientation="vertical" aria-valuemin={230} aria-valuemax={520} aria-valuenow={width} tabIndex={0} className="ws-resize-handle" onPointerDown={e => { start.current = { x: e.clientX, width }; e.currentTarget.setPointerCapture(e.pointerId); }} onPointerMove={e => { if (start.current) change(start.current.width + start.current.x - e.clientX); }} onPointerUp={() => { start.current = null; }} onPointerCancel={() => { start.current = null; }} onKeyDown={e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); change(width + (e.key === 'ArrowLeft' ? 10 : -10)); } }}/>
}
