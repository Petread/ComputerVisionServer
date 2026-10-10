import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type {
  RuntimeChannelStreamListResponse,
  RuntimeLiveViewState,
  RuntimeStemRotationPriorityControl
} from '@cv-platform/schemas';
import {
  buildServiceEndpointUrl,
  buildChannelPlacementUpdate,
  canonicalCaptureEligibilityPolicy,
  channelPlacementEditable,
  ChannelHttpRequestEndpointsPanel,
  cropOverlayPrimitive,
  LiveFrameView,
  ManualLiveCapturePanel,
  mergeRuntimeLiveViewState,
  overlayClassColors,
  overlayPrimitiveLabel,
  overlayStaleAfterMs,
  PipelineResultPanel,
  selectBrowserStreamEndpoint,
  StemRotationPriorityControlPanel,
  toAbsoluteEndpointUrl
} from './channel-detail-page';
import {
  effectiveRuntimeActualState,
  shouldUseLiveFrameStream
} from './presentation';

describe('channel execution placement', () => {
  it('creates an Edge placement update only when the target changes', () => {
    expect(
      buildChannelPlacementUpdate('edge_device', ' edge_jetson ', {
        target: 'server',
        edge_device_id: null
      })
    ).toEqual({ target: 'edge_device', edge_device_id: 'edge_jetson' });

    expect(
      buildChannelPlacementUpdate('edge_device', 'edge_jetson', {
        target: 'edge_device',
        edge_device_id: 'edge_jetson'
      })
    ).toBeUndefined();

    expect(
      buildChannelPlacementUpdate('server', '', {
        target: 'edge_device',
        edge_device_id: 'edge_jetson'
      })
    ).toEqual({ target: 'server', edge_device_id: null });
  });

  it('allows placement changes only after the runtime has stopped', () => {
    expect(
      channelPlacementEditable({
        desired_state: 'stopped',
        observed_state: 'stopped',
        status: 'stopped',
        placement: { target: 'server' },
        runtime_state: null
      })
    ).toBe(true);
    expect(
      channelPlacementEditable({
        desired_state: 'running',
        observed_state: 'degraded',
        status: 'degraded',
        placement: { target: 'server' },
        runtime_state: null
      })
    ).toBe(false);
    expect(
      channelPlacementEditable({
        desired_state: 'stopped',
        observed_state: 'starting',
        status: 'starting',
        placement: { target: 'server' },
        runtime_state: null
      })
    ).toBe(false);
  });
});

const frontendPriorityControl: RuntimeStemRotationPriorityControl = {
  schema_version: 'runtime.stem-rotation-priority-control.v1',
  priority_source: 'FRONTEND',
  priority_mode: 'VORSCHNITT_KRITISCH',
  optimization_enable: true,
  revision: 4,
  updated_at: '2026-08-20T10:00:00Z',
  updated_by: 'commissioning'
};

