import { useRef, useState, type CSSProperties, type PropsWithChildren } from 'react';
import { Icon, ToolButton } from './workspace-ui';

export function OverlayControls({layers, visibility, onChange, label, opacity, onOpacity}: {layers: string[]; visibility: Record<string, boolean>; onChange: (value: Record<string, boolean>) => void; label: (key: string) => string; opacity: number; onOpacity: (value: number) => void}) {
  const ref = useRef<HTMLDetailsElement>(null);
  return <details ref={ref} className="workspace-overlay-menu" onKeyDown={e => {if(e.key === 'Escape' && ref.current) {ref.current.open = false; ref.current.querySelector('summary')?.focus();}}}>
    <summary><Icon name="layers" size={16}/> Overlays <small>{layers.filter(l => visibility[l] ?? true).length}/{layers.length}</small></summary>
    <div className="workspace-overlay-popover"><div className="actions-row"><button onClick={() => onChange(Object.fromEntries(layers.map(l => [l, true])))}>Alle</button><button onClick={() => onChange(Object.fromEntries(layers.map(l => [l, false])))}>Keine</button></div>
      {layers.map(l => <div className="workspace-overlay-row" key={l}><button aria-pressed={visibility[l] ?? true} onClick={() => onChange({...visibility, [l]: !(visibility[l] ?? true)})}><Icon name={(visibility[l] ?? true) ? 'eye' : 'eyeOff'} size={16}/>{label(l)}</button><button title={`Nur ${label(l)} anzeigen`} onClick={() => onChange(Object.fromEntries(layers.map(k => [k, k === l])))}>Solo</button></div>)}
      {!layers.length && <p>Keine Overlays im aktuellen Ergebnis.</p>}
      <label>Deckkraft {Math.round(opacity * 100)}%<input aria-label="Overlay-Deckkraft" type="range" min="0" max="1" step="0.05" value={opacity} onChange={e => onOpacity(Number(e.target.value))}/></label>
    </div>
  </details>;
}

export function LiveViewport({children, opacity}: PropsWithChildren<{opacity: number}>) {
  const ref = useRef<HTMLDivElement>(null); const media = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1); const [pan, setPan] = useState({x: 0, y: 0});
  const drag = useRef<{x: number; y: number; px: number; py: number} | null>(null);
  const [frozen, setFrozen] = useState<string | null>(null); const [error, setError] = useState('');
  function freeze() {
    if(frozen) {setFrozen(null); return;}
    const source = media.current; if(!source) return;
    const rect = source.getBoundingClientRect();
    const canvas = document.createElement('canvas'); canvas.width = Math.round(rect.width); canvas.height = Math.round(rect.height);
    const ctx = canvas.getContext('2d'); if(!ctx) return;
    let drawn = false;
    try {
      source.querySelectorAll('video, img, canvas').forEach(node => {
        const bounds = node.getBoundingClientRect(); if(!bounds.width || !bounds.height) return;
        if(node instanceof HTMLVideoElement && node.readyState < 2) return;
        if(node instanceof HTMLImageElement && !node.complete) return;
        ctx.globalAlpha = (node.closest('.live-view-overlay') ? opacity : 1) * Number(getComputedStyle(node).opacity || 1);
        ctx.drawImage(node as CanvasImageSource, bounds.left - rect.left, bounds.top - rect.top, bounds.width, bounds.height); drawn = true;
      });
      if(drawn) {setFrozen(canvas.toDataURL('image/png')); setError('');} else setError('Noch kein Bild zum Einfrieren verfügbar.');
    } catch {setError('Dieses Streamformat erlaubt kein Standbild im Browser.');}
  }
  return <div ref={ref} className="workspace-live-viewport" style={{'--overlay-opacity': opacity} as CSSProperties}>
    <div className="workspace-canvas-tools"><ToolButton icon="minus" label="Verkleinern" disabled={scale <= 1} onClick={() => {setScale(s => Math.max(1, s - .25)); setPan({x: 0, y: 0});}}/><button onClick={() => {setScale(1); setPan({x: 0, y: 0});}}>Einpassen · {Math.round(scale * 100)}%</button><ToolButton icon="plus" label="Vergrößern" disabled={scale >= 4} onClick={() => setScale(s => Math.min(4, s + .25))}/><ToolButton icon={frozen ? 'play' : 'pause'} label={frozen ? 'Live fortsetzen' : 'Ansicht einfrieren'} active={Boolean(frozen)} onClick={freeze}/><ToolButton icon="fullscreen" label="Vollbild" onClick={() => {const operation = document.fullscreenElement ? document.exitFullscreen() : (ref.current?.closest('.channel-live-block') ?? ref.current)?.requestFullscreen(); void operation?.catch(() => setError('Vollbild ist in diesem Browser nicht verfügbar.'));}}/>{frozen && <strong>Standbild</strong>}</div>
    {error && <p role="status">{error}</p>}
    <div className="workspace-canvas-clip" onPointerDown={e => {if(scale <= 1 || (e.target as HTMLElement).closest('button, a, input')) return; drag.current = {x: e.clientX, y: e.clientY, px: pan.x, py: pan.y}; e.currentTarget.setPointerCapture(e.pointerId);}} onPointerMove={e => {if(drag.current) {const bounds=e.currentTarget.getBoundingClientRect(); const limitX=bounds.width*(scale-1)/2; const limitY=bounds.height*(scale-1)/2; setPan({x: Math.max(-limitX, Math.min(limitX, drag.current.px+e.clientX-drag.current.x)), y: Math.max(-limitY, Math.min(limitY, drag.current.py+e.clientY-drag.current.y))});}}} onPointerUp={() => {drag.current=null;}} onPointerCancel={() => {drag.current=null;}}>
      <div className="workspace-canvas-transform" style={{transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`}}>
        <div ref={media} className="workspace-live-media" style={{visibility: frozen ? 'hidden' : undefined}}>{children}</div>
        {frozen && <img className="workspace-frozen-image" src={frozen} alt="Eingefrorenes Kamerabild mit Overlays"/>}
      </div>
    </div>
  </div>;
}
