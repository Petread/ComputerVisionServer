// Render the actual production build with CDP request interception. No app server,
// backend, camera or real API is started. Every browser request is fulfilled here
// or rejected; fixtures cannot be included in the production dependency graph.
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fixtureResponse, useLiveFixture, resultRequests } from './workspace-layout-fixtures.mjs';
const debugOrigin = process.env.UI_WORKSPACE_CDP_URL ?? 'http://127.0.0.1:9228';
assert(['127.0.0.1','localhost'].includes(new URL(debugOrigin).hostname));
const origin = 'http://127.0.0.1:4178';
const dist = path.resolve('frontend/apps/admin-web/dist');
const output = '.tmp/ui-workspace-validation';
await mkdir(output,{recursive:true});
const targets=await(await fetch(`${debugOrigin}/json/list`)).json();
const socket=new WebSocket(targets.find(target=>target.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>socket.addEventListener('open',resolve,{once:true}));
let sequence=0;const pending=new Map(),errors=[],unhandled=[],checks=[];
const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`CDP timeout ${method}`));},15000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});
socket.addEventListener('message',async event=>{
 const message=JSON.parse(event.data);
 if(message.id){const task=pending.get(message.id);if(task){clearTimeout(task.timer);pending.delete(message.id);message.error?task.reject(new Error(JSON.stringify(message.error))):task.resolve(message.result);}return;}
 if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
 if(message.method==='Runtime.consoleAPICalled' && message.params.type==='error') errors.push(message.params.args.map(arg=>arg.description??arg.value));
 if(message.method!=='Fetch.requestPaused')return;
 const {requestId,request}=message.params;const url=new URL(request.url);
 try {
  if(url.origin!==origin){await send('Fetch.failRequest',{requestId,errorReason:'BlockedByClient'});return;}
  let body,type='application/json';
  if(url.pathname.startsWith('/frontend/assets/')){
    const file=path.resolve(dist,url.pathname.slice('/frontend/'.length));assert(file.startsWith(dist+path.sep));body=await readFile(file);type=file.endsWith('.css')?'text/css':'application/javascript';
  }else if(url.pathname.startsWith('/frontend')){body=await readFile(path.join(dist,'index.html'));type='text/html';}
  else {const data=fixtureResponse(url.pathname,request.method,request.postData,url.searchParams);if(data===undefined){unhandled.push(`${request.method} ${url.pathname}`);await send('Fetch.fulfillRequest',{requestId,responseCode:404,body:Buffer.from('{}').toString('base64')});return;}body=Buffer.from(JSON.stringify(data));}
  await send('Fetch.fulfillRequest',{requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:type}],body:body.toString('base64')});
 }catch(error){errors.push(String(error));await send('Fetch.failRequest',{requestId,errorReason:'Failed'});}
});
const evaluate=async expression=>{const data=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(data.exceptionDetails)throw new Error(JSON.stringify(data.exceptionDetails));return data.result.value;};
const settle=()=>new Promise(resolve=>setTimeout(resolve,180));
const click=async(label,selector='button')=>{await evaluate(`(()=>{const element=[...document.querySelectorAll(${JSON.stringify(selector)})].find(e=>e.getClientRects().length && (e.textContent.trim()===${JSON.stringify(label)}||e.getAttribute('aria-label')===${JSON.stringify(label)}));if(!element)throw Error('Missing ${label}');element.click()})()`);await settle();};
const check=async(label,expression)=>{assert(await evaluate(expression),label);checks.push(label);console.log(`PASS ${label}`);};
const capture=async name=>{const data=await send('Page.captureScreenshot',{format:'png'});await writeFile(`${output}/${name}.png`,Buffer.from(data.data,'base64'));};
const fill=async(selector,value)=>{await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);await settle();};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Fetch.enable',{patterns:[{urlPattern:'*'}]});
 await send('Emulation.setDeviceMetricsOverride',{width:1670,height:1010,deviceScaleFactor:1,mobile:false});
 await send('Page.navigate',{url:`${origin}/frontend/channels`});
 for(let i=0;i<70;i++){if(await evaluate('document.querySelectorAll(".workspace-channel-table tbody tr").length===15'))break;await settle();}
 await check('Production channel list renders 15 fixture channels','document.querySelectorAll(".workspace-channel-table tbody tr").length===15');
 await check('Demo icon rail and topbar dimensions','document.querySelector(".ws-sidebar").clientWidth<=60 && document.querySelector(".ws-topbar").clientHeight===47');
 await check('Table spans the available width','document.querySelector(".workspace-channel-table").getBoundingClientRect().right>=innerWidth-16');
 await check('No document scrolling','document.documentElement.scrollHeight===innerHeight && document.documentElement.scrollWidth===innerWidth');
 await capture('channels');await click('Linie 15','[role=treeitem]');
 await check('Structure filters channel rows','document.querySelectorAll(".workspace-channel-table tbody tr").length===5');
 await click('Stammdrehung · Vorzentrierung','a.ws-object-link');
 for(let i=0;i<40;i++){if(await evaluate('!!document.querySelector(".workspace-canvas-clip")'))break;await settle();}
 await check('Live canvas fills the workspace','document.querySelector(".workspace-canvas-clip").clientWidth>1200 && document.querySelector(".workspace-canvas-clip").clientHeight>700');
 await check('Canvas controls sit bottom left','(()=>{const tools=document.querySelector(".workspace-canvas-tools").getBoundingClientRect(),viewport=document.querySelector(".workspace-live-viewport").getBoundingClientRect();return Math.abs(tools.left-viewport.left-12)<2 && Math.abs(viewport.bottom-tools.bottom-12)<2;})()');
 await check('Inspector buttons match the standard radius','[...document.querySelectorAll(".channel-runtime-controls button")].every(e=>getComputedStyle(e).borderRadius==="4px")');
 await check('Capture is a closed inspector section without its own tab','!!document.querySelector(".channel-live-workspace__tools .workspace-capture-section:not([open])") && ![...document.querySelectorAll(".workspace-channel-tabs button")].some(e=>e.textContent==="Aufnahmen")');
 await evaluate('document.querySelector(".workspace-capture-section").open=true');await settle();
 await check('Capture retains project-specific Label Studio link','document.querySelector(".workspace-capture-section a").getAttribute("href")==="/label-studio/projects/21/data/"');
 await evaluate('document.querySelector(".workspace-capture-section").scrollIntoView({block:"start"})');await settle();await capture('capture');
 await check('Expanded capture fits the inspector','(()=>{const e=document.querySelector(".channel-live-workspace__tools");return e.scrollWidth===e.clientWidth;})()');
 await evaluate('document.querySelector(".workspace-capture-section").open=false;document.querySelector(".channel-live-workspace__tools").scrollTop=0');
 await click('Gespeicherte Ergebnisse ansehen');
 for(let i=0;i<40;i++){if(await evaluate('!!document.querySelector(".channel-result-content")'))break;await settle();}
 await check('Channel history uses timeline and structured values','document.querySelectorAll(".channel-result-entry").length===30 && document.querySelector(".channel-result-values").textContent.includes("1234") && !document.querySelector(".result-history-page")');
 await check('Disabled storage links to channel configuration','document.querySelector(".channel-results-notice").textContent.includes("deaktiviert")');
 await capture('channel-results');
 await evaluate('document.querySelectorAll(".channel-result-entry")[1].click()');await settle();
 await check('Selecting a failed execution pins it and explains incomplete outputs','document.querySelector(".channel-result-content").textContent.includes("unvollständig") && document.querySelector(".channel-results-workspace > footer").textContent.includes("fixiert")');
 await click('Ältere Ergebnisse');
 await check('Channel history pages through stored executions','document.querySelectorAll(".channel-result-entry").length===5 && document.querySelector(".channel-result-timeline footer").textContent.includes("31–35 von 35")');
 await click('Neueste');await settle();
 await fill('.channel-results-workspace .ws-search input','1234');await new Promise(resolve=>setTimeout(resolve,400));await settle();
 await check('Channel result search is applied on the API','document.querySelectorAll(".channel-result-entry").length===1');
 assert(resultRequests.length>0 && resultRequests.every(params=>params.channel_id==='chn-layout'),'Every history request is scoped to the current channel');checks.push('Every history request is scoped to the current channel');
 await click('Speicherung konfigurieren');await check('Storage shortcut opens the correct settings section','document.querySelector(".ws-config-form h2").textContent==="Datenerfassung & Historie"');await click('Betrieb','.ws-tabs button');
 await capture('live');await click('Inspektor ein-/ausblenden');
 await check('Inspector toggle releases canvas width','document.querySelector(".workspace-canvas-clip").clientWidth>1500');await click('Inspektor ein-/ausblenden');
 await click('Pipeline','.ws-tabs button');for(let i=0;i<40;i++){if(await evaluate('document.querySelectorAll(".runtime-canvas-node").length===4'))break;await settle();}
 await check('Graph is primary, full height and dark','document.querySelectorAll(".runtime-canvas-node").length===4 && document.querySelector(".workspace-graph-center").clientHeight>700 && getComputedStyle(document.querySelector(".workspace-graph-center")).backgroundColor==="rgb(25, 37, 46)" && !document.querySelector("dialog[open]")');
 await capture('pipeline');await click('Bildgröße','.runtime-canvas-node');
 await click('Version & Vorlage');await check('Version dialog uses a flat form without metric cards','!!document.querySelector("dialog[open] .channel-pipeline-settings__facts") && !document.querySelector("dialog[open] .metric-pill")');await capture('pipeline-version');
 await click('Vorlage','dialog .ws-tabs button');await check('Template selection is a separate dialog section','!!document.querySelector(".channel-pipeline-settings__pane:not([hidden]) select")');await capture('pipeline-template');await click('Dialog schließen');
 await check('Node parameters use the inspector','!!document.querySelector(".workspace-node-inspector input")');await capture('pipeline-properties');
 await fill('.workspace-node-inspector input','640');await click('Betrieb','.ws-tabs button');await click('Pipeline','.ws-tabs button');
 await check('Graph draft survives tab switching','document.querySelector(".workspace-node-inspector input").value==="640"');
 await click('Bild & Kalibrierung öffnen');await check('Calibration opens a native modal','!!document.querySelector("dialog[open] .runtime-node-properties-modal")');await click('Dialog schließen');
 await check('Closing calibration retains node selection','document.querySelector(".workspace-node-inspector input").value==="640"');
 await click('Konfiguration','.ws-tabs button');
 await check('Channel settings have section navigation','document.querySelectorAll(".workspace-config-tab .ws-subnav button").length===5 && document.querySelectorAll(".workspace-config-fields:not([hidden])").length===1');
 await capture('configuration');await click('Quelle','.workspace-config-tab .ws-subnav button');
 await check('Source settings retain real Basler controls','[...document.querySelectorAll(".workspace-config-fields:not([hidden]) select")].some(e=>[...e.options].some(o=>o.text==="Continuous"))');await capture('configuration-source');
 await click('Allgemein & Platz','.workspace-config-tab .ws-subnav button');await fill('.workspace-config-fields:not([hidden]) input','Geänderter Kanal');await click('Betrieb','.ws-tabs button');await click('Konfiguration','.ws-tabs button');
 await check('Channel draft survives tab switching','document.querySelector(".workspace-config-fields:not([hidden]) input").value==="Geänderter Kanal"');await click('Verwerfen');
 await check('Discard restores saved channel fields','document.querySelector(".workspace-config-fields:not([hidden]) input").value==="Stammdrehung · Vorzentrierung"');
 // The remaining graph draft is discarded only inside this isolated test browser.
 await evaluate('window.confirm=()=>true');
 await click('Projekte & Modelle','a.ws-nav-link');await settle();
 await check('Project links open the specific Label Studio project','[...document.querySelectorAll("a")].filter(a=>a.getAttribute("href")==="/label-studio/projects/21/data/").length===1');await capture('projects');
 await send('Page.navigate',{url:`${origin}/frontend/pipelines/project-0`});
 for(let i=0;i<40;i++){if(await evaluate('!!document.querySelector(".project-readiness")'))break;await settle();}
 await check('Readiness uses flat facts and all five stages','document.querySelectorAll(".project-readiness__facts > div").length===6 && document.querySelectorAll(".project-readiness .dataset-flow__step").length===5 && !document.querySelector(".project-readiness .metric-pill")');
 for(const [width,height] of [[1670,1010],[1366,768],[1024,768]]) {
   await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await settle();
   await click('Dataset','.project-workspace > .ws-tabs button');
   await check(`Dataset long labels fit ${width}`,'(()=>{const section=document.querySelector("#dataset"),tab=section.parentElement;return section.scrollWidth<=section.clientWidth && tab.scrollWidth<=tab.clientWidth && [...section.querySelectorAll(".project-readiness small,.class-distribution-list span")].every(e=>e.scrollWidth<=e.clientWidth+1 && e.scrollHeight<=e.clientHeight+1);})()');await capture(`project-dataset-${width}`);
   await click('Modelle','.project-workspace > .ws-tabs button');
   await check(`Model names and metadata fit ${width}`,'(()=>{const section=document.querySelector("#models"),master=section.querySelector(".model-master");return section.scrollWidth<=section.clientWidth && master.scrollWidth<=master.clientWidth && [...master.querySelectorAll(".model-master-card,dd,strong,small")].every(e=>e.scrollWidth<=e.clientWidth+1 && e.scrollHeight<=e.clientHeight+1);})()');await capture(`project-models-${width}`);
 }
 await evaluate('document.querySelectorAll(".model-master-card")[1].click()');await settle();
 await check('Model selection preserves the full name','document.querySelectorAll(".model-master-card")[1].getAttribute("aria-pressed")==="true" && document.querySelector(".model-detail__header h3").textContent.endsWith("_2")');
 await send('Emulation.setDeviceMetricsOverride',{width:1670,height:1010,deviceScaleFactor:1,mobile:false});
 const testImage=await evaluate(`(()=>{const c=document.createElement('canvas');c.width=1280;c.height=720;const x=c.getContext('2d');x.fillStyle='#263841';x.fillRect(0,0,1280,720);x.strokeStyle='#526e7a';x.lineWidth=2;x.strokeRect(2,2,1276,716);x.fillStyle='#9eafb7';x.font='24px Segoe UI';x.fillText('LAYOUT-TESTBILD · KEINE KAMERA',40,50);return c.toDataURL('image/png').split(',')[1];})()`);
 useLiveFixture(testImage);
 await send('Page.navigate',{url:`${origin}/frontend/channels/chn-layout`});for(let i=0;i<40;i++){if(await evaluate('document.querySelectorAll(".workspace-live-media img").length>0'))break;await settle();}
 await check('Real image component preserves landscape aspect ratio','(()=>{const e=document.querySelector(".live-view-media");return Math.abs(e.clientWidth/e.clientHeight-1280/720)<.01;})()');
 await evaluate('document.querySelector(".workspace-overlay-menu").open=true');await settle();
 await check('Overlay layer controls are available','document.querySelectorAll(".workspace-overlay-row").length===2');
 await check('Overlay vectors are drawn on the real canvas','(()=>{const c=document.querySelector(".live-view-overlay__canvas");return c.getContext("2d").getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0);})()');await click('Keine');
 await check('Hidden overlays clear the real canvas','(()=>{const c=document.querySelector(".live-view-overlay__canvas");return c.getContext("2d").getImageData(0,0,c.width,c.height).data.every((v,i)=>i%4!==3||v===0);})()');
 await check('Overlay visibility persists for the channel','Object.values(JSON.parse(localStorage.getItem("runtime-live-overlay-layers:chn-layout"))).every(value=>value===false)');await click('Alle');await capture('live-overlays');
 await evaluate('document.querySelector(".workspace-overlay-menu").open=false');
 for(const [width,height] of [[1366,768],[1920,1080]]) {
   await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await settle();
   await check(`Live layout fits ${width}×${height}`,'document.documentElement.scrollHeight===innerHeight && document.documentElement.scrollWidth===innerWidth && document.querySelector(".workspace-canvas-clip").clientHeight>innerHeight*.6');await capture(`live-${width}`);
   await click('Pipeline','.ws-tabs button');await settle();await check(`Graph layout fits ${width}×${height}`,'document.querySelector(".workspace-graph-center").clientWidth>600 && document.querySelector(".workspace-graph-center").clientHeight>innerHeight*.6 && document.documentElement.scrollHeight===innerHeight');await capture(`pipeline-${width}`);await click('Betrieb','.ws-tabs button');
   await click('Ergebnisverlauf','.workspace-channel-tabs button');await settle();await check(`Channel results fit ${width}×${height}`,'document.documentElement.scrollHeight===innerHeight && document.documentElement.scrollWidth===innerWidth && document.querySelector(".channel-results-body").clientHeight>innerHeight-330 && document.querySelector(".channel-result-timeline > .ws-scroll").scrollHeight>document.querySelector(".channel-result-timeline > .ws-scroll").clientHeight');await capture(`results-${width}`);await click('Betrieb','.workspace-channel-tabs button');
 }
 assert.deepEqual(errors,[],'Browser runtime exceptions');assert.deepEqual([...new Set(unhandled)],[],'Unmocked API requests');
 await writeFile(`${output}/report.json`,JSON.stringify({checks,errors,unhandled},null,2));
}finally{await send('Fetch.disable');socket.close();await writeFile(`${output}/diagnostics.json`,JSON.stringify({checks,errors,unhandled},null,2));}