describe('StemRotationPriorityControlPanel', () => {
  it('shows both geometric modes as editable for the frontend source', () => {
    const html = renderToStaticMarkup(
      <StemRotationPriorityControlPanel
        control={frontendPriorityControl}
        busy={false}
        onModeChange={vi.fn()}
        onOptimizationEnableChange={vi.fn()}
      />
    );

    expect(html).toContain('Quelle: Frontend (Test/Inbetriebnahme)');
    expect(html).toContain('Vorschnitt / oben-unten');
    expect(html).toContain('Nachschnitt / links-rechts');
    expect(html).toContain('aria-pressed="true"');
    expect(html).not.toContain('SPS-Leseadapter ist derzeit');
  });

  it('makes frontend mode controls read-only when PLC is the active source', () => {
    const html = renderToStaticMarkup(
      <StemRotationPriorityControlPanel
        control={{ ...frontendPriorityControl, priority_source: 'PLC' }}
        busy={false}
        onModeChange={vi.fn()}
        onOptimizationEnableChange={vi.fn()}
      />
    );

    expect(html).toContain('Quelle: SPS (nur Anzeige)');
    expect(html).toContain('SPS-Leseadapter ist derzeit');
    expect((html.match(/disabled=""/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
});

describe('live overlay presentation', () => {
  it('keeps hydrated live images when a lightweight overlay event arrives', () => {
    const polled = {
      channel_id: 'chn_stem',
      actual_state: 'running',
      available: true,
      stale: false,
      frame: { image_base64: 'canonical', sequence: 10, metadata: {} },
      source_frame: { image_base64: 'raw', sequence: 10, metadata: {} },
      undistorted_frame: { image_base64: 'undistorted', sequence: 10, metadata: {} },
      overlay: [],
      source_overlay: [],
      raw_overlay: [],
      layers_available: [],
      metrics: {},
      source_health: {}
    } as unknown as RuntimeLiveViewState;
    const event = {
      ...polled,
      frame: { image_base64: null, sequence: 11, metadata: {} },
      source_frame: { image_base64: null, sequence: 11, metadata: {} },
      undistorted_frame: { image_base64: null, sequence: 11, metadata: {} },
      overlay: [{ primitive_type: 'label', layer: 'fresh', geometry: {}, style: {}, metadata: {} }],
      source_overlay: [
        { primitive_type: 'label', layer: 'source-fresh', geometry: {}, style: {}, metadata: {} }
      ],
      raw_overlay: [
        { primitive_type: 'label', layer: 'raw-fresh', geometry: {}, style: {}, metadata: {} }
      ],
      layers_available: ['fresh']
    } as unknown as RuntimeLiveViewState;

    const merged = mergeRuntimeLiveViewState(polled, event, true, true);

    expect(merged?.frame?.image_base64).toBe('canonical');
    expect(merged?.source_frame?.image_base64).toBe('raw');
    expect(merged?.undistorted_frame?.image_base64).toBe('undistorted');
    expect(merged?.overlay[0]?.layer).toBe('fresh');
    expect(merged?.source_overlay?.[0]?.layer).toBe('source-fresh');
    expect(merged?.raw_overlay?.[0]?.layer).toBe('raw-fresh');
  });

  it('keeps REST fallback pixels and overlays from the same snapshot', () => {
    const polled = {
      channel_id: 'chn_photo',
      actual_state: 'running',
      available: true,
      stale: false,
      frame: { image_base64: 'photo-a', sequence: 10, metadata: {} },
      overlay: [
        { primitive_type: 'label', layer: 'photo-a-label', geometry: {}, style: {}, metadata: {} }
      ],
      source_overlay: [],
      raw_overlay: [],
      layers_available: ['photo-a-label'],
      metrics: {},
      source_health: {}
    } as unknown as RuntimeLiveViewState;
    const newerEvent = {
      ...polled,
      frame: { image_base64: null, sequence: 11, metadata: {} },
      overlay: [
        { primitive_type: 'label', layer: 'photo-b-label', geometry: {}, style: {}, metadata: {} }
      ],
      layers_available: ['photo-b-label']
    } as unknown as RuntimeLiveViewState;

    const merged = mergeRuntimeLiveViewState(polled, newerEvent, true, false);

    expect(merged?.frame?.sequence).toBe(10);
    expect(merged?.frame?.image_base64).toBe('photo-a');
    expect(merged?.overlay[0]?.layer).toBe('photo-a-label');
    expect(merged?.layers_available).toEqual(['photo-a-label']);
  });

  it('derives the processed crop box in original-frame coordinates', () => {
    const state = {
      display_context_id: 'ctx_crop_1',
      frame: {
        sequence: 17,
        width: 320,
        height: 200,
        image_context: {
          context_id: 'ctx_crop_1',
          transform: {
            transform_type: 'crop',
            parameters: {
              x: 137,
              y: 83,
              width: 320,
              height: 200,
              source_width: 1280,
              source_height: 720
            }
          }
        }
      },
      source_frame: {
        width: 1280,
        height: 720,
        image_context: { context_id: 'ctx_original_1', kind: 'original' }
      }
    } as unknown as RuntimeLiveViewState;

    expect(cropOverlayPrimitive(state)).toMatchObject({
      primitive_type: 'box',
      layer: 'pipeline_crop',
      image_context_id: 'ctx_original_1',
      geometry: {
        x: 137,
        y: 83,
        width: 320,
        height: 200,
        coordinate_system: 'image_pixels'
      },
      resolution: { width: 1280, height: 720 }
    });
  });

  it('formats class labels with confidence values', () => {
    expect(
      overlayPrimitiveLabel({
        primitive_type: 'polygon',
        layer: 'segmentation',
        label: 'Waldkante',
        confidence: 0.319971,
        geometry: {},
        coordinate_system: 'normalized_xy',
        resolution: {},
        style: {},
        metadata: {}
      })
    ).toBe('Waldkante · 32.0%');
  });

  it('assigns stable, class-specific overlay colors', () => {
    expect(overlayClassColors('Waldkante')).toEqual(overlayClassColors('waldkante'));
    expect(overlayClassColors('Waldkante')).not.toEqual(overlayClassColors('Riss'));
  });

  it('expires realtime overlays according to the configured processing cadence', () => {
    const state = {
      diagnostics: { effective_source_limit_fps: 1 },
      metrics: {}
    } as RuntimeLiveViewState;

    expect(overlayStaleAfterMs(state)).toBe(2500);
    expect(
      overlayStaleAfterMs({
        ...state,
        diagnostics: { effective_source_limit_fps: 30 }
      } as RuntimeLiveViewState)
    ).toBe(2000);
  });

  it('uses a ready stream to recover live metadata while persistence still says starting', () => {
    expect(shouldUseLiveFrameStream('starting', 'ready')).toBe(true);
    expect(shouldUseLiveFrameStream('starting', 'starting')).toBe(false);
  });

  it('shows fresh live runtime state ahead of stale persisted lifecycle state', () => {
    expect(
      effectiveRuntimeActualState('starting', {
        actual_state: 'degraded',
        available: true,
        stale: false
      })
    ).toBe('degraded');
    expect(
      effectiveRuntimeActualState('starting', {
        actual_state: 'running',
        available: true,
        stale: true
      })
    ).toBe('starting');
  });
});

const targets = [
  {
    capture_target_id: 'lpr_internal_42',
    display_name: 'Bearing defects',
    detail: 'detection - Press dataset - active',
    task_type: 'detection' as const,
    status: 'active',
    dataset_name: 'Press dataset',
    label_studio_project_id: 42,
    selectable: true
  }
];

describe('ManualLiveCapturePanel', () => {
  it('allows one bootstrap snapshot but not interval capture without eligibility', () => {
    expect(canonicalCaptureEligibilityPolicy(null)).toEqual({
      singleCaptureAllowed: true,
      intervalCaptureAllowed: false,
      requireEligibility: false
    });
    expect(canonicalCaptureEligibilityPolicy('REJECTED').singleCaptureAllowed).toBe(false);
    expect(canonicalCaptureEligibilityPolicy('ACCEPTED')).toEqual({
      singleCaptureAllowed: true,
      intervalCaptureAllowed: true,
      requireEligibility: true
    });
  });

  it('renders project selection and manual capture controls', () => {
    const html = renderToStaticMarkup(
      <ManualLiveCapturePanel
        targets={targets}
        targetsLoading={false}
        selectedTargetId="lpr_internal_42"
        onSelectTarget={vi.fn()}
        intervalSeconds={10}
        onIntervalSecondsChange={vi.fn()}
        requestPredictions={true}
        onRequestPredictionsChange={vi.fn()}
        captureSource="LIVE_VIEW_FRAME"
        onCaptureSourceChange={vi.fn()}
        session={{ channel_id: 'chn_press', state: 'idle', capture_count: 0, failure_count: 0 }}
        lastResult={null}
        feedback={null}
        busy={false}
        onCaptureOnce={vi.fn()}
        onStartInterval={vi.fn()}
        onStopInterval={vi.fn()}
      />
    );

    expect(html).toContain('Label Studio project');
    expect(html).toContain('Bearing defects - detection - Press dataset - active');
    expect(html).toContain('Capture once');
    expect(html).toContain('Predictions');
    expect(html).toContain('Start');
    expect(html).toContain('Stop');
  });

  it('shows running interval state and recent result feedback', () => {
    const html = renderToStaticMarkup(
      <ManualLiveCapturePanel
        targets={targets}
        targetsLoading={false}
        selectedTargetId="lpr_internal_42"
        onSelectTarget={vi.fn()}
        intervalSeconds={5}
        onIntervalSecondsChange={vi.fn()}
        requestPredictions={true}
        onRequestPredictionsChange={vi.fn()}
        captureSource="LIVE_VIEW_FRAME"
        onCaptureSourceChange={vi.fn()}
        session={{
          manual_capture_session_id: 'mcs_1',
          channel_id: 'chn_press',
          state: 'running',
          capture_target_id: 'lpr_internal_42',
          target_display_name: 'Bearing defects',
          label_studio_project_id: 42,
          interval_seconds: 5,
          capture_count: 3,
          failure_count: 0
        }}
        lastResult={{
          capture_request_id: 'mcr_1',
          channel_id: 'chn_press',
          channel_display_name: 'Press live camera',
          capture_target_id: 'lpr_internal_42',
          target_display_name: 'Bearing defects',
          label_studio_project_id: 42,
          mode: 'interval',
          interval_seconds: 5,
          captured_at: '2026-05-01T10:00:00Z',
          operator_user_id: 'usr_operator',
          source_frame_metadata: {},
          status: 'success',
          label_studio_task_ids: [7001],
          metadata: {}
        }}
        feedback={{ tone: 'success', message: 'Interval capture running every 5 seconds.' }}
        busy={false}
        onCaptureOnce={vi.fn()}
        onStartInterval={vi.fn()}
        onStopInterval={vi.fn()}
      />
    );

    expect(html).toContain('Running');
    expect(html).toContain('Interval / 5s');
    expect(html).toContain('Captured');
    expect(html).toContain('Interval capture running every 5 seconds.');
  });
});

describe('selectBrowserStreamEndpoint', () => {
  it('selects the only ready raw WHEP endpoint', () => {
    const response: RuntimeChannelStreamListResponse = {
      schema_version: 'runtime.channel-streams.list.v2',
      channel_id: 'chn_live',
      desired_state: 'running',
      actual_state: 'ready',
      negotiated_profile: { width: 1900, height: 1200, fps: 30 },
      components: {
        camera: { state: 'ready', ready: true, details: {} },
        encoder: { state: 'ready', ready: true, details: {} },
        publication: { state: 'ready', ready: true, details: {} },
        browser_readers: { state: 'idle', ready: false, details: {} }
      },
      stream_auth_enabled: false,
      access_policy_default: 'unauthenticated_lan',
      items: [
        {
          variant: 'raw',
          status: 'ready',
          intended_use: 'Browser live video',
          overlay_included: false,
          protocol: 'webrtc',
          endpoints: [
            {
              protocol: 'webrtc',
              url: '/stream-webrtc/channels/camera/live/whep',
              browser_compatible: true,
              vlc_compatible: false
            }
          ],
          codec: 'h264',
          encoder: {
            mode: 'gpu',
            encoder: 'nvcudah264enc',
            codec: 'h264',
            hardware_accelerated: true
          },
          compositor_mode: 'none',
          fanout: 'server',
          multicast: false,
          access_policy: 'unauthenticated_lan',
          authentication_required: false,
          ready: true,
          metrics: {
            dropped_frames: 0,
            repeated_frames: 0,
            connected_clients: 0,
            reconnect_count: 0,
            last_frame_sequence: 42
          }
        }
      ],
      warnings: [],
      meta: { request_id: 'req_test', generated_at: '2026-07-22T12:00:00Z' }
    };

    expect(selectBrowserStreamEndpoint(response)).toMatchObject({
      protocol: 'webrtc',
      url: '/stream-webrtc/channels/camera/live/whep'
    });
    response.items.push({
      ...response.items[0],
      variant: 'canonical_model',
      intended_use: 'Canonical model view',
      endpoints: [
        {
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/model/whep',
          browser_compatible: true,
          vlc_compatible: false
        }
      ]
    });
    expect(selectBrowserStreamEndpoint(response, 'canonical_model')).toMatchObject({
      protocol: 'webrtc',
      url: '/stream-webrtc/channels/camera/model/whep'
    });
    response.items.push({
      ...response.items[0],
      variant: 'undistorted_full',
      intended_use: 'Undistorted full view',
      endpoints: [
        {
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/undistorted/whep',
          browser_compatible: true,
          vlc_compatible: false
        }
      ]
    });
    expect(selectBrowserStreamEndpoint(response, 'undistorted_full')).toMatchObject({
      protocol: 'webrtc',
      url: '/stream-webrtc/channels/camera/undistorted/whep'
    });
  });

  it('returns no endpoint while the single publisher is not ready', () => {
    const response = {
      schema_version: 'runtime.channel-streams.list.v2',
      channel_id: 'chn_live',
      desired_state: 'running',
      actual_state: 'starting',
      negotiated_profile: {},
      components: {
        camera: { state: 'ready', ready: true, details: {} },
        encoder: { state: 'starting', ready: false, details: {} },
        publication: { state: 'starting', ready: false, details: {} },
        browser_readers: { state: 'idle', ready: false, details: {} }
      },
      stream_auth_enabled: false,
      access_policy_default: 'unauthenticated_lan',
      items: [],
      warnings: [],
      meta: { request_id: 'req_test', generated_at: '2026-07-22T12:00:00Z' }
    } satisfies RuntimeChannelStreamListResponse;

    expect(selectBrowserStreamEndpoint(response)).toBeNull();
  });
});

describe('toAbsoluteEndpointUrl', () => {
  it('resolves relative stream endpoints against the current browser origin', () => {
    expect(
      toAbsoluteEndpointUrl(
        '/stream-webrtc/channels/camera/raw/whep',
        'https://172.23.27.242'
      )
    ).toBe('https://172.23.27.242/stream-webrtc/channels/camera/raw/whep');
    expect(toAbsoluteEndpointUrl('https://example.invalid/stream')).toBe(
      'https://example.invalid/stream'
    );
  });
});


describe('LiveFrameView', () => {
  it('renders production WebRTC streams as a video element instead of an iframe', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        visibleLayers={{}}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/raw/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={{
          channel_id: 'chn_live',
          runtime_instance_id: 'rex_live',
          actual_state: 'running',
          available: true,
          stale: false,
          frame: null,
          overlay: [],
          layers_available: [],
          metrics: {
            frame_count: 12,
            acquired_frame_count: 14,
            dropped_frame_count: 2,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'RTSP source connected',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-05-01T10:00:01Z'
        }}
      />
    );

    expect(html).toContain('<video');
    expect(html).toContain('live-view-image--webrtc');
    expect(html).not.toContain('<iframe');
  });

  it('switches from the full stream to the canonical pipeline model frame', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        previewView="model"
        visibleLayers={{}}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/raw/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={{
          channel_id: 'chn_live',
          runtime_instance_id: 'rex_live',
          actual_state: 'running',
          available: true,
          stale: false,
          frame: {
            image_base64: 'Y2Fub25pY2FsLXZpZXc=',
            mime_type: 'image/jpeg',
            width: 1600,
            height: 640,
            sequence: 12,
            metadata: { view_profile_id: 'stem-measurement-view-v1' }
          },
          source_frame: {
            image_base64: 'ZnVsbC12aWV3',
            mime_type: 'image/jpeg',
            width: 1920,
            height: 1080,
            sequence: 12,
            metadata: {}
          },
          overlay: [],
          layers_available: [],
          metrics: {
            frame_count: 12,
            acquired_frame_count: 12,
            dropped_frame_count: 0,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'RTSP source connected',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-08-05T10:00:01Z'
        }}
      />
    );

    expect(html).toContain('data:image/jpeg;base64,Y2Fub25pY2FsLXZpZXc=');
    expect(html).toContain('Canonical runtime model frame');
    expect(html).not.toContain('<video');
    expect(html).not.toContain('data:image/jpeg;base64,ZnVsbC12aWV3');
  });

  it('uses the continuous canonical WebRTC stream for the model view', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        previewView="model"
        visibleLayers={{}}
        canonicalBrowserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/model/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={
          {
            channel_id: 'chn_live',
            actual_state: 'running',
            available: true,
            stale: false,
            frame: {
              image_base64: 'Y2Fub25pY2FsLXZpZXc=',
              mime_type: 'image/jpeg',
              width: 1600,
              height: 640,
              sequence: 12,
              metadata: { view_profile_id: 'stem-measurement-view-v1' }
            },
            overlay: [],
            layers_available: [],
            metrics: {},
            source_health: { connected: true, reconnect_count: 0, source_error_count: 0 }
          } as unknown as RuntimeLiveViewState
        }
      />
    );

    expect(html).toContain('<video');
    expect(html).toContain('live-view-image--webrtc');
    expect(html).not.toContain('data:image/jpeg;base64,Y2Fub25pY2FsLXZpZXc=');
  });

  it('renders a real frame with runtime-bound overlay primitives', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        previewView="model"
        visibleLayers={{ detections: true, values: true, reference_edges: true }}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/live/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={{
          channel_id: 'chn_live',
          runtime_instance_id: 'rex_live',
          actual_state: 'running',
          available: true,
          stale: false,
          frame: {
            image_base64: 'ZmFrZS1pbWFnZQ==',
            mime_type: 'image/jpeg',
            width: 640,
            height: 360,
            source_timestamp: '2026-05-01T10:00:00Z',
            captured_at: '2026-05-01T10:00:01Z',
            sequence: 12,
            metadata: {}
          },
          overlay: [
            {
              primitive_type: 'box',
              layer: 'detections',
              label: 'defect',
              confidence: 0.92,
              geometry: { x_min: 0.1, y_min: 0.2, x_max: 0.4, y_max: 0.6 },
              style: {},
              metadata: {}
            },
            {
              primitive_type: 'measurement',
              layer: 'values',
              label: 'count',
              geometry: {},
              style: {},
              metadata: { value: 1 }
            },
            {
              primitive_type: 'line',
              layer: 'reference_edges',
              label: 'start_edge',
              geometry: {
                start: { x: 10, y: 10 },
                end: { x: 10, y: 90 },
                coordinate_system: 'pixel_xy'
              },
              style: { stroke: '#5db7ff', stroke_width: 2 },
              metadata: {}
            }
          ],
          layers_available: ['detections', 'values', 'reference_edges'],
          metrics: {
            frame_count: 12,
            acquired_frame_count: 14,
            dropped_frame_count: 2,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'RTSP source connected',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-05-01T10:00:01Z'
        }}
      />
    );

    expect(html).toContain('<img');
    expect(html).toContain('live-view-media');
    expect(html).toContain('aspect-ratio:640 / 360');
    expect(html).toContain('count: 1');
    expect(html).toContain('live-view-overlay__canvas');
  });

  it('renders server-projected overlays on the distorted raw WebRTC stream', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        visibleLayers={{ color_object_contour: true, values: true }}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/live/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={{
          channel_id: 'chn_live',
          runtime_instance_id: 'rex_live',
          actual_state: 'running',
          available: true,
          stale: false,
          frame: {
            image_base64: 'cGlwZWxpbmUtZnJhbWU=',
            mime_type: 'image/jpeg',
            width: 640,
            height: 360,
            source_timestamp: '2026-05-01T10:00:00Z',
            captured_at: '2026-05-01T10:00:01Z',
            sequence: 12,
            metadata: {}
          },
          overlay: [
            {
              primitive_type: 'polygon',
              layer: 'color_object_contour',
              label: 'board',
              confidence: 1,
              geometry: {
                points: [
                  { x: 0.1, y: 0.2 },
                  { x: 0.4, y: 0.2 },
                  { x: 0.4, y: 0.3 },
                  { x: 0.1, y: 0.3 }
                ],
                coordinate_system: 'normalized_xy'
              },
              style: {},
              metadata: {}
            },
            {
              primitive_type: 'measurement',
              layer: 'values',
              label: 'quality',
              geometry: {},
              style: {},
              metadata: { value: 'good' }
            }
          ],
          raw_overlay: [
            {
              primitive_type: 'polygon',
              layer: 'color_object_contour',
              label: 'board',
              confidence: 1,
              geometry: {
                points: [
                  { x: 0.11, y: 0.21 },
                  { x: 0.41, y: 0.21 },
                  { x: 0.41, y: 0.31 },
                  { x: 0.11, y: 0.31 }
                ],
                coordinate_system: 'normalized_xy'
              },
              style: {},
              metadata: { projected_from_intrinsic_calibration_hash: 'calibration-v1' }
            }
          ],
          layers_available: ['color_object_contour'],
          metrics: {
            frame_count: 12,
            acquired_frame_count: 12,
            dropped_frame_count: 0,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'RTSP source connected',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-05-01T10:00:01Z'
        }}
      />
    );

    expect(html).toContain('<video');
    expect(html).not.toContain('data:image/jpeg;base64,cGlwZWxpbmUtZnJhbWU=');
    expect(html).toContain('live-view-overlay__canvas');
    expect(html).toContain('quality: good');
  });

  it('renders server-projected overlays on the full camera frame', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        visibleLayers={{ segmentation: true }}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/live/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={{
          channel_id: 'chn_live',
          actual_state: 'running',
          available: true,
          stale: false,
          frame: {
            image_base64: 'Y2Fub25pY2Fs',
            mime_type: 'image/jpeg',
            width: 1600,
            height: 640,
            sequence: 22,
            metadata: { view_profile_id: 'stem-measurement-view-v1' }
          },
          undistorted_frame: {
            image_base64: 'dW5kaXN0b3J0ZWQ=',
            mime_type: 'image/jpeg',
            width: 1920,
            height: 1080,
            sequence: 22,
            metadata: {}
          },
          overlay: [],
          raw_overlay: [
            {
              primitive_type: 'polygon',
              layer: 'segmentation',
              label: 'stem',
              geometry: {
                points: [
                  { x: 0.2, y: 0.2 },
                  { x: 0.7, y: 0.2 },
                  { x: 0.7, y: 0.5 }
                ],
                coordinate_system: 'normalized_xy'
              },
              coordinate_system: 'normalized_xy',
              projected_from_context_id: 'ctx_canonical',
              style: {},
              metadata: {}
            }
          ],
          layers_available: ['segmentation'],
          metrics: {
            frame_count: 22,
            acquired_frame_count: 22,
            dropped_frame_count: 0,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'connected',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          }
        }}
      />
    );

    expect(html).not.toContain('data:image/jpeg;base64,dW5kaXN0b3J0ZWQ=');
    expect(html).toContain('live-view-overlay__canvas');
    expect(html).toContain('<video');
  });

  it('does not imply streaming when no live frame is available', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="stopped"
        visibleLayers={{}}
        liveState={null}
      />
    );

    expect(html).toContain('Runtime is not live');
    expect(html).not.toContain('<img');
  });

  it('renders the latest trigger frame even when the realtime runtime is stopping', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="stopping"
        visibleLayers={{}}
        liveState={{
          channel_id: 'chn_trigger',
          runtime_instance_id: 'trigger-latest-chn_trigger',
          actual_state: 'stopping',
          available: true,
          stale: false,
          frame: {
            image_base64: 'ZmFrZS10cmlnZ2VyLWZyYW1l',
            mime_type: 'image/jpeg',
            width: 320,
            height: 180,
            source_timestamp: '2026-06-05T08:00:00Z',
            captured_at: '2026-06-05T08:00:01Z',
            sequence: 4,
            metadata: { trigger_endpoint_id: 'tep_test' }
          },
          overlay: [],
          layers_available: [],
          metrics: {
            frame_count: 4,
            acquired_frame_count: 4,
            dropped_frame_count: 0,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'Latest trigger frame',
            connected: true,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-06-05T08:00:01Z'
        } as RuntimeLiveViewState}
      />
    );

    expect(html).toContain('Latest trigger channel frame');
    expect(html).toContain('live-view-media');
    expect(html).not.toContain('Runtime is not live');
  });

  it('prompts for trigger invocation when a trigger live view has no frame yet', () => {
    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="stopped"
        visibleLayers={{}}
        liveState={{
          channel_id: 'chn_trigger',
          runtime_instance_id: 'trigger-latest-chn_trigger',
          actual_state: 'stopped',
          available: false,
          stale: false,
          frame: null,
          overlay: [],
          layers_available: [],
          metrics: {
            frame_count: 0,
            acquired_frame_count: 0,
            dropped_frame_count: 0,
            reconnect_count: 0,
            source_error_count: 0,
            buffer_depth: 0
          },
          source_health: {
            state: 'ok',
            status_text: 'Waiting for trigger',
            connected: false,
            reconnect_count: 0,
            source_error_count: 0
          },
          updated_at: '2026-06-05T08:00:01Z'
        } as RuntimeLiveViewState}
      />
    );

    expect(html).toContain('Invoke the trigger pipeline to publish a control frame');
    expect(html).not.toContain('Runtime is not live');
  });

  it('respects hidden overlay layer filters for mask overlays', () => {
    const liveState = {
      channel_id: 'chn_live',
      runtime_instance_id: 'rex_live',
      actual_state: 'running',
      available: true,
      stale: false,
      frame: {
        image_base64: 'ZmFrZS1pbWFnZQ==',
        mime_type: 'image/jpeg',
        width: 320,
        height: 240,
        source_timestamp: '2026-05-01T10:00:00Z',
        captured_at: '2026-05-01T10:00:01Z',
        sequence: 13,
        metadata: {}
      },
      overlay: [
        {
          primitive_type: 'mask',
          layer: 'color_object_mask',
          label: 'Color object mask',
          geometry: { data: 'ZmFrZS1tYXNr' },
          style: { opacity: 0.36 },
          metadata: {}
        },
        {
          primitive_type: 'box',
          layer: 'color_object_box',
          label: 'board',
          confidence: 1,
          geometry: { x_min: 0.1, y_min: 0.2, x_max: 0.4, y_max: 0.6 },
          style: {},
          metadata: {}
        }
      ],
      layers_available: ['color_object_mask', 'color_object_box'],
      metrics: {
        frame_count: 13,
        acquired_frame_count: 13,
        dropped_frame_count: 0,
        reconnect_count: 0,
        source_error_count: 0,
        buffer_depth: 0
      },
      source_health: {
        state: 'ok',
        status_text: 'RTSP source connected',
        connected: true,
        reconnect_count: 0,
        source_error_count: 0
      },
      updated_at: '2026-05-01T10:00:01Z'
    } as RuntimeLiveViewState;

    const html = renderToStaticMarkup(
      <LiveFrameView
        actualState="running"
        previewView="model"
        visibleLayers={{ color_object_mask: false, color_object_box: true }}
        browserStreamEndpoint={{
          protocol: 'webrtc',
          url: '/stream-webrtc/channels/camera/live/whep',
          browser_compatible: true,
          vlc_compatible: false
        }}
        liveState={liveState}
      />
    );

    expect(html).not.toContain('live-view-overlay__mask');
    expect(html).toContain('live-view-overlay__canvas');
  });
});

