import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useDemo } from './context';
import { defaultLayers, layerDefinitions, type Channel } from './model';
import { Badge, Icon, ToolButton, ResizeHandle } from './ui';

// Deterministic canvas source: no camera, network, external images, or invented angle target.
function drawScene(ctx: CanvasRenderingContext2D, time: number, layers: string[], opacity = 1) {
  const width = 1200, height = 675;
  const bg = ctx.createLinearGradient(0, 0, 0, height); bg.addColorStop(0, '#202b30'); bg.addColorStop(1, '#121c22');
  ctx.fillStyle = bg; ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = '#2a353b'; ctx.fillRect(0, 58, width, 56); ctx.fillRect(0, 545, width, 80);
  for (let i = 0; i < 13; i++) {
    const x = i * 108 - 35;
    const metal = ctx.createLinearGradient(x, 0, x + 72, 0); metal.addColorStop(0, '#354249'); metal.addColorStop(.4, '#67716f'); metal.addColorStop(.8, '#3d4b50'); metal.addColorStop(1, '#222e34');
    ctx.fillStyle = metal; ctx.fillRect(x, 120, 72, 425);
    ctx.strokeStyle = '#879289'; ctx.lineWidth = 1; ctx.strokeRect(x + 2, 120, 68, 425);
    ctx.fillStyle = '#162229'; ctx.fillRect(x - 6, 92, 85, 28); ctx.fillRect(x - 6, 545, 85, 26);
    ctx.fillStyle = '#75827b'; ctx.fillRect(x + 30, 99, 9, 12);
  }
  const sway = Math.sin(time / 1800) * 5;
  ctx.save(); ctx.translate(0, sway);
  const log = new Path2D('M-40 219 Q150 211 320 236 Q490 257 680 243 Q930 232 1240 265 L1240 443 Q1000 411 770 420 Q580 438 365 411 Q180 394 -40 409 Z');
  ctx.shadowColor = '#000b'; ctx.shadowBlur = 24; ctx.shadowOffsetY = 18;
  const wood = ctx.createLinearGradient(0, 205, 0, 447); wood.addColorStop(0, '#5c402a'); wood.addColorStop(.16, '#a98556'); wood.addColorStop(.4, '#c2a373'); wood.addColorStop(.72, '#937046'); wood.addColorStop(1, '#4e3626');
  ctx.fillStyle = wood; ctx.fill(log); ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
  ctx.save(); ctx.clip(log);
  for (let i = 0; i < 115; i++) {
    ctx.strokeStyle = i % 3 === 0 ? '#35271c65' : '#dec49c45'; ctx.lineWidth = i % 5 === 0 ? 3 : 1;
    ctx.beginPath();
    for (let x = -20; x < 1240; x += 12) { const y = 215 + i * 2.1 + Math.sin(x / 97 + i * 1.7) * 5 + Math.sin(x / 261) * 12; if (x === -20) ctx.moveTo(x, y); else ctx.lineTo(x, y); }
    ctx.stroke();
  }
  for (const [x, y] of [[245, 310], [706, 355], [995, 292]]) { for (let r = 20; r > 0; r -= 4) { ctx.strokeStyle = '#4b32279c'; ctx.lineWidth = 2; ctx.beginPath(); ctx.ellipse(x, y, r * 2, r * .65, .1, 0, Math.PI * 2); ctx.stroke(); } }
  ctx.restore();
  ctx.globalAlpha = opacity;
  if (layers.includes('contour')) { ctx.strokeStyle = '#55e6bb'; ctx.lineWidth = 2.5; ctx.stroke(log); ctx.fillStyle = '#55e6bb0c'; ctx.fill(log); }
  if (layers.includes('boxes')) { ctx.strokeStyle = '#65bbff'; ctx.lineWidth = 2; ctx.strokeRect(60, 208, 1090, 238); }
  if (layers.includes('axis')) { ctx.strokeStyle = '#f2cf6f'; ctx.setLineDash([12, 8]); ctx.beginPath(); ctx.moveTo(0, 325); ctx.lineTo(1200, 343); ctx.stroke(); ctx.setLineDash([]); ctx.beginPath(); ctx.moveTo(600, 302); ctx.lineTo(600, 366); ctx.moveTo(568, 334); ctx.lineTo(632, 334); ctx.stroke(); }
  if (layers.includes('measure')) { ctx.strokeStyle = '#dca4ff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(845, 242); ctx.lineTo(845, 417); ctx.moveTo(831, 242); ctx.lineTo(859, 242); ctx.moveTo(831, 417); ctx.lineTo(859, 417); ctx.stroke(); }
  if (layers.includes('labels')) { ctx.font = '14px Segoe UI'; ctx.fillStyle = '#123b35'; ctx.fillRect(78, 176, 197, 28); ctx.fillStyle = '#82efcb'; ctx.fillText('Stamm · Demo-Konfidenz 98 %', 87, 195); ctx.fillStyle = '#271f32'; ctx.fillRect(868, 319, 104, 28); ctx.fillStyle = '#e3bdff'; ctx.fillText('384 mm · Demo', 876, 338); }
  ctx.restore();
  if (layers.includes('roi')) { ctx.globalAlpha = opacity; ctx.strokeStyle = '#97afc3'; ctx.setLineDash([7, 7]); ctx.strokeRect(65, 148, 1070, 366); ctx.setLineDash([]); ctx.globalAlpha = 1; }
}
export function Thumbnail({ index = 0 }: { index?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => { const ctx = ref.current?.getContext('2d'); if (ctx) { ctx.scale(120 / 1200, 68 / 675); drawScene(ctx, index * 800, ['contour']); } }, [index]);
  return <canvas ref={ref} width={120} height={68} className="ws-thumbnail" aria-label="Synthetisches Kamerabild"/>;
}
export function LiveWorkspace({ channel }: { channel: Channel }) {
  const { data, update, notify } = useDemo();
  const [layersOpen, setLayersOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [overlaysOn, setOverlaysOn] = useState(true);
  const opacity = Number(data.settings[`${channel.id}:overlay-opacity`] ?? 85);
  const setOpacity = (value: number) => update(d => ({ ...d, settings: { ...d.settings, [`${channel.id}:overlay-opacity`]: String(value) } }));
  const inspectorWidth = Number(data.settings['layout:live-inspector'] ?? 310);
  const [paused, setPaused] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [view, setView] = useState('Kamerabild');
  const canvas = useRef<HTMLCanvasElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const elapsed = useRef(0);
  const layers = data.overlays[channel.id] ?? defaultLayers;
  const moving = channel.stream === 'running' && !paused;
  const renderedLayers = useMemo(() => overlaysOn ? layers : [], [overlaysOn, layers]);
  const toggle = (id: string) => update(d => ({ ...d, overlays: { ...d.overlays, [channel.id]: layers.includes(id) ? layers.filter(l => l !== id) : [...layers, id] } }));
  const setLayers = (next: string[]) => update(d => ({ ...d, overlays: { ...d.overlays, [channel.id]: next } }));
  useEffect(() => {
    const node = canvas.current; if (!node) return;
    let animation = 0, previous = 0;
    const paint = (now: number) => {
      const rect = node.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.round(rect.width * dpr), h = Math.round(rect.height * dpr);
      if (node.width !== w || node.height !== h) { node.width = w; node.height = h; }
      const ctx = node.getContext('2d');
      if (ctx && w && h) {
        if (moving && previous) elapsed.current += now - previous;
        ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#10191e'; ctx.fillRect(0, 0, w, h);
        const scale = Math.min(w / 1200, h / 675) * zoom * (view === 'Modellansicht' ? 1.3 : 1);
        ctx.translate(w / 2 + pan.x * dpr, h / 2 + pan.y * dpr); ctx.scale(scale, scale); ctx.translate(-600, -337.5);
        drawScene(ctx, elapsed.current, renderedLayers, opacity / 100);
      }
      previous = now;
      if (moving) animation = requestAnimationFrame(paint);
    };
    const observer = new ResizeObserver(() => { if (!moving) paint(performance.now()); }); observer.observe(node);
    animation = requestAnimationFrame(paint);
    return () => { cancelAnimationFrame(animation); observer.disconnect(); };
  }, [moving, opacity, renderedLayers, zoom, pan.x, pan.y, view]);
  const command = (field: 'runtime' | 'stream', running: boolean) => update(d => ({ ...d, channels: d.channels.map(c => c.id === channel.id ? { ...c, [field]: running ? 'running' : 'stopped' } : c) }), `${field === 'runtime' ? 'Runtime' : 'Stream'} ${running ? 'gestartet' : 'gestoppt'} · simuliert`);
  return <div className={`ws-live-workspace ${inspectorOpen ? '' : 'ws-live-workspace--wide'}`} style={{ '--ws-live-inspector': `${inspectorWidth}px` } as CSSProperties}>
    <div className="ws-viewer" ref={surface}>
      <div className="ws-viewer-toolbar"><span className="ws-live-indicator"><i className={moving ? 'is-live' : ''}/>{moving ? 'LIVE · DEMO' : 'STANDBILD · DEMO'}</span><select aria-label="Bildraum" value={view} onChange={e => { setView(e.target.value); setPan({ x: 0, y: 0 }); }}>{['Kamerabild', 'Modellansicht'].map(v => <option key={v}>{v}</option>)}</select><span className="ws-spacer"/><ToolButton icon={paused ? 'play' : 'pause'} label={paused ? 'Bild fortsetzen' : 'Bild einfrieren'} onClick={() => setPaused(!paused)} active={paused}/><ToolButton icon="layers" label="Overlay-Ebenen" active={layersOpen} onClick={() => setLayersOpen(!layersOpen)}/><ToolButton icon={overlaysOn ? 'eye' : 'hidden'} label="Alle Overlays anzeigen" active={overlaysOn} onClick={() => setOverlaysOn(!overlaysOn)}/><ToolButton icon="fit" label="Bild einpassen" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}/><ToolButton icon="expand" label="Livebild Vollbild" onClick={() => { if (document.fullscreenElement) void document.exitFullscreen(); else void surface.current?.requestFullscreen().catch(() => notify('Vollbild ist in diesem Browser nicht verfügbar.')); }}/><ToolButton icon="panel" label="Messwerte einblenden" active={inspectorOpen} onClick={() => setInspectorOpen(!inspectorOpen)}/></div>
      <div className="ws-canvas-stage"><canvas ref={canvas} aria-label="Simulierter Livestream mit synchronen Overlay-Ebenen" onWheel={e => setZoom(z => Math.max(.5, Math.min(4, z + (e.deltaY < 0 ? .1 : -.1))))} onPointerDown={e => { drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; e.currentTarget.setPointerCapture(e.pointerId); }} onPointerMove={e => { if (drag.current) setPan({ x: drag.current.px + e.clientX - drag.current.x, y: drag.current.py + e.clientY - drag.current.y }); }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}/>
        {channel.stream !== 'running' && <div className="ws-frame-warning"><Icon name="alert"/>{channel.stream === 'offline' ? 'Quelle offline' : 'Stream gestoppt'} · Beispielbild, kein aktuelles Kamerabild</div>}
        <div className="ws-canvas-caption"><span>YB / {channel.name}</span><span>Synthetische Sequenz · 1200 × 675</span></div>
      </div>
      {layersOpen && <aside className="ws-layer-popover" aria-label="Overlay-Ebenen"><header><strong>Overlay-Ebenen</strong><ToolButton icon="close" label="Ebenen schließen" onClick={() => setLayersOpen(false)}/></header><div className="ws-layer-actions"><button onClick={() => { setLayers(layerDefinitions.map(l => l.id)); setOverlaysOn(true); }}>Alle</button><button onClick={() => setLayers([])}>Keine</button><button onClick={() => { setLayers(defaultLayers); setOverlaysOn(true); setOpacity(85); }}>Standard</button></div>{['Erkennung', 'Geometrie', 'Information'].map(group => <section key={group}><h4>{group}</h4>{layerDefinitions.filter(l => l.group === group).map(layer => <div key={layer.id} className="ws-layer-row"><button aria-pressed={layers.includes(layer.id)} onClick={() => toggle(layer.id)}><Icon name={layers.includes(layer.id) ? 'eye' : 'hidden'} size={16}/><i style={{ background: layer.color }}/>{layer.label}</button><button className="ws-layer-solo" title={`Nur ${layer.label} anzeigen`} onClick={() => { setLayers([layer.id]); setOverlaysOn(true); }}>Solo</button></div>)}</section>)}<label className="ws-opacity">Deckkraft <span>{opacity} %</span><input aria-label="Overlay-Deckkraft" type="range" min="0" max="100" value={opacity} onChange={e => setOpacity(Number(e.target.value))}/></label><small>{overlaysOn ? `${layers.length} Ebenen ausgewählt` : 'Overlays global ausgeblendet · Auswahl bleibt erhalten'}</small></aside>}
      <footer className="ws-viewer-footer"><span><i className="ws-dot"/> {moving ? '25,0' : '0,0'} FPS <span className="ws-separator">│</span> Latenz {moving ? '38 ms' : '—'}</span><div><button onClick={() => setZoom(z => Math.max(.5, z - .25))} aria-label="Verkleinern">−</button><span>{Math.round(zoom * 100)} %</span><button onClick={() => setZoom(z => Math.min(4, z + .25))} aria-label="Vergrößern">+</button></div><button onClick={() => { const link = document.createElement('a'); link.download = `${channel.id}-demo.png`; link.href = canvas.current?.toDataURL() ?? ''; link.click(); }}><Icon name="download" size={14}/>Bild speichern</button></footer>
    </div>
    {inspectorOpen && <aside className="ws-live-inspector ws-scroll"><ResizeHandle label="Breite der Live-Messwerte" width={inspectorWidth} onChange={width => update(d => ({ ...d, settings: { ...d.settings, "layout:live-inspector": String(width) } }))}/><div className="ws-inspector-title">Betrieb & Messwerte <span className="ws-muted">Demo</span></div><section className="ws-section"><div className="ws-section-heading"><h3>Inferenz-Runtime</h3><Badge status={channel.runtime}/></div><div className="ws-button-row"><button className={channel.runtime === 'running' ? '' : 'ws-primary'} disabled={channel.runtime === 'offline'} onClick={() => command('runtime', channel.runtime !== 'running')}><Icon name={channel.runtime === 'running' ? 'stop' : 'play'} size={14}/>{channel.runtime === 'running' ? 'Stoppen' : 'Starten'}</button><ToolButton icon="refresh" label="Runtime neu starten" onClick={() => command('runtime', true)}/></div><dl><dt>Pipeline</dt><dd>{channel.pipeline}</dd><dt>Version</dt><dd>v{data.graphs[channel.id]?.activeVersion ?? 1}</dd><dt>Ausführung</dt><dd>{channel.target}</dd></dl></section><section className="ws-section"><div className="ws-section-heading"><h3>Kamera-Stream</h3><Badge status={channel.stream}/></div><button onClick={() => command('stream', channel.stream !== 'running')}><Icon name={channel.stream === 'running' ? 'stop' : 'play'} size={14}/>{channel.stream === 'running' ? 'Stream stoppen' : 'Stream starten'}</button><p className="ws-help">Kamera und Inferenz sind unabhängig steuerbar.</p></section><section className="ws-section"><h3>Letztes Messergebnis</h3><div className="ws-measurement">384 <span>mm</span></div><p className="ws-help">Stammdurchmesser · simulierter Messwert</p><dl><dt>Objekt erkannt</dt><dd>Ja</dd><dt>Konfidenz</dt><dd>98,2 %</dd><dt>Korrekturwinkel</dt><dd>Keine gültige Referenz</dd><dt>Entscheidungsstatus</dt><dd><Badge status="Prüfen"/></dd></dl></section><details className="ws-section"><summary>Diagnose & Bildraum</summary><dl><dt>Quelle</dt><dd>{channel.source}</dd><dt>Bildverlust</dt><dd>0,0 % · Demo</dd><dt>Koordinaten</dt><dd>{view}</dd></dl><p className="ws-help">Alle Werte dieser Vorschau sind Beispieldaten. Die Simulation ist keine Messung oder Kalibrierung.</p></details></aside>}
  </div>;
}
