import { useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { RuntimePlaces, RuntimeChannelSummary } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { Field, Icon, Modal } from '../../components/workspace-ui';

export function usePlaces() { return useQuery({ queryKey: ['runtime-places'], queryFn: api.places.list, staleTime: 15000 }); }
export function matchesPlace(channel: Pick<RuntimeChannelSummary, 'facility_id'>, selection: string, places?: RuntimePlaces) {
  if (!selection) return true;
  if (selection === 'unassigned') return !channel.facility_id;
  return channel.facility_id === selection || Boolean(places?.facilities.some(f => f.id === channel.facility_id && f.area_id === selection));
}
export function PlaceTree({ value, onChange, channels = [] }: { value: string; onChange: (value: string) => void; channels?: RuntimeChannelSummary[] }) {
  const query = usePlaces();
  const [closed, setClosed] = useState<string[]>([]);
  const [focus, setFocus] = useState(value);
  const rows = [{id: '', name: 'Alle Kanäle', level: 1}, {id: 'unassigned', name: 'Nicht zugeordnet', level: 1}, ...(query.data?.areas.flatMap(area => [{id: area.id, name: area.name, level: 1}, ...(!closed.includes(area.id) ? query.data.facilities.filter(f => f.area_id === area.id).map(f => ({id: f.id, name: f.name, level: 2})) : [])]) ?? [])];
  const toggle = (id: string) => setClosed(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  return <aside className="ws-place-tree"><div className="ws-inspector-title">Anlagenstruktur<Link className="ws-icon-button" to="/operations/places" title="Struktur verwalten" aria-label="Struktur verwalten"><Icon name="settings" size={15}/></Link></div>
    <div className="ws-tree-items ws-scroll" role="tree" aria-label="Technische Plätze">{rows.map((row, index) => {
      const area = query.data?.areas.some(a => a.id === row.id);
      const expanded = area ? !closed.includes(row.id) : undefined;
      return <button key={row.id} role="treeitem" aria-level={row.level} aria-selected={value === row.id} aria-expanded={expanded} tabIndex={focus === row.id ? 0 : -1} className={`${value === row.id ? 'is-selected' : ''} ${row.level === 2 ? 'ws-tree-child' : ''}`} onFocus={() => setFocus(row.id)} onClick={() => onChange(row.id)} onKeyDown={event => {
        let next = index;
        if (event.key === 'ArrowDown') next = Math.min(rows.length - 1, index + 1);
        else if (event.key === 'ArrowUp') next = Math.max(0, index - 1);
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = rows.length - 1;
        else if (event.key === 'ArrowRight' && area) { if (!expanded) toggle(row.id); else next = Math.min(rows.length - 1, index + 1); }
        else if (event.key === 'ArrowLeft') { if (area && expanded) toggle(row.id); else if (row.level === 2) next = rows.findIndex(r => r.id === query.data?.facilities.find(f => f.id === row.id)?.area_id); }
        else return;
        event.preventDefault();
        (event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="treeitem"]')[next])?.focus();
      }}>
        {area ? <span className={`ws-tree-arrow ${expanded ? 'is-open' : ''}`} onClick={event => {event.stopPropagation(); toggle(row.id);}}><Icon name="chevron" size={12}/></span> : <Icon name={row.level === 2 ? 'devices' : 'channels'} size={14}/>}
        <span>{row.name}</span><small>{channels.filter(channel => matchesPlace(channel, row.id, query.data)).length}</small>
      </button>;
    })}{query.isError && <p role="alert">Struktur nicht verfügbar. <button onClick={() => void query.refetch()}>Erneut laden</button></p>}</div><footer>Bereich → Anlage → Kanäle</footer>
  </aside>;
}
export function FacilitySelect({ value, onChange }: { value: string; onChange: (id: string, name: string) => void }) {
  const query = usePlaces();
  return <Field label="Bereich / Anlage"><select value={value} disabled={query.isPending || query.isError} onChange={e => onChange(e.target.value, query.data?.facilities.find(f => f.id === e.target.value)?.name ?? '')}>
    <option value="">Nicht zugeordnet (bisherige Ortsbezeichnung)</option>
    {value && !query.data?.facilities.some(f => f.id === value) && <option value={value}>Aktuelle Zuordnung</option>}
    {query.data?.areas.map(area => <optgroup key={area.id} label={area.name}>{query.data.facilities.filter(f => f.area_id === area.id).map(f => <option key={f.id} value={f.id}>{f.name}</option>)}</optgroup>)}
  </select>{query.isError && <small>Struktur konnte nicht geladen werden.</small>}</Field>;
}
type Draft = { kind: 'areas' | 'facilities'; id?: string; name: string; area_id: string };
export function PlacesPage() {
  const query = usePlaces(); const client = useQueryClient();
  const session = useQuery({queryKey: ['session'], queryFn: api.admin.me});
  const canWrite = ['platform_admin', 'ml_engineer', 'operator'].includes(session.data?.user?.role ?? '');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [removal, setRemoval] = useState<Draft | null>(null);
  const mutation = useMutation({ mutationFn: (d: Draft & { remove?: boolean }) => d.remove ? api.places.remove(d.kind, d.id!) : api.places.save(d.kind, {name: d.name, ...(d.kind === 'facilities' ? {area_id: d.area_id} : {})}, d.id), onSuccess: data => {
    client.setQueryData(['runtime-places'], data); void client.invalidateQueries({queryKey: ['runtime-channels']}); void client.invalidateQueries({queryKey: ['runtime-channel']}); setDraft(null); setRemoval(null);
  }});
  const actions = (d: Draft) => canWrite && <div className="actions-row"><button onClick={() => {mutation.reset(); setDraft(d);}}>Bearbeiten</button><button onClick={() => {mutation.reset(); setRemoval(d);}}>Entfernen</button></div>;
  const error = mutation.isError ? describeError(mutation.error, 'Änderung fehlgeschlagen') : null;
  return <div className="workspace-page"><header className="workspace-toolbar"><Link to="/channels">← Kanäle</Link><strong>Bereiche und Anlagen</strong>{canWrite && <button onClick={() => {mutation.reset(); setDraft({kind: 'areas', name: '', area_id: ''});}}>Bereich hinzufügen</button>}</header>
    {query.isError && <p role="alert">Stammdaten konnten nicht geladen werden. <button onClick={() => void query.refetch()}>Erneut laden</button></p>}
    <div className="workspace-tab-body"><table className="workspace-table"><thead><tr><th>Bereich / Anlage</th><th>Verwalten</th></tr></thead><tbody>{query.data?.areas.map(a => <PlaceRows key={a.id} area={a} places={query.data} actions={actions} onAdd={() => {mutation.reset(); setDraft({kind: 'facilities', area_id: a.id, name: ''});}} canWrite={canWrite}/>)}</tbody></table>
    {!query.isPending && !query.isError && !query.data?.areas.length && <p>Noch keine Bereiche angelegt. Bestehende Kanäle finden Sie unter „Nicht zugeordnet“.</p>}</div>
    {draft && <Modal title={draft.kind === 'areas' ? 'Bereich' : 'Anlage'} onClose={() => !mutation.isPending && setDraft(null)}><form className="panel-stack" onSubmit={e => {e.preventDefault(); mutation.mutate(draft);}}>
      <Field label="Bezeichnung"><input autoFocus required maxLength={160} value={draft.name} onChange={e => setDraft({...draft, name: e.target.value})}/></Field>
      {draft.kind === 'facilities' && <Field label="Bereich"><select required value={draft.area_id} onChange={e => setDraft({...draft, area_id: e.target.value})}>{query.data?.areas.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>}
      {error && <p role="alert">{error.title}: {error.description}</p>}<button disabled={mutation.isPending || !draft.name.trim()}>Speichern</button>
    </form></Modal>}
    {removal && <Modal title="Struktureintrag entfernen" onClose={() => !mutation.isPending && setRemoval(null)}><p>„{removal.name}“ entfernen? Zugeordnete Kanäle oder Anlagen verhindern das Entfernen.</p>{error && <p role="alert">{error.description ?? error.title}</p>}<button disabled={mutation.isPending} onClick={() => mutation.mutate({...removal, remove: true})}>Entfernen</button></Modal>}
  </div>;
}
function PlaceRows({area, places, actions, onAdd, canWrite}: {area: RuntimePlaces['areas'][number]; places: RuntimePlaces; actions: (d: Draft) => React.ReactNode; onAdd: () => void; canWrite: boolean}) {
  return <><tr><td><strong>{area.name}</strong></td><td>{actions({kind: 'areas', ...area, area_id: ''})}{canWrite && <button onClick={onAdd}>Anlage hinzufügen</button>}</td></tr>{places.facilities.filter(f => f.area_id === area.id).map(f => <tr key={f.id}><td>↳ {f.name}</td><td>{actions({kind: 'facilities', ...f})}</td></tr>)}</>;
}