describe('PipelineResultPanel', () => {
  it('renders the latest pipeline result without connector controls', () => {
    const html = renderToStaticMarkup(
      <PipelineResultPanel
        results={[
          {
            result_record_id: 'rrc_1',
            channel_id: 'chn_live',
            pipeline_version_id: 'plv_1',
            correlation_id: 'corr_1',
            completed_at: '2026-05-01T10:00:00Z',
            mode: 'realtime',
            status: 'completed',
            result_type: 'realtime',
            schema_version: 'runtime.execution.result.v1',
            result: {},
            trigger_response: { LKW: 2, Lader: 1, result: true },
            derived_preview: {},
            inference_output_count: 1,
            derived_output_count: 1,
            overlay_primitive_count: 0,
            timing: {},
            model_routes: []
          }
        ]}
      />
    );

    expect(html).toContain('Pipeline Result');
    expect(html).toContain('LKW');
    expect(html).toContain('2');
    expect(html).toContain('Open result history');
    expect(html).not.toContain('Output Connectors');
  });
});

describe('ChannelHttpRequestEndpointsPanel', () => {
  it('builds external service URLs from reverse-proxy base paths', () => {
    expect(
      buildServiceEndpointUrl(
        '/runtime-trigger',
        '/api/v1/runtime/triggers/stream-http-put',
        'https://172.23.27.242'
      )
    ).toBe('https://172.23.27.242/runtime-trigger/api/v1/runtime/triggers/stream-http-put');
    expect(
      buildServiceEndpointUrl(
        '/runtime-results',
        '/api/v1/runtime/channels/chn_live/results/latest',
        'https://172.23.27.242'
      )
    ).toBe('https://172.23.27.242/runtime-results/api/v1/runtime/channels/chn_live/results/latest');
  });

  it('renders separate trigger and live result HTTP request endpoints', () => {
    const html = renderToStaticMarkup(
      <ChannelHttpRequestEndpointsPanel
        endpoints={[
          {
            kind: 'trigger_execution',
            label: 'HTTP Trigger ausloesen',
            description: 'PLC reject decision',
            method: 'PUT',
            path: '/api/v1/runtime/triggers/packing-station-plc',
            available: true,
            auth_required: true,
            auth_label: 'runtime.trigger.invoke',
            trigger_endpoint_id: 'tep_1',
            pipeline_version_id: 'plv_trigger',
            pipeline_label: 'Reject decision v1',
            status: 'active',
            last_activity_at: '2026-05-01T10:00:00Z'
          },
          {
            kind: 'latest_result',
            label: 'Aktuelles Live-Ergebnis abfragen',
            description: 'Latest live pipeline result',
            method: 'GET',
            path: '/api/v1/runtime/channels/chn_live/results/latest',
            available: true,
            auth_required: false,
            auth_label: 'runtime.results.read',
            pipeline_version_id: 'plv_live',
            pipeline_label: 'Live inspection v3',
            status: 'active',
            last_activity_at: '2026-05-01T10:00:02Z'
          }
        ]}
      />
    );

    expect(html).toContain('HTTP Request Endpunkte');
    expect(html).toContain('Reject decision v1');
    expect(html).toContain('/api/v1/runtime/triggers/packing-station-plc');
    expect(html).toContain('/api/v1/runtime/channels/chn_live/results/latest');
    expect(html).toContain('runtime.trigger.invoke');
    expect(html).toContain('Not required');
  });
});
