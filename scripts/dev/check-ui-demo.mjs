// Browser smoke checks using Chrome DevTools Protocol; no additional npm dependency.
// Start the local demo and an isolated Chrome with --remote-debugging-port=9227 first.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

const origin = process.env.UI_DEMO_URL ?? 'http://127.0.0.1:4173';
const debugOrigin = process.env.UI_DEMO_CDP_URL ?? 'http://127.0.0.1:9227';
for (const url of [origin, debugOrigin]) assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname), 'Checks may only target loopback hosts');
const targets = await (await fetch(`${debugOrigin}/json/list`)).json();
const target = targets.find(t => t.type === 'page');
assert(target, 'No isolated Chrome page is available');
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
let sequence = 0;
const pending = new Map(), errors = [], requests = [];
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const task = pending.get(message.id); if (!task) return;
    pending.delete(message.id); clearTimeout(task.timeout);
    if (message.error) task.reject(new Error(JSON.stringify(message.error))); else task.resolve(message.result);
  }
  if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
  if (message.method === 'Network.requestWillBeSent') requests.push(message.params.request.url);
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
  pending.set(id, { resolve, reject, timeout }); socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
};
const settle = () => new Promise(resolve => setTimeout(resolve, 150));
const click = async (text, selector = 'button') => { await evaluate(`(() => { const item = [...document.querySelectorAll(${JSON.stringify(selector)})].find(e => e.textContent.trim() === ${JSON.stringify(text)} || e.getAttribute('aria-label') === ${JSON.stringify(text)}); if (!item) throw new Error('Missing button: ' + ${JSON.stringify(text)}); item.click(); })()`); await settle(); };
const route = async path => { await evaluate(`location.hash = ${JSON.stringify(`#${path}`)}`); await settle(); };
const check = async (label, expression) => { assert(await evaluate(expression), label); console.log(`PASS ${label}`); };
const fill = async (selector, value) => { await evaluate(`(() => { const input = document.querySelector(${JSON.stringify(selector)}); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(value)}); input.dispatchEvent(new Event('input', {bubbles:true})); })()`); await settle(); };
const screenshot = async name => { const picture = await send('Page.captureScreenshot', { format: 'png' }); await writeFile(`.tmp/ui-demo-validation/${name}.png`, Buffer.from(picture.data, 'base64')); };

try {
  await mkdir('.tmp/ui-demo-validation', { recursive: true });
  await send('Runtime.enable'); await send('Network.enable'); await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: origin });
  for (let i = 0; i < 80; i++) { if (await evaluate('Boolean(document.querySelector(".ws-app"))')) break; await settle(); }
  await evaluate("localStorage.removeItem('cv-ybbs-workspace-demo-v1')");
  await send('Page.reload'); await new Promise(resolve => setTimeout(resolve, 900));
  await route('/channels');
  await check('Demo starts without a backend or login', 'document.querySelectorAll(".ws-channel-table tbody tr").length === 12');
  await check('Full HD shell has no document scroll', 'document.documentElement.scrollHeight === innerHeight && document.documentElement.scrollWidth === innerWidth');
  await click('Besäumlinie3', '[role=treeitem]');
  await check('Facility filters channel rows', 'document.querySelectorAll(".ws-channel-table tbody tr").length === 3');
  await screenshot('channels-filtered');
  await route('/channels/chn-demo-1');
  await check('Live canvas uses the available workspace', 'document.querySelector(".ws-canvas-stage").clientWidth > 1200 && document.querySelector(".ws-canvas-stage").clientHeight > 700');
  await click('Overlay-Ebenen');
  await click('Objektkontur');
  await check('Individual overlay switch changes local selection', '!JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).overlays["chn-demo-1"].includes("contour")');
  await click('Alle Overlays anzeigen'); await click('Alle Overlays anzeigen');
  await check('Global toggle preserves individual selection', '!JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).overlays["chn-demo-1"].includes("contour")');
  await click('Standard'); await click('Bild einfrieren');
  const frozen = await evaluate('document.querySelector(".ws-canvas-stage canvas").toDataURL()');
  await settle(); assert.equal(await evaluate('document.querySelector(".ws-canvas-stage canvas").toDataURL()'), frozen, 'Paused frame must not advance');
  console.log('PASS Frozen canvas remains stable'); await screenshot('live-overlays');
  await click('Pipeline', '.ws-tabs button');
  await check('Graph is primary and the inspector is visible', 'document.querySelectorAll(".ws-graph-node").length === 6 && !!document.querySelector(".ws-node-inspector") && !document.querySelector("dialog[open]")');
  await fill('.ws-node-inspector input', 'Segmentierung · Teständerung');
  await click('Betrieb', '.ws-tabs button'); await click('Pipeline', '.ws-tabs button');
  await check('Graph draft survives tab changes', 'document.querySelector(".ws-node-inspector input").value === "Segmentierung · Teständerung"');
  await screenshot('graph');
  await route('/pipelines');
  await check('Object navigation protects unsaved graph changes', 'document.querySelector("dialog[open]")?.textContent.includes("Ungespeicherte Änderungen")');
  await click('Speichern & wechseln');
  await check('Project workspace is a compact list', 'document.querySelectorAll(".ws-project-table tbody tr").length === 14');
  await screenshot('projects');
  await route('/channels/chn-demo-1?tab=Pipeline');
  await check('Saving does not activate a graph version', 'JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).graphs["chn-demo-1"].version === 5 && JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).graphs["chn-demo-1"].activeVersion === 4');
  await click('Aktivieren');
  await check('Explicit activation selects the saved version', 'JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).graphs["chn-demo-1"].activeVersion === 5');
  await route('/channels/chn-demo-1?tab=Konfiguration');
  await fill('.ws-config-form input', 'Stammausrichtung · Lokaler Test');
  await click('Quelle', '.ws-subnav button'); await click('Allgemein & Platz', '.ws-subnav button');
  await check('Channel form draft survives section changes', 'document.querySelector(".ws-config-form input").value === "Stammausrichtung · Lokaler Test"');
  await click('Speichern');
  await route('/system/structure');
  await check('Referenced places cannot be deleted', '[...document.querySelectorAll(".ws-structure-table button")].filter(b => b.textContent === "Löschen").every(b => b.disabled)');
  await click('Bereich'); await fill('dialog input', 'Neuer Testbereich'); await click('Speichern', 'dialog button');
  await check('Creating a structural area persists locally', 'JSON.parse(localStorage.getItem("cv-ybbs-workspace-demo-v1")).areas.some(a => a.name === "Neuer Testbereich")');
  await screenshot('structure');
  for (const path of ['/results', '/edge-devices', '/edge-devices/YB-EDGE-01?tab=Softwareupdates', '/pipelines/prj-1?tab=Modelle', '/pipelines/prj-1?tab=Automatisierung', '/system/health', '/system/training', '/system/deployments', '/system/snapshots', '/system/registry', '/system/templates', '/system/batch', '/system/access', '/system/integrations', '/system/workers', '/system/audit', '/runtime-pipelines']) {
    await route(path); await check(`Renders ${path}`, '!!document.querySelector(".ws-module-content").innerText.trim() && !document.querySelector("vite-error-overlay")');
  }
  await send('Emulation.setDeviceMetricsOverride', { width: 1366, height: 768, deviceScaleFactor: 1, mobile: false });
  await route('/channels/chn-demo-1');
  await check('Notebook viewport keeps canvas and shell accessible', 'document.documentElement.scrollWidth === innerWidth && document.documentElement.scrollHeight === innerHeight && document.querySelector(".ws-canvas-stage").clientHeight > 400');
  await screenshot('live-notebook');
  await click('Pipeline', '.ws-tabs button'); await click('Graph einpassen'); await screenshot('graph-notebook');
  await send('Page.reload'); await new Promise(resolve => setTimeout(resolve, 600));
  await check('Saved state survives a reload', 'document.body.innerText.includes("Stammausrichtung · Lokaler Test")');
  assert.equal(errors.length, 0, `Browser exceptions: ${JSON.stringify(errors)}`);
  assert.equal(requests.filter(url => /^https?:/.test(url) && new URL(url).origin !== new URL(origin).origin).length, 0, 'No nonlocal HTTP traffic');
  assert.equal(requests.filter(url => /\/app\/(api|router)\.|\/packages\/api-client\//.test(url)).length, 0, 'Production API and router must not load');
  console.log(`PASS Network isolation (${requests.length} local requests), no browser exceptions`);
  await evaluate("localStorage.removeItem('cv-ybbs-workspace-demo-v1')");
  await send('Page.navigate', { url: `${origin}/#/channels/chn-demo-1` });
  console.log('Browser smoke checks completed. Screenshots: .tmp/ui-demo-validation/');
} finally { socket.close(); }
