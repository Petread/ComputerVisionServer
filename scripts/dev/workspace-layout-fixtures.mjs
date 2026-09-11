// Contract-shaped fixtures for the compiled UI layout test. Never imported by the app.
const base = {slug:'layout-camera',mode:'realtime',source_summary:'192.0.2.20',assigned_pipeline:'Stammdrehung · Pipeline',output_summary:{summary:'Overlay',connector_count:0,primary_outputs:['overlay']},capture_summary:{summary:'Aufnahme',rule_count:0,target_project_count:0,modes:[]},health_state:'ok',performance:{fps:0,frame_count:0,drop_count:0,trigger_count:0,reconnect_count:0,source_error_count:0},authoring_label_project_ids:[],result_history_policy:{enabled:false,retention_hours:24,sample_interval_seconds:5,max_records:10000},stem_rotation_priority_control:{schema_version:'runtime.stem-rotation-priority-control.v1',priority_source:'FRONTEND',priority_mode:'NONE',optimization_enable:false,revision:0},placement:{target:'server'}};
export const channel = {...base, channel_id:'chn-layout', name:'Stammdrehung · Vorzentrierung', technical_place:'Säge / Linie 1', facility_id:'facility-1', source_type:'basler_pylon', source_config:{address:'192.0.2.20'}, source_secrets:{}, status:'stopped', observed_state:'stopped', desired_state:'stopped', health:{state:'unknown',status_text:'Gestoppt'}, triggerable_pipelines:[], pipeline_version_id:'version-1', description:'Kamera vor dem ersten Walzenpaar', request_endpoints:[]};
export const channels = Array.from({length:15},(_,index)=>({...channel, channel_id:index ? `chn-layout-${index}` : channel.channel_id, name:index ? ['Basler Sprühnebel','Brettflächenkontrolle','Stammkrümmungserkennung'][index%3]+` · Kamera ${index+1}` : channel.name, facility_id:index%3 ? 'facility-2' : 'facility-1'}));
const port = {key:'frame',data_type:'frame',required:true};
export const nodeTypes = [['source.channel_frame','source','Kamerabild'],['preprocessing.resize','preprocessing','Bildgröße'],['inference.segmentation','inference','Segmentierung'],['output.result','output','Ergebnisausgabe']].map(([node_type,node_category,display_name])=>({node_type,node_category,display_name,contract_version:1,description:'Layout-Testbaustein',supported_modes:['realtime'],input_ports:node_category==='source'?[]:[port],output_ports:node_category==='output'?[]:[port],config_schema_ref:'',property_schema:node_category==='preprocessing'?[{key:'width',label:'Breite',field_type:'number',required:false,section:'Parameter'}]:[],default_config:{},runtime_handler_ref:'',compatibility_constraints:{}}));
export const graph = {schema_version:'runtime.pipeline-graph.v1',nodes:nodeTypes.map((type,index)=>({node_key:`node-${index}`,node_type:type.node_type,node_category:type.node_category,node_contract_version:1,input_ports:type.input_ports,output_ports:type.output_ports,config:{},logical_model_ref:null,position:{x:80+index*245,y:index%2?240:100}})),edges:nodeTypes.slice(1).map((_,index)=>({from_node_key:`node-${index}`,to_node_key:`node-${index+1}`,from_port:'frame',to_port:'frame'}))};
const report = {status:'valid',issues:[],error_count:0,warning_count:0};
const version = {pipeline_version_id:'version-1',pipeline_definition_id:'definition-1',version_no:1,status:'published',mode:'realtime',config_digest:'fixture',validation_status:'valid',error_count:0,warning_count:0,graph,validation_report:report,model_capability_constraints:{},node_contract_versions:{}};
const definition = {pipeline_definition_id:'definition-1',slug:'layout',name:'Stammdrehung · Pipeline',status:'published',latest_version_id:'version-1',published_version_id:'version-1',latest_version_mode:'realtime',published_version_mode:'realtime',template:false,scope:'channel_instance',owner_channel_id:channel.channel_id,version_count:1,versions:[version],latest_version:version};
const projects = Array.from({length:11},(_,index)=>({label_project_id:`project-${index}`,label_studio_project_id:index+21,title:['Sprühnebelsegmentierung – Fehlererkennung','Stammsegmentierung','Jahrringerkennung Stirn + Schräg'][index%3],task_type:'segmentation',status:'ready',next_recommended_action:'Modell trainieren',readiness:{readiness_percent:100,eligible_task_count:317,readiness_reason:'Bereit'},models:[],current_endpoints:[],training_pipeline_slug:'inspection'}));
const liveState = {channel_id:channel.channel_id,actual_state:'stopped',available:false,stale:true,frame:null,source_frame:null,overlay:[],layers_available:[],source_health:{state:'unknown',connected:false,status_text:'Gestoppt',reconnect_count:0,source_error_count:0},metrics:{frame_count:0,acquired_frame_count:0,dropped_frame_count:0,reconnect_count:0,source_error_count:0,buffer_depth:0}};
export function useLiveFixture(image, width=1280, height=720) {
  Object.assign(channel,{status:'running',observed_state:'running',desired_state:'running',health:{state:'ok',status_text:'Läuft'}});
  const frame={image_base64:image,mime_type:'image/png',width,height,sequence:1,captured_at:new Date().toISOString(),metadata:{}};
  const overlay=[{primitive_type:'box',layer:'detections',label:'Testkontur',geometry:{x_min:.2,y_min:.2,x_max:.8,y_max:.8},style:{stroke:'#65caac'},metadata:{}},{primitive_type:'line',layer:'reference',geometry:{start:{x:width*.1,y:height*.5},end:{x:width*.9,y:height*.5},coordinate_system:'pixel_xy'},style:{stroke:'#a1bad4'},metadata:{}}];
  Object.assign(liveState,{actual_state:'running',available:true,stale:false,frame,source_frame:frame,overlay,source_overlay:overlay,raw_overlay:overlay,layers_available:['detections','reference'],source_health:{...liveState.source_health,state:'ok',connected:true,status_text:'Layout-Testbild'},updated_at:new Date().toISOString()});
}
export function fixtureResponse(path, method, body) {
  if(path==='/api/v1/auth/me') return {authenticated:true,user:{username:'operator',display_name:'Layout Test',role:'platform_admin'}};
  if(path.endsWith('/runtime/places')) return {areas:[{id:'area-1',name:'Säge'},{id:'area-2',name:'Weiterverarbeitung'}],facilities:[{id:'facility-1',area_id:'area-1',name:'Linie 1'},{id:'facility-2',area_id:'area-2',name:'Coatinglinie'}]};
  if(path.endsWith('/runtime/channels')) return {items:channels};
  if(path.endsWith('/runtime/channels/chn-layout')) {
    if(method==='PATCH' || method==='PUT') Object.assign(channel,JSON.parse(body));
    return channel;
  }
  if(path.endsWith('/streams')) return {items:[],actual_state:'stopped',desired_state:'stopped'};
  if(path.endsWith('/authoring-context')) return {context:null};
  if(path.endsWith('/live-view/latest') || path.endsWith('/pipeline-preview')) return {state:liveState};
  if(path.endsWith('/manual-capture/session')) return {session:{channel_id:channel.channel_id,state:'idle',capture_count:0,failure_count:0,last_result:null}};
  if(path.endsWith('/runtime/pipeline-definitions')) return {items:[definition]};
  if(path.endsWith('/runtime/pipeline-definitions/definition-1')) return definition;
  if(path.endsWith('/runtime/node-types')) return {items:nodeTypes};
  if(path.endsWith('/api/v1/project-pipelines:refresh')) return {job_id:'layout-refresh',status:'queued'};
  if(path.endsWith('/api/v1/project-pipelines')) return {items:projects};
  if(['/manual-capture/targets','/manual-capture/history','/runtime/trigger-endpoints','/runtime/model-capabilities','/label-projects','/runtime/edge-devices'].some(suffix=>path.endsWith(suffix))) return {items:[]};
  return undefined;
}
