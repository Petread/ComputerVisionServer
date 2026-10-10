import { FacilitySelect, PlaceTree, matchesPlace, usePlaces } from './places';
import { Icon, Modal, Search, ToolButton } from '../../components/workspace-ui';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState } from '@cv-platform/ui-kit';
import type {
  RuntimeChannelCreateRequest,
  RuntimeChannelSummary,
  RuntimePipelineDefinitionSummary,
  RuntimeSourceType
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import {
  type ChannelFilters,
  channelRuntimeActualState,
  channelStatusLabel,
  channelStatusTone,
  filterChannels,
  runtimeActionAvailability,
  sourceTypeLabel
} from './presentation';
import { useEdgeViewerLease } from './use-edge-viewer-lease';
import { BaslerCameraIpField, baslerCameraAddressConfig, validBaslerCameraIp } from './basler-camera-address';

const sourceTypes: RuntimeSourceType[] = [
  'rtsp',
  'usb_opencv',
  'aravis_gige',
  'flir_blackfly',
  'basler_pylon',
  'snapshot',
  'trigger_capture',
  'file_test'
];

const baslerAcquisitionModes = ['Continuous', 'SingleFrame', 'MultiFrame'];
const baslerGrabStrategies = ['LatestImageOnly', 'OneByOne', 'LatestImages'];
const baslerPixelFormats = ['BayerRG8', 'BayerBG8', 'BayerGB8', 'BayerGR8', 'Mono8', 'RGB8', 'BGR8', 'YUV422Packed'];
const baslerTriggerModes = ['Off', 'On'];
const baslerTriggerSources = ['Line1', 'Line2', 'Software'];
const baslerTriggerActivations = ['RisingEdge', 'FallingEdge', 'AnyEdge'];
const baslerTransportModes = [
  { value: 'auto', label: 'Auto' },
  { value: 'pylon_direct', label: 'Pylon direct' },
  { value: 'aravis_rtsp_bridge', label: 'Aravis RTSP bridge' }
];

const initialForm = {
  name: '',
  technicalPlace: '',
  facilityId: '',
  description: '',
  sourceType: 'rtsp' as RuntimeSourceType,
  sourceAddress: '',
  testFileName: '',
  testFileSourceConfig: {} as Record<string, unknown>,
  rtspUsername: '',
  rtspPassword: '',
  baslerAcquisitionMode: 'Continuous',
  baslerGrabStrategy: 'LatestImageOnly',
  baslerPixelFormat: 'BayerRG8',
  baslerCameraFps: '30',
  baslerLinkSpeedMbps: '1000',
  baslerExposureTimeUs: '',
  baslerGainDb: '',
  baslerTriggerMode: 'Off',
  baslerTriggerSource: 'Line1',
  baslerTriggerActivation: 'RisingEdge',
  baslerPacketSize: '',
  baslerInterPacketDelay: '',
  baslerBufferCount: '16',
  baslerOutputQueueSize: '',
  baslerFrameRetentionMs: '',
  baslerGrabFailureReconnectThreshold: '10',
  baslerWidth: '',
  baslerHeight: '',
  baslerTransportMode: 'auto',
  baslerCameraIp: '',
  maxFps: '',
  sampleEveryN: '1',
  pipelineMaxInflight: '2',
  pipelineVersionId: '',
  executionTarget: 'server' as 'server' | 'edge_device',
  edgeDeviceId: '',
  captureSummary: '',
  historyEnabled: false,
  historyRetentionHours: '24',
  historySampleIntervalSeconds: '5',
  historyMaxRecords: '10000'
};

export function ChannelListPage() {
  const [place, setPlace] = useState('');
  const [treeOpen, setTreeOpen] = useState(true);
  const [monitor, setMonitor] = useState(false);
  const places = usePlaces();
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [filters, setFilters] = useState<ChannelFilters>({
    search: '',
    sourceType: 'all' as RuntimeSourceType | 'all',
    status: 'all',
    technicalPlace: ''
  });
  const [form, setForm] = useState(initialForm);
  const [createOpen, setCreateOpen] = useState(false);

  const channelsQuery = useQuery({
    queryKey: ['runtime-channels'],
    queryFn: () => api.channels.list(),
    refetchInterval: 8000
  });
  const runtimePipelinesQuery = useQuery({
    queryKey: ['runtime-pipeline-definitions', 'channels'],
    queryFn: () => api.runtimePipelines.list({ includeTemplates: true, scope: 'template' }),
    staleTime: 30000
  });
  const edgeDevicesQuery = useQuery({
    queryKey: ['edge-devices', 'channel-placement'],
    queryFn: () => api.edgeDevices.list({ enabled: true }),
    staleTime: 15_000
  });
  const channelListError = channelsQuery.isError
    ? describeError(channelsQuery.error, 'Channel list could not be loaded')
    : null;

  const channels = useMemo(() => channelsQuery.data?.items ?? [], [channelsQuery.data?.items]);
  const pipelineOptions = useMemo(
    () => publishedPipelineOptions(runtimePipelinesQuery.data?.items ?? []),
    [runtimePipelinesQuery.data?.items]
  );
  const visibleChannels = useMemo(() => filterChannels(channels, filters).filter(c => matchesPlace(c, place, places.data)), [channels, filters, place, places.data]);

  const createMutation = useMutation({
    mutationFn: (payload: RuntimeChannelCreateRequest) => api.channels.create(payload),
    onSuccess: (channel) => {
      setForm(initialForm);
      setCreateOpen(false);
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels-status'] });
      queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] });
      notify({ title: 'Channel created', description: channel.name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Channel creation failed'), tone: 'error' });
    }
  });
  const testFileUploadMutation = useMutation({
    mutationFn: (file: File) => api.channels.uploadTestFile(file),
    onSuccess: (upload) => {
      setForm((current) => ({
        ...current,
        sourceType: 'file_test',
        sourceAddress: String(upload.source_config.path ?? upload.path),
        testFileName: upload.file_name,
        testFileSourceConfig: upload.source_config,
        maxFps: current.maxFps,
        sampleEveryN: current.sampleEveryN || '1'
      }));
      notify({ title: 'Test file uploaded', description: upload.file_name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Test file upload failed'), tone: 'error' });
    }
  });

  const commandMutation = useMutation({
    mutationFn: ({
      channelId,
      command,
      force = false
    }: {
      channelId: string;
      command: 'start' | 'stop';
      force?: boolean;
    }) => {
      if (command === 'start') {
        return api.channels.start(channelId);
      }
      return api.channels.stop(channelId, {
        force,
        reason: force ? 'Operator force-stopped a stuck runtime transition.' : undefined
      });
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels-status'] });
      notify({
        title: 'Runtime command accepted',
        description: response.channel.name,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Runtime command failed'), tone: 'error' });
    }
  });

  const canCreate = Boolean(
      form.name.trim() &&
      form.technicalPlace.trim() &&
      sourceAddressValid(form.sourceType, form.sourceAddress) &&
      (form.sourceType !== 'basler_pylon' || validBaslerCameraIp(form.baslerCameraIp)) &&
      (form.executionTarget === 'server' || Boolean(form.edgeDeviceId)) &&
      !testFileUploadMutation.isPending
  );
  const showSourceTuning =
    form.sourceType === 'rtsp' ||
    form.sourceType === 'usb_opencv' ||
    form.sourceType === 'aravis_gige' ||
    form.sourceType === 'flir_blackfly' ||
    form.sourceType === 'basler_pylon' ||
    form.sourceType === 'file_test';

  return (
    <div className="panel-stack panel-stack--wide channel-list-page">
      {createOpen ? (
      <Modal title="Kanal hinzufügen" onClose={() => setCreateOpen(false)} wide><section id="channel-create-panel" className="channel-create-panel">
        <div className="channel-create-panel__header">
          <div>
            <h2>Neuen Kanal anlegen</h2>
            <p className="subtle-text">Quelle und technischen Einsatzort konfigurieren.</p>
          </div>
        </div>
        <form
          className="channel-form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canCreate) {
              return;
            }
            createMutation.mutate(buildCreatePayload(form));
          }}
        >
          <label>
            <span>Name</span>
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </label>
          <FacilitySelect value={form.facilityId} onChange={(id, name) => setForm({...form, facilityId: id, technicalPlace: name || form.technicalPlace})}/>
          <label>
            <span>Ortsbezeichnung</span>
            <input
              disabled={Boolean(form.facilityId)} value={form.technicalPlace}
              onChange={(event) => setForm({ ...form, technicalPlace: event.target.value })}
            />
          </label>
          <label>
            <span>Source type</span>
            <select
              value={form.sourceType}
              onChange={(event) =>
                setForm(resetSourceSelection(form, event.target.value as RuntimeSourceType))
              }
            >
              {sourceTypes.map((sourceType) => (
                <option key={sourceType} value={sourceType}>
                  {sourceTypeLabel(sourceType)}
                </option>
              ))}
            </select>
          </label>
          {form.sourceType === 'file_test' ? (
            <label className="channel-form-grid__wide test-file-picker">
              <span>Test file</span>
              <input
                type="file"
                accept="image/*,video/*"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    testFileUploadMutation.mutate(file);
                  }
                  event.target.value = '';
                }}
              />
              <small className="subtle-text">
                {testFileUploadMutation.isPending
                  ? 'Uploading test file...'
                  : form.testFileName
                    ? form.testFileName
                    : 'Choose an image or short video for pipeline tests.'}
              </small>
            </label>
          ) : (
            <label>
              <span>
                {form.sourceType === 'rtsp'
                  ? 'Stream URL'
                  : form.sourceType === 'usb_opencv'
                    ? 'USB device index or path'
                    : 'Source address'}
              </span>
              <input
                value={form.sourceAddress}
                onChange={(event) => setForm({ ...form, sourceAddress: event.target.value })}
                placeholder={sourcePlaceholder(form.sourceType)}
              />
              {form.sourceType === 'usb_opencv' ? (
                <small className="subtle-text">
                  Use an index such as 0 on Windows, or /dev/video0 bzw. a stable
                  /dev/v4l/by-id path on Linux/Jetson.
                </small>
              ) : null}
            </label>
          )}
          {form.sourceType === 'rtsp' ? (
            <>
              <label>
                <span>RTSP username</span>
                <input
                  value={form.rtspUsername}
                  onChange={(event) => setForm({ ...form, rtspUsername: event.target.value })}
                  autoComplete="off"
                />
              </label>
              <label>
                <span>RTSP password</span>
                <input
                  type="password"
                  value={form.rtspPassword}
                  onChange={(event) => setForm({ ...form, rtspPassword: event.target.value })}
                  autoComplete="new-password"
                />
              </label>
            </>
          ) : null}
          {form.sourceType === 'basler_pylon' ? (
            <>
              <BaslerCameraIpField
                value={form.baslerCameraIp}
                onChange={(baslerCameraIp) => setForm({ ...form, baslerCameraIp })}
              />
              <label>
                <span>Transport mode</span>
                <select
                  value={form.baslerTransportMode}
                  onChange={(event) => setForm({ ...form, baslerTransportMode: event.target.value })}
                >
                  {baslerTransportModes.map((mode) => (
                    <option key={mode.value} value={mode.value}>
                      {mode.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Acquisition mode</span>
                <select
                  value={form.baslerAcquisitionMode}
                  onChange={(event) => setForm({ ...form, baslerAcquisitionMode: event.target.value })}
                >
                  {baslerAcquisitionModes.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Grab strategy</span>
                <select
                  value={form.baslerGrabStrategy}
                  onChange={(event) => setForm({ ...form, baslerGrabStrategy: event.target.value })}
                >
                  {baslerGrabStrategies.map((strategy) => (
                    <option key={strategy} value={strategy}>
                      {strategy}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Pixel format</span>
                <select
                  value={form.baslerPixelFormat}
                  onChange={(event) => setForm({ ...form, baslerPixelFormat: event.target.value })}
                >
                  {baslerPixelFormats.map((format) => (
                    <option key={format} value={format}>
                      {format}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Camera FPS</span>
                <input
                  type="number"
                  min="0.1"
                  max="240"
                  step="0.1"
                  value={form.baslerCameraFps}
                  onChange={(event) => setForm({ ...form, baslerCameraFps: event.target.value })}
                />
              </label>
              <label>
                <span>Camera link speed (Mbit/s)</span>
                <input
                  type="number"
                  min="100"
                  max="100000"
                  step="100"
                  value={form.baslerLinkSpeedMbps}
                  onChange={(event) => setForm({ ...form, baslerLinkSpeedMbps: event.target.value })}
                />
              </label>
              <label>
                <span>Exposure time (us)</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerExposureTimeUs}
                  onChange={(event) => setForm({ ...form, baslerExposureTimeUs: event.target.value })}
                />
              </label>
              <label>
                <span>Gain (dB)</span>
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={form.baslerGainDb}
                  onChange={(event) => setForm({ ...form, baslerGainDb: event.target.value })}
                />
              </label>
              <label>
                <span>Trigger mode</span>
                <select
                  value={form.baslerTriggerMode}
                  onChange={(event) => setForm({ ...form, baslerTriggerMode: event.target.value })}
                >
                  {baslerTriggerModes.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </select>
              </label>
              {form.baslerTriggerMode === 'On' ? (
                <>
                  <label>
                    <span>Trigger source</span>
                    <select
                      value={form.baslerTriggerSource}
                      onChange={(event) => setForm({ ...form, baslerTriggerSource: event.target.value })}
                    >
                      {baslerTriggerSources.map((source) => (
                        <option key={source} value={source}>
                          {source}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Trigger activation</span>
                    <select
                      value={form.baslerTriggerActivation}
                      onChange={(event) => setForm({ ...form, baslerTriggerActivation: event.target.value })}
                    >
                      {baslerTriggerActivations.map((activation) => (
                        <option key={activation} value={activation}>
                          {activation}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              ) : null}
              <label>
                <span>Packet size</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerPacketSize}
                  onChange={(event) => setForm({ ...form, baslerPacketSize: event.target.value })}
                />
              </label>
              <label>
                <span>Inter-packet delay</span>
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={form.baslerInterPacketDelay}
                  onChange={(event) => setForm({ ...form, baslerInterPacketDelay: event.target.value })}
                />
              </label>
              <label>
                <span>Grab buffers</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerBufferCount}
                  onChange={(event) => setForm({ ...form, baslerBufferCount: event.target.value })}
                />
              </label>
              <label>
                <span>Output queue size</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerOutputQueueSize}
                  onChange={(event) => setForm({ ...form, baslerOutputQueueSize: event.target.value })}
                />
              </label>
              <label>
                <span>Frame retention (ms)</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerFrameRetentionMs}
                  onChange={(event) => setForm({ ...form, baslerFrameRetentionMs: event.target.value })}
                />
              </label>
              <label>
                <span>Reconnect after failed grabs</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerGrabFailureReconnectThreshold}
                  onChange={(event) => setForm({ ...form, baslerGrabFailureReconnectThreshold: event.target.value })}
                />
              </label>
              <label>
                <span>Width</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerWidth}
                  onChange={(event) => setForm({ ...form, baslerWidth: event.target.value })}
                />
              </label>
              <label>
                <span>Height</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.baslerHeight}
                  onChange={(event) => setForm({ ...form, baslerHeight: event.target.value })}
                />
              </label>
              {baslerConfigurationWarning(form) ? (
                <div className="channel-form-grid__wide inline-alert inline-alert--warn">
                  {baslerConfigurationWarning(form)}
                </div>
              ) : null}
            </>
          ) : null}
          {showSourceTuning ? (
            <>
              <label>
                <span>Max processing FPS</span>
                <input
                  type="number"
                  min="0.1"
                  max="120"
                  step="0.1"
                  value={form.maxFps}
                  onChange={(event) => setForm({ ...form, maxFps: event.target.value })}
                />
              </label>
              <label>
                <span>Sample every N</span>
                <input
                  type="number"
                  min="1"
                  max="1000"
                  step="1"
                  value={form.sampleEveryN}
                  onChange={(event) => setForm({ ...form, sampleEveryN: event.target.value })}
                />
              </label>
              <label>
                <span>Pipeline max. parallel</span>
                <input
                  type="number"
                  min="1"
                  max="16"
                  step="1"
                  value={form.pipelineMaxInflight}
                  onChange={(event) =>
                    setForm({ ...form, pipelineMaxInflight: event.target.value })
                  }
                />
              </label>
            </>
          ) : null}
          <label>
            <span>Pipeline template</span>
            <select
              value={form.pipelineVersionId}
              onChange={(event) => setForm({ ...form, pipelineVersionId: event.target.value })}
            >
              <option value="">
                {pipelineOptions.length ? 'No template' : 'Publish a runtime pipeline template first'}
              </option>
              {pipelineOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Ausführungsort</span>
            <select
              value={form.executionTarget}
              onChange={(event) =>
                setForm({
                  ...form,
                  executionTarget: event.target.value as 'server' | 'edge_device',
                  edgeDeviceId: event.target.value === 'server' ? '' : form.edgeDeviceId
                })
              }
            >
              <option value="server">Zentraler Server</option>
              <option value="edge_device">Edge Device</option>
            </select>
          </label>
          {form.executionTarget === 'edge_device' ? (
            <label>
              <span>Edge Device</span>
              <select
                required
                value={form.edgeDeviceId}
                onChange={(event) => setForm({ ...form, edgeDeviceId: event.target.value })}
              >
                <option value="">Gerät auswählen</option>
                {(edgeDevicesQuery.data?.items ?? []).map((device) => (
                  <option key={device.edge_device_id} value={device.edge_device_id}>
                    {device.name} / {device.address.management_ip} / {device.connectivity_state}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="channel-form-grid__wide">
            <span>Description</span>
            <textarea
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
            />
          </label>
          <label>
            <span>Capture</span>
            <input
              value={form.captureSummary}
              onChange={(event) => setForm({ ...form, captureSummary: event.target.value })}
              placeholder="Capture defects to Label Studio"
            />
          </label>
          <label>
            <span>Result history</span>
            <input
              type="checkbox"
              checked={form.historyEnabled}
              onChange={(event) => setForm({ ...form, historyEnabled: event.target.checked })}
            />
            <small>
              Off by default. Only model fields mapped to the History destination in Result
              Output are stored.
            </small>
          </label>
          {form.historyEnabled ? (
            <>
              <label>
                <span>Delete after (hours)</span>
                <input
                  type="number"
                  min="1"
                  max="8760"
                  value={form.historyRetentionHours}
                  onChange={(event) =>
                    setForm({ ...form, historyRetentionHours: event.target.value })
                  }
                />
              </label>
              <label>
                <span>Store at most every (seconds)</span>
                <input
                  type="number"
                  min="1"
                  max="3600"
                  value={form.historySampleIntervalSeconds}
                  onChange={(event) =>
                    setForm({ ...form, historySampleIntervalSeconds: event.target.value })
                  }
                />
              </label>
              <label>
                <span>Maximum records</span>
                <input
                  type="number"
                  min="1"
                  max="1000000"
                  value={form.historyMaxRecords}
                  onChange={(event) =>
                    setForm({ ...form, historyMaxRecords: event.target.value })
                  }
                />
              </label>
            </>
          ) : null}
          <div className="channel-form-grid__actions">
            <button type="submit" className="button--primary" disabled={!canCreate || createMutation.isPending}>
              {createMutation.isPending ? 'Creating...' : 'Create channel'}
            </button>
            <Link to="/runtime-pipelines" className="button-link">
              Open pipeline designer
            </Link>
          </div>
        </form>
      </section></Modal>
      ) : null}

      <div className={`ws-split-list ${treeOpen ? '' : 'ws-split-list--full'}`}>{treeOpen && <PlaceTree value={place} onChange={setPlace} channels={channels}/>}<section className="ws-list-workspace">
        {channelListError ? (
          <div className="inline-alert inline-alert--danger">
            <strong>{channelListError.title}</strong>
            {channelListError.description ? <p>{channelListError.description}</p> : null}
            <button type="button" onClick={() => channelsQuery.refetch()}>
              Retry
            </button>
          </div>
        ) : null}
        <div className="ws-toolbar"><ToolButton icon="tree" label="Anlagenstruktur ein-/ausblenden" active={treeOpen} onClick={() => setTreeOpen(value => !value)}/><Search value={filters.search} onChange={search => setFilters({...filters, search})} placeholder="Kanäle suchen …"/>
          <select aria-label="Bildquelle filtern" value={filters.sourceType} onChange={event => setFilters({...filters, sourceType: event.target.value as RuntimeSourceType | 'all'})}><option value="all">Alle Quellen</option>{sourceTypes.map(type => <option key={type} value={type}>{sourceTypeLabel(type)}</option>)}</select>
          <select aria-label="Kanalstatus filtern" value={filters.status} onChange={event => setFilters({...filters, status: event.target.value as ChannelFilters['status']})}>{[['all','Alle Zustände'],['running','Läuft'],['stopped','Gestoppt'],['starting','Startet'],['validating','Validiert'],['loading_pipeline','Lädt Pipeline'],['stopping','Stoppt'],['degraded','Warnung'],['failed','Fehler'],['ok','Bereit'],['down','Nicht erreichbar']].map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select>
          <span className="ws-spacer"/><ToolButton icon="list" label="Listenansicht" active={!monitor} onClick={() => setMonitor(false)}/><ToolButton icon="grid" label="Monitoransicht" active={monitor} onClick={() => setMonitor(true)}/><button className="ws-primary" onClick={() => setCreateOpen(true)}><Icon name="plus" size={14}/>Kanal anlegen</button>
        </div><div className="ws-scroll">
        {channelsQuery.isLoading && !channels.length ? (
          <EmptyState title="Loading channels" body="Runtime channels are being loaded from the admin API." />
        ) : !visibleChannels.length && !channelListError ? (
          <EmptyState
            title="No channels match the current filters"
            body="Adjust the search or create the first runtime channel for a technical place."
          />
        ) : !visibleChannels.length ? null : (
          <ChannelCollection monitor={monitor}>
            {visibleChannels.map((channel) => (
              <ChannelCard
                key={channel.channel_id}
                channel={channel}
                row={!monitor}
                commandPending={commandMutation.isPending}
                onCommand={(command) =>
                  commandMutation.mutate({
                    channelId: channel.channel_id,
                    command,
                    force:
                      command === 'stop' &&
                      (['validating', 'loading_pipeline', 'starting', 'stopping'].includes(
                        channelRuntimeActualState(channel)
                      ) ||
                        channel.runtime_state?.desired_state === 'start_requested' ||
                        channel.runtime_state?.desired_state === 'stop_requested' ||
                        channel.desired_state === 'start_requested' ||
                        channel.desired_state === 'stop_requested')
                  })
                }
              />
            ))}
          </ChannelCollection>
        )}
      </div><footer className="ws-statusline"><span>{visibleChannels.length} von {channels.length} Kanälen</span><span>Runtime und Live-Stream unabhängig bedienbar</span></footer></section></div>
    </div>
  );
}

function ChannelCollection({monitor, children}: {monitor: boolean; children: React.ReactNode}) {
  return monitor ? <div className="pipeline-grid channel-grid">{children}</div> : <table className="ws-table workspace-channel-table"><thead><tr><th>Bild</th><th>Kanal</th><th>Runtime</th><th>Quelle</th><th>Pipeline / Ausführung</th><th aria-label="Aktionen"/></tr></thead><tbody>{children}</tbody></table>;
}

function ChannelCard(props: {
  row?: boolean;
  channel: RuntimeChannelSummary;
  commandPending: boolean;
  onCommand: (command: 'start' | 'stop') => void;
}) {
  const tone = channelStatusTone(props.channel);
  const actualState = channelRuntimeActualState(props.channel);
  const desiredState =
    props.channel.runtime_state?.desired_state ?? props.channel.desired_state;
  const runtimeRecoveryRequired =
    ['validating', 'loading_pipeline', 'starting', 'stopping'].includes(actualState) ||
    desiredState === 'start_requested' ||
    desiredState === 'stop_requested';
  const runtimeActions = runtimeActionAvailability(props.channel, { busy: props.commandPending });
  if (props.row) return <tr>
    <td><Link to="/channels/$channelId" params={{channelId: props.channel.channel_id}} aria-label={`${props.channel.name} öffnen`}><ChannelLivePreview channel={props.channel} actualState={actualState}/></Link></td>
    <td><Link className="ws-object-link" to="/channels/$channelId" params={{channelId: props.channel.channel_id}}>{props.channel.name}</Link><small>{props.channel.technical_place || 'Nicht zugeordnet'}</small></td>
    <td><span className={`ws-badge ws-badge--${tone === 'success' ? 'ok' : tone === 'danger' ? 'bad' : tone === 'warn' ? 'warn' : 'muted'}`}><i/>{channelStatusLabel(props.channel)}</span></td>
    <td>{sourceTypeLabel(props.channel.source_type)}</td>
    <td>{props.channel.assigned_pipeline || 'Keine Pipeline'}<small>{props.channel.placement.target === 'server' ? 'Zentraler Server' : `Edge · ${props.channel.placement.edge_device_name ?? props.channel.placement.edge_management_ip ?? 'Gerät'}`}</small>{props.channel.placement.target === 'edge_device' && <small>Deployment: {props.channel.placement.edge_deployment_status ?? 'Ausstehend'} · Admission: {props.channel.placement.edge_admission_status ?? 'Ausstehend'}</small>}</td>
    <td><div className="ws-button-row"><button className="ws-icon-button" aria-label={`${props.channel.name} starten`} title={runtimeActions.start.reason || 'Starten'} disabled={!runtimeActions.start.enabled} onClick={() => props.onCommand('start')}><Icon name="play" size={15}/></button><button className="ws-icon-button" aria-label={`${props.channel.name} ${runtimeRecoveryRequired ? 'hart stoppen' : 'stoppen'}`} title={runtimeActions.stop.reason || (runtimeRecoveryRequired ? 'Hart stoppen' : 'Stoppen')} disabled={!runtimeActions.stop.enabled} onClick={() => props.onCommand('stop')}><Icon name="stop" size={14}/></button></div></td>
  </tr>;
  return (
    <article className="pipeline-card pipeline-card--compact channel-card">
      <div className="pipeline-card__title-row">
        <div>
          <p className="training-hero__eyebrow">{props.channel.technical_place}</p>
          <h3><Link to="/channels/$channelId" params={{channelId: props.channel.channel_id}}>{props.channel.name}</Link></h3>
        </div>
        <span className={`traffic-status traffic-status--${tone}`}>
          <span className={`channel-status-dot channel-status-dot--${tone}`} />
          {channelStatusLabel(props.channel)}
        </span>
      </div>

      <Link
        to="/channels/$channelId"
        params={{ channelId: props.channel.channel_id }}
        className="channel-card__preview-link"
        aria-label={`${props.channel.name} öffnen`}
      >
        <ChannelLivePreview channel={props.channel} actualState={actualState} />
      </Link>

      <span className={`traffic-status traffic-status--${props.channel.placement.target === 'edge_device' ? 'success' : 'neutral'}`}>
        {props.channel.placement.target === 'edge_device' ? 'Edge Device' : 'Server'}
      </span>
      <p className="subtle-text">
        Ausführung: {props.channel.placement.target === 'server'
          ? 'Zentraler Server'
          : `${props.channel.placement.edge_device_name ?? 'Edge Device'} / ${props.channel.placement.edge_management_ip ?? 'IP unbekannt'}`}
      </p>
      {props.channel.placement.target === 'edge_device' ? (
        <dl className="summary-list">
          <div>
            <dt>Edge Deployment</dt>
            <dd>{props.channel.placement.edge_deployment_status ?? 'Noch nicht ausgerollt'}</dd>
          </div>
          <div>
            <dt>Admission</dt>
            <dd>{props.channel.placement.edge_admission_status ?? 'Ausstehend'}</dd>
          </div>
          {props.channel.placement.workload_release_id ? (
            <div>
              <dt>Release</dt>
              <dd><code>{props.channel.placement.workload_release_id}</code></dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="channel-card__actions">
        <button
          type="button"
          className="button--primary"
          onClick={() => props.onCommand('start')}
          disabled={!runtimeActions.start.enabled}
          title={runtimeActions.start.reason}
        >
          Starten
        </button>
        <button
          type="button"
          onClick={() => props.onCommand('stop')}
          disabled={!runtimeActions.stop.enabled}
          title={runtimeActions.stop.reason}
        >
          {runtimeRecoveryRequired ? 'Hart stoppen' : 'Stoppen'}
        </button>
      </div>
    </article>
  );
}

function ChannelLivePreview(props: { channel: RuntimeChannelSummary; actualState: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [pageVisible, setPageVisible] = useState(true);
  useEffect(() => {
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => setVisible(entries.some(e => e.isIntersecting)));
    if (ref.current) observer?.observe(ref.current);
    if (!observer) setVisible(true);
    const update = () => setPageVisible(!document.hidden); update();
    document.addEventListener('visibilitychange', update);
    return () => {observer?.disconnect(); document.removeEventListener('visibilitychange', update);};
  }, []);
  const liveEnabled = visible && pageVisible && ['running', 'degraded'].includes(props.actualState);
  const edgeLeaseActive = useEdgeViewerLease(
    props.channel.channel_id,
    liveEnabled && props.channel.placement.target === 'edge_device',
    { maxFps: 0.5, maxWidth: 320 }
  );
  const livePreviewQuery = useQuery({
    queryKey: ['runtime-channel-live-preview', props.channel.channel_id],
    queryFn: () => api.channels.liveViewLatest(props.channel.channel_id, { maxFrameWidth: 220 }),
    enabled:
      liveEnabled &&
      (props.channel.placement.target !== 'edge_device' || edgeLeaseActive),
    refetchInterval: liveEnabled ? 3500 : false
  });
  const frame = livePreviewQuery.data?.state.frame ?? null;
  const imageSrc = frame?.image_base64 ? liveImageSource(frame.image_base64, frame.mime_type) : null;
  const label = liveEnabled
    ? livePreviewQuery.data?.state.stale
      ? 'Stale'
      : imageSrc
        ? 'Live'
        : 'Warming up'
    : 'Not live';

  return (
    <div ref={ref} className={`channel-card__live-preview ${imageSrc ? '' : 'channel-card__live-preview--empty'}`}>
      {imageSrc ? <img src={imageSrc} alt={`Live preview for ${props.channel.name}`} /> : <span>{label}</span>}
      <small>{label}</small>
    </div>
  );
}

function buildCreatePayload(form: typeof initialForm): RuntimeChannelCreateRequest {
  const { config, credentials } = sourceConfigAndCredentials(
    form.sourceType,
    form.sourceAddress,
    form.testFileSourceConfig,
    form.rtspUsername,
    form.rtspPassword,
    form.maxFps,
    form.sampleEveryN,
    form.pipelineMaxInflight,
    baslerPylonConfig(form.sourceAddress, form)
  );
  return {
    name: form.name.trim(),
    technical_place: form.technicalPlace.trim(),
    facility_id: form.facilityId || null,
    description: form.description.trim() || null,
    source_type: form.sourceType,
    source_config: config,
    source_credentials: credentials,
    mode: 'realtime',
    pipeline_version_id: form.pipelineVersionId || null,
    placement: {
      target: form.executionTarget,
      edge_device_id: form.executionTarget === 'edge_device' ? form.edgeDeviceId : null
    },
    result_history_policy: {
      enabled: form.historyEnabled,
      retention_hours: positiveInteger(form.historyRetentionHours, 24),
      sample_interval_seconds: positiveInteger(form.historySampleIntervalSeconds, 5),
      max_records: positiveInteger(form.historyMaxRecords, 10000)
    },
    capture_summary: form.captureSummary.trim()
      ? { summary: form.captureSummary.trim(), rule_count: 1, target_project_count: 0, modes: [] }
      : undefined
  };
}

function positiveInteger(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function publishedPipelineOptions(pipelines: RuntimePipelineDefinitionSummary[]) {
  return pipelines
    .filter((pipeline) => Boolean(pipeline.published_version_id))
    .map((pipeline) => ({
      value: pipeline.published_version_id ?? '',
      label: `${pipeline.name} / ${pipeline.published_version_mode ?? 'live'}${
        pipeline.description ? ` / ${pipeline.description}` : ''
      }`
    }));
}

function sourceConfigAndCredentials(
  sourceType: RuntimeSourceType,
  sourceAddress: string,
  testFileSourceConfig: Record<string, unknown>,
  rtspUsername: string,
  rtspPassword: string,
  maxFps: string,
  sampleEveryN: string,
  pipelineMaxInflight: string,
  baslerConfig: Record<string, unknown>
): { config: Record<string, unknown>; credentials?: { username?: string; password?: string } } {
  const value = sourceAddress.trim();
  if (sourceType === 'rtsp') {
    const parsed = parseCredentialedRtspUrl(value);
    const username = rtspUsername.trim() || parsed.username;
    const password = rtspPassword || parsed.password;
    return {
      config: withRealtimeSourceTuning(
        parsed.config,
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      ),
      credentials:
        username || password
          ? {
              ...(username ? { username } : {}),
              ...(password ? { password } : {})
            }
          : undefined
    };
  }
  if (sourceType === 'snapshot') {
    return { config: { url: value } };
  }
  if (sourceType === 'usb_opencv') {
    return {
      config: withRealtimeSourceTuning(
        usbOpenCvConfig(value),
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'flir_blackfly') {
    return {
      config: withRealtimeSourceTuning(
        flirBlackflyConfig(value),
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'aravis_gige') {
    return {
      config: withRealtimeSourceTuning(
        aravisGigeConfig(value),
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'basler_pylon') {
    return {
      config: withRealtimeSourceTuning(
        baslerConfig,
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'file_test') {
    return {
      config: withRealtimeSourceTuning(
        {
          ...testFileSourceConfig,
          path: value
        },
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  return { config: value ? { camera: value } : {} };
}

function withRealtimeSourceTuning(
  config: Record<string, unknown>,
  maxFps: string,
  sampleEveryN: string,
  pipelineMaxInflight: string
) {
  const tuned = { ...config };
  delete tuned.max_fps;
  delete tuned.sample_every_n;
  delete tuned.pipeline_max_inflight;
  const fps = Number(maxFps);
  if (Number.isFinite(fps) && fps > 0) {
    tuned.max_fps = Math.min(Math.max(fps, 0.1), 120);
  }
  const sample = Math.round(Number(sampleEveryN));
  if (Number.isFinite(sample) && sample > 1) {
    tuned.sample_every_n = Math.min(Math.max(sample, 1), 1000);
  }
  const maxInflight = Math.round(Number(pipelineMaxInflight));
  if (Number.isFinite(maxInflight) && maxInflight > 0) {
    tuned.pipeline_max_inflight = Math.min(Math.max(maxInflight, 1), 16);
  }
  return tuned;
}

function sourceAddressValid(sourceType: RuntimeSourceType, sourceAddress: string) {
  if (sourceType === 'snapshot') {
    return /^https?:\/\//i.test(sourceAddress.trim());
  }
  return sourceType === 'trigger_capture' || Boolean(sourceAddress.trim());
}

function sourcePlaceholder(sourceType: RuntimeSourceType) {
  if (sourceType === 'rtsp') {
    return 'rtsp://camera.local/stream';
  }
  if (sourceType === 'usb_opencv') {
    return '0 or /dev/v4l/by-id/usb-camera-video-index0';
  }
  if (sourceType === 'flir_blackfly') {
    return 'serial number, device id, or 192.168.10.42';
  }
  if (sourceType === 'basler_pylon') {
    return 'serial, name:PressCam, model:a2A1920-51gcBAS, or 192.168.10.42';
  }
  if (sourceType === 'aravis_gige') {
    return 'Aravis camera name, serial number, or 192.168.10.42';
  }
  if (sourceType === 'snapshot') {
    return 'https://camera.local/latest.jpg';
  }
  if (sourceType === 'file_test') {
    return '/data/runtime/test-image.jpg';
  }
  return 'Press camera 1';
}

function flirBlackflyConfig(value: string) {
  const trimmed = value.trim();
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(trimmed)) {
    return { ip_address: trimmed };
  }
  if (trimmed.startsWith('id:')) {
    return { device_id: trimmed.slice(3).trim() };
  }
  if (trimmed.startsWith('name:')) {
    return { user_defined_name: trimmed.slice(5).trim() };
  }
  return { serial_number: trimmed };
}

function usbOpenCvConfig(value: string, existing: Record<string, unknown> = {}) {
  const trimmed = value.trim();
  const config = { ...existing };
  delete config.device_index;
  delete config.device_path;
  delete config.device;
  delete config.uri;
  if (/^\d+$/.test(trimmed)) {
    return { ...config, device_index: Number.parseInt(trimmed, 10) };
  }
  return { ...config, device_path: trimmed };
}

function aravisGigeConfig(value: string) {
  return {
    ...flirBlackflyConfig(value),
    camera_name: value,
    vendor: 'flir_blackfly',
    pixel_format: 'BayerRG8',
    conversion_mode: 'bayer'
  };
}

function baslerPylonConfig(value: string, form: typeof initialForm) {
  const config: Record<string, unknown> = {
    ...baslerCameraAddressConfig(baslerIdentifierConfig(value), form.baslerCameraIp),
    transport_mode: form.baslerTransportMode || 'auto',
    acquisition_mode: form.baslerAcquisitionMode || 'Continuous',
    grab_strategy: form.baslerGrabStrategy || 'LatestImageOnly',
    pixel_format: form.baslerPixelFormat || 'BayerRG8',
    trigger_mode: form.baslerTriggerMode || 'Off'
  };
  addPositiveNumber(config, 'fps', form.baslerCameraFps);
  addPositiveNumber(config, 'link_speed_mbps', form.baslerLinkSpeedMbps);
  if (config.fps !== undefined) {
    config.acquisition_frame_rate_enable = true;
  }
  addPositiveNumber(config, 'exposure_time_us', form.baslerExposureTimeUs);
  addNonNegativeNumber(config, 'gain_db', form.baslerGainDb);
  addPositiveInteger(config, 'packet_size', form.baslerPacketSize);
  addNonNegativeInteger(config, 'inter_packet_delay', form.baslerInterPacketDelay);
  addPositiveInteger(config, 'buffer_count', form.baslerBufferCount);
  addPositiveInteger(config, 'output_queue_size', form.baslerOutputQueueSize);
  addPositiveInteger(config, 'frame_retention_ms', form.baslerFrameRetentionMs);
  addPositiveInteger(
    config,
    'grab_failure_reconnect_threshold',
    form.baslerGrabFailureReconnectThreshold
  );
  addPositiveInteger(config, 'width', form.baslerWidth);
  addPositiveInteger(config, 'height', form.baslerHeight);
  if (form.baslerTriggerMode === 'On') {
    config.trigger_source = form.baslerTriggerSource || 'Line1';
    config.trigger_activation = form.baslerTriggerActivation || 'RisingEdge';
  }
  return config;
}

function baslerIdentifierConfig(value: string) {
  const trimmed = value.trim();
  if (trimmed.startsWith('model:')) {
    return { model: trimmed.slice(6).trim() };
  }
  if (trimmed.startsWith('camera:')) {
    return { camera_name: trimmed.slice(7).trim() };
  }
  return flirBlackflyConfig(trimmed);
}

function baslerConfigurationWarning(form: typeof initialForm) {
  const width = Number(form.baslerWidth);
  const height = Number(form.baslerHeight);
  const fps = Number(form.baslerCameraFps);
  const linkSpeedMbps = Number(form.baslerLinkSpeedMbps);
  const bitsPerPixel: Record<string, number> = {
    Mono8: 8,
    BayerRG8: 8,
    BayerBG8: 8,
    BayerGB8: 8,
    BayerGR8: 8,
    YUV422Packed: 16,
    RGB8: 24,
    BGR8: 24
  };
  const bpp = bitsPerPixel[form.baslerPixelFormat];
  if (width > 0 && height > 0 && fps > 0 && linkSpeedMbps > 0 && bpp) {
    const payloadMbps = (width * height * fps * bpp) / 1_000_000;
    if (payloadMbps > linkSpeedMbps * 0.8) {
      return `Estimated raw payload is ${payloadMbps.toFixed(0)} Mbit/s before transport overhead. Keep it below roughly 80% of the configured ${linkSpeedMbps.toFixed(0)}-Mbit/s camera link by using Bayer/Mono8, a smaller ROI, or fewer FPS.`;
    }
  }
  if (
    form.baslerAcquisitionMode === 'Continuous' &&
    form.baslerTriggerMode === 'On' &&
    form.baslerTriggerSource === 'Software'
  ) {
    return 'Continuous live acquisition is configured with a synchronous software trigger. Use Trigger mode Off for a free-running stream unless every frame must be software-triggered.';
  }
  return null;
}

function addPositiveNumber(config: Record<string, unknown>, key: string, value: string) {
  const number = Number(value);
  if (Number.isFinite(number) && number > 0) {
    config[key] = number;
  }
}

function addNonNegativeNumber(config: Record<string, unknown>, key: string, value: string) {
  const number = Number(value);
  if (Number.isFinite(number) && number >= 0) {
    config[key] = number;
  }
}

function addPositiveInteger(config: Record<string, unknown>, key: string, value: string) {
  const number = Math.round(Number(value));
  if (Number.isFinite(number) && number > 0) {
    config[key] = number;
  }
}

function addNonNegativeInteger(config: Record<string, unknown>, key: string, value: string) {
  const number = Math.round(Number(value));
  if (Number.isFinite(number) && number >= 0) {
    config[key] = number;
  }
}

function liveImageSource(imageBase64: string, mimeType = 'image/jpeg') {
  return imageBase64.startsWith('data:') ? imageBase64 : `data:${mimeType};base64,${imageBase64}`;
}

function resetSourceSelection(form: typeof initialForm, sourceType: RuntimeSourceType) {
  return {
    ...form,
    sourceType,
    sourceAddress: '',
    testFileName: '',
    testFileSourceConfig: {}
  };
}

function parseCredentialedRtspUrl(value: string) {
  if (!value) {
    return { config: { url: value }, username: '', password: '' };
  }
  try {
    const parsed = new URL(value);
    if (!['rtsp:', 'rtsps:'].includes(parsed.protocol) || (!parsed.username && !parsed.password)) {
      return { config: { url: value }, username: '', password: '' };
    }
    const username = decodeURIComponent(parsed.username);
    const password = decodeURIComponent(parsed.password);
    parsed.username = '';
    parsed.password = '';
    return { config: { url: parsed.toString() }, username, password };
  } catch {
    return { config: { url: value }, username: '', password: '' };
  }
}
