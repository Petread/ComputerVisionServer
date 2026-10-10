import { describe, expect, it } from 'vitest';
import type {
  RuntimeCameraIntrinsicCalibrationProfile,
  RuntimeImageViewProfile,
  RuntimeLogicalModelRef,
  RuntimeLiveViewState,
  RuntimeModelCapabilityOption,
  RuntimeNodePort,
  RuntimeNodePropertySpec,
  RuntimeNodeSpec,
  RuntimeNodeTypeSummary,
  RuntimePipelineOutputCatalog,
  RuntimePipelineGraph,
  RuntimeStemCurvatureCoordinateFrameDefinition,
  RuntimeStemSurfacePhaseCalibrationObservation
} from '@cv-platform/schemas';
import {
  applyImageViewProfileToGraph,
  buildCompatibleConnections,
  buildLogicalModelRefFromCapability,
  boardCutSiemensSpsMappings,
  canAddNodeToGraph,
  commissioningPreviewTargetNodeKey,
  cropEditorAdjustedBox,
  cropEditorConfigFromBox,
  cropEditorCoordinateSpace,
  cropEditorLiveState,
  defaultModelRef,
  eligibleNodeTypesForGraph,
  formatModelCapabilityOption,
  guidedNodeGroupsForCategory,
  graphToAuthoringGraph,
  inferGraphMode,
  intrinsicCalibrationSourceFrame,
  modelCapabilityMatchesSelectionPolicy,
  normalizeBoxList,
  normalizeClearanceRules,
  normalizeColorProfiles,
  normalizeCvLineSelectionRules,
  normalizeDirectionReference,
  normalizeEdgeRoleMappings,
  normalizeMeasurementRules,
  normalizeMetricApplyTo,
  normalizeCameraIntrinsicCalibrationProfile,
  normalizeImageViewProfile,
  normalizeTrackerRoiList,
  trackerRoiIsWholeView,
  normalizeStemCurvatureProcessCalibrationProfile,
  normalizeStemMinimumPrincipleValidationArtifact,
  normalizeStemSurfacePhaseCalibrationProfile,
  normalizeResultProjections,
  normalizeSiemensSpsMappings,
  normalizeQualityRules,
  normalizeResponseMappings,
  normalizeSpatialRules,
  paletteGroupsForCategory,
  portsCompatible,
  reconcileBoardCutClassRules,
  reconcileBoardCutClassRulesInGraph,
  reconcileBoardSurfaceClassMapping,
  requiredTaskTypeForNode,
  projectProcessPointToImage,
  resolveSelectedModelReleaseId,
  resultFieldOptionsForNode,
  selectedBoardCalibrationChannelId,
  stemSurfacePhaseCalibrationCaptureFromLiveState,
  stemSurfacePhaseCalibrationFitPreview,
  stemCoordinateFrameDefinition,
  stemSpatialCalibrationUnavailableReason,
  stemCoordinateFrameReferencePoints,
  stemProcessGeometryFromCanonicalView,
  stemProcessUndistortedCalibrationFrame,
  suggestedResponseKey,
  triggerablePipelineSummary,
  upstreamIntrinsicCalibrationProfile,
  upstreamImageViewProfile,
  upstreamStemSurfacePhaseCalibrationProfile,
  validationIssueSummary,
  visiblePropertyFields
} from './runtime-pipeline-designer-page';

describe('runtime pipeline designer helpers', () => {
  it('shows the metadata selection only for the configured Edge metadata response mode', () => {
    const fields = ['edge_response_mode', 'edge_response_metadata_fields', 'projections'].map(
      (key) => ({ key } as RuntimeNodePropertySpec)
    );
    const node = {
      node_key: 'result', node_type: 'output.result', node_category: 'output',
      node_contract_version: 1, input_ports: [], output_ports: [], config: {}, position: {}
    } as RuntimeNodeSpec;
    expect(visiblePropertyFields(fields, node).map((field) => field.key)).toEqual([
      'edge_response_mode', 'projections'
    ]);
    node.config.edge_response_mode = 'result_with_metadata';
    expect(visiblePropertyFields(fields, node)).toEqual(fields);
    node.config.edge_response_mode = 'result_only';
    expect(visiblePropertyFields(fields, node).map((field) => field.key)).toEqual([
      'edge_response_mode', 'projections'
    ]);
  });

  it('previews an inconsistent raw-phase angle fit before applying calibration', () => {
    const rawValues = [0, -0.1582, -0.1918, -0.1327];
    const knownAngles = [0, 30, 60, 90];
    const observations = rawValues.map(
      (rawPhase, index): RuntimeStemSurfacePhaseCalibrationObservation => ({
        observation_id: `preview-${index}`,
        raw_phase: rawPhase,
        known_angle_deg: knownAngles[index],
        actual_marking: `mark-${index}`,
        diameter_mm: 350,
        image_center_x_px: 500,
        image_center_y_px: 250,
        weight: 1
      })
    );

    const preview = stemSurfacePhaseCalibrationFitPreview(observations);

    expect(preview?.degrees_per_raw_phase).toBeCloseTo(-355.02, 2);
    expect(preview?.mean_abs_error_deg).toBeCloseTo(19.29, 2);
    expect(preview?.maximum_abs_error_deg).toBeCloseTo(42.89, 2);
    expect(preview?.evaluated[3].calculated_phase_deg).toBeCloseTo(47.11, 2);
    expect(preview?.worst_observation_index).toBe(3);
  });

  it('identifies the largest mismatch in a four-frame commissioning fit', () => {
    const frameNumbers = [6741, 6749, 6765, 6778];
    const rawValues = [0, -0.0472, -0.0671, -0.1048];
    const knownAngles = [0, 45, 90, 135];
    const weights = [1, 0.86, 0.86, 0.88];
    const observations = rawValues.map(
      (rawPhase, index): RuntimeStemSurfacePhaseCalibrationObservation => ({
        observation_id: `frame-${frameNumbers[index]}`,
        raw_phase: rawPhase,
        known_angle_deg: knownAngles[index],
        actual_marking: `Frame ${frameNumbers[index]}`,
        diameter_mm: 350,
        image_center_x_px: 500,
        image_center_y_px: 250,
        weight: weights[index]
      })
    );

    const preview = stemSurfacePhaseCalibrationFitPreview(observations);

    expect(preview?.degrees_per_raw_phase).toBeCloseTo(-1259.97, 2);
    expect(preview?.maximum_abs_error_deg).toBeCloseTo(14.47, 2);
    expect(preview?.worst_observation_index).toBe(1);
    expect(preview?.evaluated[1].calculated_phase_deg).toBeCloseTo(59.47, 2);
  });

  it('recognizes the tracker whole-view template placeholder', () => {
    const [wholeView] = normalizeTrackerRoiList([
      { label: 'placeholder', x: 0, y: 0, width: 1, height: 1 }
    ]);
    const [processRegion] = normalizeTrackerRoiList([
      {
        label: 'transfer',
        points: [
          { x: 0.65, y: 0.1 },
          { x: 1, y: 0 },
          { x: 1, y: 1 },
          { x: 0.65, y: 0.9 }
        ]
      }
    ]);

    expect(trackerRoiIsWholeView(wholeView)).toBe(true);
    expect(trackerRoiIsWholeView(processRegion)).toBe(false);
  });

  it('extracts one synchronized raw-phase calibration frame from the live model view', () => {
    const state = {
      frame: {
        image_base64: 'canonical-jpeg',
        mime_type: 'image/jpeg',
        width: 1024,
        height: 410,
        sequence: 42,
        source_timestamp: '2026-08-11T10:00:00Z',
        metadata: {}
      },
      overlay: [
        {
          primitive_type: 'label',
          layer: 'stem_surface_phase',
          metadata: {
            eligible: true,
            phase_locked: true,
            track_id: 'trk-stem-1',
            frame_sequence: 42,
            timestamp: '2026-08-11T10:00:00Z',
            raw_phase: 0.125,
            delta_raw_phase: 0.025,
            tracking_mode: 'INCREMENTAL',
            trace_generation: 3,
            reference_eligible: true,
            measurement_eligible: true,
            failure_count: 0,
            confidence: 0.91,
            detected_feature_count: 24,
            inlier_ratio: 0.82,
            matched_feature_count: 18,
            diameter_mm: 342,
            image_center_x_px: 710,
            image_center_y_px: 250
          }
        }
      ]
    } as unknown as RuntimeLiveViewState;

    expect(stemSurfacePhaseCalibrationCaptureFromLiveState(state)).toMatchObject({
      track_id: 'trk-stem-1',
      frame_sequence: 42,
      raw_phase: 0.125,
      delta_raw_phase: 0.025,
      tracking_mode: 'INCREMENTAL',
      trace_generation: 3,
      reference_eligible: true,
      measurement_eligible: true,
      detected_feature_count: 24,
      confidence: 0.91,
      diameter_mm: 342,
      image_center_x_px: 710,
      image_center_y_px: 250
    });

    const mismatched = {
      ...state,
      frame: { ...state.frame, sequence: 43 }
    } as unknown as RuntimeLiveViewState;
    expect(stemSurfacePhaseCalibrationCaptureFromLiveState(mismatched)).toBeNull();
  });

  it('derives understandable O/X/Y/XY coordinates and keeps Z out of the homography points', () => {
    const draft = {
      origin_image_point: { x_px: 10, y_px: 20 },
      x_axis_image_endpoint: { x_px: 510, y_px: 30 },
      y_axis_image_endpoint: { x_px: 20, y_px: 320 },
      xy_corner_image_point: { x_px: 500, y_px: 330 },
      z_axis_image_endpoint: { x_px: 80, y_px: 5 }
    };

    const references = stemCoordinateFrameReferencePoints(draft, 2000, 600);
    const definition = stemCoordinateFrameDefinition(
      draft,
      2000,
      600,
      'UP_FROM_REFERENCE_PLANE',
      250
    );

    expect(references.map((point) => [point.point_id, point.process_point])).toEqual([
      ['axis-origin', { x_mm: 0, y_mm: 0 }],
      ['axis-x-end', { x_mm: 2000, y_mm: 0 }],
      ['axis-y-end', { x_mm: 0, y_mm: 600 }],
      ['axis-xy-corner', { x_mm: 2000, y_mm: 600 }]
    ]);
    expect(references).toHaveLength(4);
    expect(definition?.z_axis_image_endpoint).toEqual(draft.z_axis_image_endpoint);
    expect(definition?.z_reference_length_mm).toBe(250);
    const measured = {
      status: 'valid', calibration_method: 'CHECKERBOARD_OR_CHARUCO',
      accepted_observation_count: 8
    } as RuntimeCameraIntrinsicCalibrationProfile;
    expect(stemSpatialCalibrationUnavailableReason(measured, definition)).toBeNull();
    expect(stemSpatialCalibrationUnavailableReason(null, definition)).toContain('camera');
    expect(stemSpatialCalibrationUnavailableReason({
      ...measured, calibration_method: 'IDENTITY_PASSTHROUGH'
    }, definition)).toContain('passthrough');
    expect(stemSpatialCalibrationUnavailableReason(measured, {
      ...definition!, z_reference_length_mm: null
    })).toContain('measured vertical distance');
  });

  it('reuses the canonical view as the complete process measurement corridor', () => {
    const viewProfile = {
      source_image_width: 1001,
      source_image_height: 501,
      source_quadrilateral: [
        { x: 0.1, y: 0.2 },
        { x: 0.9, y: 0.1 },
        { x: 0.85, y: 0.9 },
        { x: 0.15, y: 0.8 }
      ]
    } as unknown as RuntimeImageViewProfile;

    const positive = stemProcessGeometryFromCanonicalView(viewProfile, 'POSITIVE_X');
    const negative = stemProcessGeometryFromCanonicalView(viewProfile, 'NEGATIVE_X');

    expect(positive?.polygons.candidate_roi).toEqual(
      positive?.polygons.curvature_observation_roi
    );
    expect(positive?.polygons.surface_tracking_roi).toEqual(
      positive?.polygons.curvature_observation_roi
    );
    expect(positive?.gates.session_start_gate[0]).toEqual({ x_px: 150, y_px: 400 });
    expect(positive?.gates.decision_gate[0]).toEqual({ x_px: 900, y_px: 50 });
    expect(positive?.gates.curvature_freeze_gate).toEqual(positive?.gates.decision_gate);
    expect(positive?.decisionMarginMm).toBe(0);
    expect(negative?.gates.session_start_gate).toEqual(positive?.gates.decision_gate);
    expect(negative?.gates.decision_gate).toEqual(positive?.gates.session_start_gate);
  });

  it('uses the right edge for transverse entry and positive X for the red exit edge', () => {
    const viewProfile = {
      source_image_width: 1001,
      source_image_height: 601,
      source_quadrilateral: [
        { x: 608 / 1000, y: 8 / 600 },
        { x: 943 / 1000, y: 74 / 600 },
        { x: 522 / 1000, y: 551 / 600 },
        { x: 97 / 1000, y: 357 / 600 }
      ]
    } as unknown as RuntimeImageViewProfile;
    const coordinateFrame = {
      origin_image_point: { x_px: 263, y_px: 375 },
      x_axis_image_endpoint: { x_px: 744, y_px: 128 }
    } as RuntimeStemCurvatureCoordinateFrameDefinition;

    const geometry = stemProcessGeometryFromCanonicalView(
      viewProfile,
      'POSITIVE_X',
      coordinateFrame
    );

    expect(geometry?.gates.session_start_gate).toEqual([
      { x_px: 943, y_px: 74 },
      { x_px: 522, y_px: 551 }
    ]);
    expect(geometry?.gates.decision_gate).toEqual([
      { x_px: 608, y_px: 8 },
      { x_px: 943, y_px: 74 }
    ]);
  });

  it('uses the original pipeline input as the crop editor background', () => {
    const liveState = {
      display_context_id: 'ctx_crop_1',
      display_context: { kind: 'crop' },
      frame: { width: 320, height: 200, image_base64: 'cropped' },
      source_frame: {
        width: 1280,
        height: 720,
        image_base64: 'original',
        image_context: { context_id: 'ctx_original_1', kind: 'original' }
      }
    } as unknown as RuntimeLiveViewState;

    const editorState = cropEditorLiveState(liveState);

    expect(editorState?.frame).toBe(liveState.source_frame);
    expect(editorState?.display_context_id).toBe('ctx_original_1');
    expect(cropEditorCoordinateSpace(editorState)).toEqual({
      x: 0,
      y: 0,
      width: 1280,
      height: 720,
      sourceWidth: 1280,
      sourceHeight: 720
    });
    expect(
      cropEditorConfigFromBox(editorState, {
        x: 0.1,
        y: 0.2,
        width: 0.5,
        height: 0.25
      })
    ).toEqual({ x: 128, y: 144, width: 640, height: 180 });
  });

  it('adds the displayed crop origin when replacing a crop selection', () => {
    const liveState = {
      frame: {
        width: 320,
        height: 200,
        image_context: {
          context_id: 'ctx_crop_1',
          kind: 'crop',
          transform: {
            transform_type: 'crop',
            coordinate_projection: 'crop_offset',
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
      }
    } as unknown as RuntimeLiveViewState;

    expect(cropEditorCoordinateSpace(liveState)).toEqual({
      x: 137,
      y: 83,
      width: 320,
      height: 200,
      sourceWidth: 1280,
      sourceHeight: 720
    });
    expect(
      cropEditorConfigFromBox(liveState, {
        x: 0.25,
        y: 0.25,
        width: 0.5,
        height: 0.5
      })
    ).toEqual({
      x: 217,
      y: 133,
      width: 160,
      height: 100
    });
  });

  it('keeps crop moves and resize operations inside the displayed frame', () => {
    const crop = { x: 0.2, y: 0.3, width: 0.4, height: 0.5, angle_degrees: 0 };

    expect(cropEditorAdjustedBox(crop, { x: 0.5, y: 0.5 }, { x: 1, y: 1 }, 'move')).toMatchObject({
      x: 0.6,
      y: 0.5,
      width: 0.4,
      height: 0.5
    });
    const resized = cropEditorAdjustedBox(crop, { x: 0.6, y: 0.8 }, { x: 1, y: 1 }, 'resize');
    expect(resized).toMatchObject({
      x: 0.2,
      y: 0.3
    });
    expect(resized.width).toBeCloseTo(0.8, 12);
    expect(resized.height).toBeCloseTo(0.7, 12);
  });

  it('maps node types to runtime task types', () => {
    expect(requiredTaskTypeForNode('inference.classification')).toBe('classification');
    expect(requiredTaskTypeForNode('inference.segmentation')).toBe('segmentation');
    expect(requiredTaskTypeForNode('inference.detection')).toBe('detection');
  });

  it('builds logical model refs from picker selections', () => {
    const capability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_123',
      model_family_slug: 'defect-detector',
      display_name: 'Defect Detector',
      target_path: 'realtime',
      task_type: 'detection',
      model_version_id: 'mvr_123',
      version_label: '2026.05',
      deployment_status: 'production',
      architecture_family: 'yolo',
      architecture_version: 'v11',
      architecture_size: 'm',
      architecture_display_name: 'YOLOv11 Medium',
      contract_major: 1,
      active: true,
      capability: {}
    };

    expect(buildLogicalModelRefFromCapability(capability, 'detection', 'active_production')).toEqual({
      model_family_slug: 'defect-detector',
      target_path: 'realtime',
      contract_major: 1,
      selection_policy: 'active_production',
      required_task_type: 'detection',
      deployment_release_id: null
    });
    expect(buildLogicalModelRefFromCapability(capability, 'detection', 'explicit_release')).toEqual({
      model_family_slug: 'defect-detector',
      target_path: 'realtime',
      contract_major: 1,
      selection_policy: 'explicit_release',
      required_task_type: 'detection',
      deployment_release_id: 'rel_123'
    });
    expect(formatModelCapabilityOption(capability)).toBe(
      'Defect Detector | 2026.05 | YOLOv11 Medium | production'
    );
    const projectCapability = {
      ...capability,
      capability: {
        label_project_id: 'project-a',
        label_project_title: 'Project A',
        project_active_production: true
      }
    };
    const projectRef = buildLogicalModelRefFromCapability(
      projectCapability, 'detection', 'active_production'
    );
    expect(projectRef.label_project_id).toBe('project-a');
    expect(formatModelCapabilityOption(projectCapability)).toBe(
      'Project A / Defect Detector | 2026.05 | YOLOv11 Medium | production'
    );
    expect(resolveSelectedModelReleaseId(projectRef, [
      { ...projectCapability, deployment_release_id: 'other-project', capability: {
        label_project_id: 'project-b', project_active_production: true
      } },
      { ...projectCapability, deployment_release_id: 'replacement', model_family_slug: 'other-model' }
    ])).toBe('replacement');
    expect(modelCapabilityMatchesSelectionPolicy({
      ...projectCapability,
      capability: { ...projectCapability.capability, project_active_production: false }
    }, 'active_production')).toBe(false);
    expect(modelCapabilityMatchesSelectionPolicy({
      ...projectCapability,
      capability: { ...projectCapability.capability, project_active_production: false }
    }, 'explicit_release')).toBe(true);
  });

  it('generates immutable board-cut rules from deployed class labels without text input', () => {
    const capability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_board',
      model_family_slug: 'board-features',
      display_name: 'Board features',
      target_path: 'realtime',
      task_type: 'detection',
      model_version_id: 'mvr_board',
      version_label: 'v4',
      deployment_status: 'production',
      contract_major: 1,
      active: true,
      capability: {
        class_labels: ['knot', 'crack'],
        class_map_version: 'snapshot-4:abc'
      }
    };

    const rows = reconcileBoardCutClassRules([], capability);
    expect(rows).toMatchObject([
      { class_id: 0, label_snapshot: 'knot', confirmed: false, relevant: false },
      { class_id: 1, label_snapshot: 'crack', confirmed: false, relevant: false }
    ]);
    expect(rows.every((row) => row.class_map_version === 'snapshot-4:abc')).toBe(true);

    const changed = reconcileBoardCutClassRules(
      [{ ...rows[0], confirmed: true, relevant: true }],
      { ...capability, capability: { ...capability.capability, class_map_version: 'snapshot-5:def' } }
    );
    expect(changed[0].confirmed).toBe(false);
  });

  it('remaps confirmed board-cut rules by label for an explicit model class subset', () => {
    const capability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_subset',
      model_family_slug: 'board-defect-subset',
      display_name: 'Board defect subset',
      target_path: 'realtime',
      task_type: 'segmentation',
      model_version_id: 'mvr_subset',
      version_label: 'v1',
      deployment_status: 'production',
      contract_major: 1,
      active: true,
      capability: {
        class_labels: ['Ast', 'Harzgalle', 'Riss'],
        configured_class_names: ['Ast', 'Harzgalle', 'Riss'],
        uses_all_project_classes: false,
        class_map_version: 'subset-map'
      }
    };
    const oldRules = [
      {
        class_id: 0,
        label_snapshot: 'Ast',
        class_map_version: 'full-map',
        confirmed: true,
        relevant: true,
        measurement_basis: 'max_feret_diameter',
        safety_profile: 'custom',
        clearance_multiplier: 5,
        depth_factor: 1,
        clearance_factor: 1,
        minimum_confidence: 0.14
      },
      {
        class_id: 1,
        label_snapshot: 'Bläue',
        class_map_version: 'full-map',
        confirmed: true,
        relevant: false,
        measurement_basis: 'max_feret_diameter',
        safety_profile: 'standard',
        clearance_multiplier: 1,
        depth_factor: 1,
        clearance_factor: 1,
        minimum_confidence: 0.35
      },
      {
        class_id: 3,
        label_snapshot: 'Harzgalle',
        class_map_version: 'full-map',
        confirmed: true,
        relevant: true,
        measurement_basis: 'longitudinal_extent',
        safety_profile: 'conservative',
        clearance_multiplier: 2,
        depth_factor: 1,
        clearance_factor: 1,
        minimum_confidence: 0.2
      },
      {
        class_id: 4,
        label_snapshot: 'Riss',
        class_map_version: 'full-map',
        confirmed: true,
        relevant: true,
        measurement_basis: 'transverse_extent',
        safety_profile: 'standard',
        clearance_multiplier: 3,
        depth_factor: 1,
        clearance_factor: 1,
        minimum_confidence: 0.25
      }
    ];

    const rows = reconcileBoardCutClassRules(oldRules, capability);

    expect(rows.map((row) => [row.class_id, row.label_snapshot])).toEqual([
      [0, 'Ast'],
      [1, 'Harzgalle'],
      [2, 'Riss']
    ]);
    expect(rows.every((row) => row.confirmed && row.class_map_version === 'subset-map')).toBe(true);
    expect(rows[1]).toMatchObject({
      relevant: true,
      measurement_basis: 'longitudinal_extent',
      safety_profile: 'conservative',
      clearance_multiplier: 2
    });
  });

  it('reconciles subset class rules for the complete graph before saving', () => {
    const capability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_subset',
      model_family_slug: 'board-defect-subset',
      display_name: 'Board defect subset',
      target_path: 'realtime',
      task_type: 'segmentation',
      model_version_id: 'mvr_subset',
      version_label: 'v1',
      deployment_status: 'production',
      contract_major: 1,
      active: true,
      capability: {
        class_labels: ['Ast', 'Riss'],
        configured_class_names: ['Ast', 'Riss'],
        uses_all_project_classes: false,
        class_map_version: 'subset-map'
      }
    };
    const graph = {
      nodes: [
        {
          node_key: 'segmentation',
          node_type: 'inference.segmentation',
          node_category: 'inference',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {},
          logical_model_ref: {
            model_family_slug: 'board-defect-subset',
            target_path: 'realtime',
            contract_major: 1,
            selection_policy: 'active_production',
            required_task_type: 'segmentation',
            deployment_release_id: null
          },
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'constraints',
          node_type: 'analysis.board_view_cut_constraints',
          node_category: 'analysis',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {
            class_rules: [
              {
                class_id: 0,
                label_snapshot: 'Ast',
                class_map_version: 'full-map',
                confirmed: true,
                relevant: true,
                measurement_basis: 'max_feret_diameter',
                safety_profile: 'standard',
                clearance_multiplier: 1,
                depth_factor: 1,
                clearance_factor: 1,
                minimum_confidence: 0.35
              },
              {
                class_id: 4,
                label_snapshot: 'Riss',
                class_map_version: 'full-map',
                confirmed: true,
                relevant: true,
                measurement_basis: 'transverse_extent',
                safety_profile: 'custom',
                clearance_multiplier: 3,
                depth_factor: 1,
                clearance_factor: 1,
                minimum_confidence: 0.2
              }
            ]
          },
          logical_model_ref: null,
          position: { x: 200, y: 0 }
        }
      ],
      edges: [
        {
          from_node_key: 'segmentation',
          from_port: 'segmentation',
          to_node_key: 'constraints',
          to_port: 'features'
        }
      ]
    } as RuntimePipelineGraph;

    const reconciled = reconcileBoardCutClassRulesInGraph(graph, [capability]);
    const rules = reconciled.nodes[1].config.class_rules as Array<Record<string, unknown>>;

    expect(rules.map((rule) => [rule.class_id, rule.label_snapshot])).toEqual([
      [0, 'Ast'],
      [1, 'Riss']
    ]);
    expect(rules[1]).toMatchObject({
      confirmed: true,
      clearance_multiplier: 3,
      class_map_version: 'subset-map'
    });
  });

  it('keeps board-surface role selections only for the selected segmentation release', () => {
    const capability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_surface_1',
      model_family_slug: 'board-surfaces',
      display_name: 'Board surfaces',
      target_path: 'realtime',
      task_type: 'segmentation',
      model_version_id: 'mvr_surface_1',
      version_label: 'v1',
      deployment_status: 'production',
      contract_major: 1,
      active: true,
      capability: {
        class_labels: ['top-custom', 'side-custom', 'end-custom'],
        class_map_version: 'surface-map-1'
      }
    };
    const selected = {
      board_main_face: {
        class_id: 0,
        label_snapshot: 'top-custom',
        class_map_version: 'surface-map-1',
        deployment_release_id: 'rel_surface_1',
        confirmed: true
      },
      board_side_face: {
        class_id: 1,
        label_snapshot: 'side-custom',
        class_map_version: 'surface-map-1',
        deployment_release_id: 'rel_surface_1',
        confirmed: true
      }
    };

    expect(
      reconcileBoardSurfaceClassMapping(
        selected,
        ['board_main_face', 'board_side_face'],
        capability
      )
    ).toEqual(selected);
    const changed = reconcileBoardSurfaceClassMapping(
      selected,
      ['board_main_face', 'board_side_face'],
      {
        ...capability,
        deployment_release_id: 'rel_surface_2',
        capability: { ...capability.capability, class_map_version: 'surface-map-2' }
      }
    );
    expect(changed.board_main_face?.confirmed).toBe(false);
    expect(changed.board_side_face?.confirmed).toBe(false);
  });

  it('builds the board-cut PLC preset with valid-gated unrounded millimetres', () => {
    const mappings = boardCutSiemensSpsMappings();
    expect(mappings.map((mapping) => mapping.source.field)).toEqual([
      'valid',
      'longitudinal_shift_mm'
    ]);
    expect(mappings[1].condition).toMatchObject({
      mode: 'when_field',
      field: 'valid',
      operator: 'truthy'
    });
    expect(mappings[1].conversion.rounding).toBe('none');
  });

  it('uses the configured single test camera for graphical board calibration', () => {
    const graph: RuntimePipelineGraph = {
      nodes: [
        {
          node_key: 'test_camera',
          node_type: 'source.single_board_camera_test',
          node_category: 'source',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { camera_channel_id: 'channel-lower', camera_role: 'lower' },
          logical_model_ref: null,
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'constraints',
          node_type: 'analysis.board_view_cut_constraints',
          node_category: 'analysis',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { view_role: 'lower' },
          logical_model_ref: null,
          position: { x: 1, y: 0 }
        }
      ],
      edges: []
    };

    expect(selectedBoardCalibrationChannelId(graph, 'constraints')).toBe('channel-lower');
    graph.nodes[1].config.view_role = 'upper';
    expect(selectedBoardCalibrationChannelId(graph, 'constraints')).toBeNull();

    graph.nodes[0].node_type = 'source.channel_frame';
    graph.nodes[0].config = { authoring_channel_id: 'channel-preview' };
    expect(selectedBoardCalibrationChannelId(graph, 'constraints')).toBe('channel-preview');
  });

  it('binds intrinsic calibration to the channel frame source and rejects malformed profiles', () => {
    const graph: RuntimePipelineGraph = {
      nodes: [
        {
          node_key: 'source',
          node_type: 'source.channel_frame',
          node_category: 'source',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { authoring_channel_id: 'channel-camera' },
          logical_model_ref: null,
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'undistort',
          node_type: 'preprocessing.camera_undistort',
          node_category: 'preprocessing',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {},
          logical_model_ref: null,
          position: { x: 1, y: 0 }
        }
      ],
      edges: []
    };
    const profile = {
      schema_version: 'camera.intrinsic-calibration.v1',
      status: 'valid',
      source_channel_id: 'channel-camera',
      image_width: 640,
      image_height: 480,
      distortion_model: 'opencv_standard',
      camera_matrix: [500, 0, 320, 0, 500, 240, 0, 0, 1],
      output_camera_matrix: [500, 0, 320, 0, 500, 240, 0, 0, 1],
      distortion_coefficients: [-0.2, 0.03, 0, 0, 0],
      rms_reprojection_error_px: 0.4,
      per_observation_reprojection_error_px: {},
      accepted_observation_count: 7,
      board_definition: {
        board_type: 'checkerboard',
        columns: 9,
        rows: 6,
        square_length: 1,
        marker_length: null,
        dictionary_name: 'DICT_4X4_50'
      },
      valid_roi: { x: 0, y: 0, width: 640, height: 480 },
      generated_at: '2026-08-03T10:00:00Z',
      calibration_hash: 'a'.repeat(64),
      perspective_rectified: false
    };

    expect(selectedBoardCalibrationChannelId(graph, 'undistort')).toBe('channel-camera');
    expect(normalizeCameraIntrinsicCalibrationProfile(profile)).toMatchObject({
      source_channel_id: 'channel-camera',
      status: 'valid'
    });
    expect(normalizeCameraIntrinsicCalibrationProfile({ ...profile, camera_matrix: [1, 2] })).toBeNull();
  });

  it('uses only a full-resolution source image for intrinsic live calibration', () => {
    const sourceFrame = {
      image_base64: 'c291cmNl',
      mime_type: 'image/jpeg',
      width: 1920,
      height: 1080,
      sequence: 8,
      image_context: { kind: 'original' }
    };
    const canonicalFrame = {
      ...sourceFrame,
      image_base64: 'Y2Fub25pY2Fs',
      width: 1600,
      height: 640,
      image_context: { kind: 'canonical_view' }
    };

    expect(
      intrinsicCalibrationSourceFrame({
        source_frame: sourceFrame,
        frame: canonicalFrame
      } as unknown as RuntimeLiveViewState)
    ).toBe(sourceFrame);
    expect(
      intrinsicCalibrationSourceFrame({
        frame: canonicalFrame,
        display_context: { kind: 'canonical_view' }
      } as unknown as RuntimeLiveViewState)
    ).toBeNull();
    expect(
      intrinsicCalibrationSourceFrame({
        frame: sourceFrame,
        display_context: { purpose: 'runtime_calibration' }
      } as unknown as RuntimeLiveViewState)
    ).toBe(sourceFrame);
  });

  it('edits process calibration only on the matching undistorted frame and round-trips axes', () => {
    const intrinsic = {
      schema_version: 'camera.intrinsic-calibration.v1' as const,
      status: 'valid' as const,
      source_channel_id: 'channel-camera',
      image_width: 640,
      image_height: 480,
      distortion_model: 'opencv_standard' as const,
      camera_matrix: [500, 0, 320, 0, 500, 240, 0, 0, 1],
      output_camera_matrix: [500, 0, 320, 0, 500, 240, 0, 0, 1],
      distortion_coefficients: [-0.2, 0.03, 0, 0, 0],
      rms_reprojection_error_px: 0.4,
      per_observation_reprojection_error_px: {},
      accepted_observation_count: 7,
      board_definition: {
        board_type: 'checkerboard' as const,
        columns: 9,
        rows: 6,
        square_length: 1,
        marker_length: null,
        dictionary_name: 'DICT_4X4_50'
      },
      valid_roi: { x: 0, y: 0, width: 640, height: 480 },
      generated_at: '2026-08-04T10:00:00Z',
      calibration_hash: 'a'.repeat(64),
      perspective_rectified: false as const
    };
    const graph: RuntimePipelineGraph = {
      nodes: [
        {
          node_key: 'source',
          node_type: 'source.channel_frame',
          node_category: 'source',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { authoring_channel_id: 'channel-camera' },
          logical_model_ref: null,
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'undistort',
          node_type: 'preprocessing.camera_undistort',
          node_category: 'preprocessing',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { calibration: intrinsic },
          logical_model_ref: null,
          position: { x: 1, y: 0 }
        },
        {
          node_key: 'process',
          node_type: 'preprocessing.process_coordinate_rectify',
          node_category: 'preprocessing',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {},
          logical_model_ref: null,
          position: { x: 2, y: 0 }
        }
      ],
      edges: [
        { from_node_key: 'source', from_port: 'frame', to_node_key: 'undistort', to_port: 'frame' },
        { from_node_key: 'undistort', from_port: 'frame', to_node_key: 'process', to_port: 'frame' }
      ]
    };
    const liveState = {
      frame: {
        image_base64: 'aW1hZ2U=',
        mime_type: 'image/png',
        width: 640,
        height: 480,
        sequence: 1,
        image_context: {
          transform: {
            transform_type: 'undistort',
            parameters: { calibration_hash: intrinsic.calibration_hash }
          }
        },
        metadata: { calibration_hash: intrinsic.calibration_hash, perspective_rectified: false }
      }
    } as unknown as RuntimeLiveViewState;
    const point = { x_px: 0, y_px: 0 };
    const processProfile = {
      schema_version: 'stem.curvature-process-calibration.v1',
      calibration_id: 'stem-process-v1',
      status: 'valid',
      source_channel_id: 'channel-camera',
      image_width: 640,
      image_height: 480,
      intrinsic_calibration_hash: intrinsic.calibration_hash,
      coordinate_unit: 'mm',
      reference_points: [0, 1, 2, 3].map((index) => ({
        point_id: `p-${index}`,
        image_point: point,
        process_point: { x_mm: index, y_mm: index }
      })),
      homography_image_to_process: [1, 0, 0, 0, 1, 0, 0, 0, 1],
      homography_process_to_image: [2, 0, 10, 0, 3, 20, 0, 0, 1],
      process_bounds: { x_min_mm: 0, x_max_mm: 100, y_min_mm: 0, y_max_mm: 50 },
      reference_plane: {
        plane_id: 'plane',
        z_mm: 0,
        z_positive_direction: 'TOWARD_CAMERA',
        x_axis_description: 'X conveyor',
        y_axis_description: 'Y transverse',
        z_axis_description: 'Z toward camera',
        description: ''
      },
      candidate_roi: { image_points: [point, point, point], process_points: [{ x_mm: 0, y_mm: 0 }, { x_mm: 1, y_mm: 0 }, { x_mm: 0, y_mm: 1 }] },
      curvature_observation_roi: { image_points: [point, point, point], process_points: [{ x_mm: 0, y_mm: 0 }, { x_mm: 1, y_mm: 0 }, { x_mm: 0, y_mm: 1 }] },
      surface_tracking_roi: { image_points: [point, point, point], process_points: [{ x_mm: 0, y_mm: 0 }, { x_mm: 1, y_mm: 0 }, { x_mm: 0, y_mm: 1 }] },
      session_start_gate: { image_start: point, image_end: point, process_start: { x_mm: 0, y_mm: 0 }, process_end: { x_mm: 0, y_mm: 1 } },
      curvature_freeze_gate: { image_start: point, image_end: point, process_start: { x_mm: 1, y_mm: 0 }, process_end: { x_mm: 1, y_mm: 1 } },
      decision_gate: { image_start: point, image_end: point, process_start: { x_mm: 2, y_mm: 0 }, process_end: { x_mm: 2, y_mm: 1 } },
      decision_margin_mm: 100,
      conveyor_direction: 'POSITIVE_X',
      rotation_reference_view: 'LOOKING_IN_CONVEYOR_DIRECTION',
      positive_rotation: 'CLOCKWISE',
      rms_reprojection_error_px: 0.1,
      maximum_reprojection_error_px: 0.2,
      maximum_allowed_reprojection_error_px: 2,
      provenance: { method: 'MANUAL_REFERENCE_POINT_HOMOGRAPHY', created_at: '2026-08-04T10:00:00Z', notes: '' },
      calibration_hash: 'b'.repeat(64),
      perspective_rectified: true
    };

    expect(upstreamIntrinsicCalibrationProfile(graph, 'process')?.calibration_hash).toBe(intrinsic.calibration_hash);
    expect(selectedBoardCalibrationChannelId(graph, 'process')).toBe('channel-camera');
    expect(commissioningPreviewTargetNodeKey(graph, 'process')).toBe('undistort');
    expect(commissioningPreviewTargetNodeKey(graph, 'undistort')).toBeNull();
    expect(stemProcessUndistortedCalibrationFrame(liveState, intrinsic)).toBe(liveState.frame);
    expect(stemProcessUndistortedCalibrationFrame({ ...liveState, frame: { ...liveState.frame!, metadata: { perspective_rectified: true } } }, intrinsic)).toBeNull();
    const canonicalPreviewState = {
      ...liveState,
      frame: {
        ...liveState.frame!,
        width: 1600,
        height: 640,
        image_context: {
          transform: { transform_type: 'masked_crop', parameters: {} }
        },
        metadata: { perspective_rectified: false, preserves_source_perspective: true }
      },
      undistorted_frame: liveState.frame
    } as unknown as RuntimeLiveViewState;
    expect(stemProcessUndistortedCalibrationFrame(canonicalPreviewState, intrinsic)).toBe(
      liveState.frame
    );
    const rawPassthroughState = {
      frame: {
        image_base64: 'cmF3LWltYWdl',
        mime_type: 'image/png',
        width: 640,
        height: 480,
        sequence: 2,
        image_context: { kind: 'original' },
        metadata: {}
      }
    } as unknown as RuntimeLiveViewState;
    const passthroughIntrinsic = {
      ...intrinsic,
      calibration_method: 'IDENTITY_PASSTHROUGH' as const
    };
    expect(
      stemProcessUndistortedCalibrationFrame(rawPassthroughState, passthroughIntrinsic)
    ).toBe(rawPassthroughState.frame);
    expect(stemProcessUndistortedCalibrationFrame(rawPassthroughState, intrinsic)).toBeNull();
    expect(normalizeStemCurvatureProcessCalibrationProfile(processProfile)?.calibration_id).toBe('stem-process-v1');
    expect(normalizeStemCurvatureProcessCalibrationProfile({
      ...processProfile, spatial_calibration: { camera_center_mm: 'invalid' }
    })).toBeNull();
    expect(
      normalizeStemSurfacePhaseCalibrationProfile({
        schema_version: 'stem.surface-phase-calibration.v1',
        calibration_id: 'phase-v1',
        calibration_version: '1',
        status: 'valid',
        source_channel_id: 'channel-camera',
        image_width: 640,
        image_height: 480,
        intrinsic_calibration_hash: intrinsic.calibration_hash,
        process_calibration_hash: processProfile.calibration_hash,
        rotation_reference_view: 'LOOKING_IN_CONVEYOR_DIRECTION',
        positive_rotation: 'CLOCKWISE',
        fit_model: 'LINEAR_THROUGH_ORIGIN',
        degrees_per_raw_phase: 360,
        observations: [0, 1, 2, 3].map((index) => ({
          observation_id: `phase-${index}`,
          raw_phase: index - 1.5,
          known_angle_deg: (index - 1.5) * 360,
          diameter_mm: 300,
          image_center_x_px: 320,
          image_center_y_px: 240,
          weight: 1
        })),
        accepted_observation_count: 4,
        mean_abs_error_deg: 0,
        rms_error_deg: 0,
        maximum_abs_error_deg: 0,
        maximum_mean_abs_error_deg: 3,
        maximum_abs_error_limit_deg: 7.5,
        valid_diameter_min_mm: 300,
        valid_diameter_max_mm: 300,
        valid_image_center_x_min_px: 320,
        valid_image_center_x_max_px: 320,
        valid_image_center_y_min_px: 240,
        valid_image_center_y_max_px: 240,
        provenance: {
          method: 'MARKED_TEST_STEM',
          created_at: '2026-08-04T12:00:00Z',
          notes: ''
        },
        calibration_hash: 'c'.repeat(64)
      })?.calibration_id
    ).toBe('phase-v1');
    expect(projectProcessPointToImage(processProfile.homography_process_to_image, 5, 7)).toEqual({ x_px: 20, y_px: 41 });
  });

  it('summarizes concrete validation blockers with their node and field', () => {
    expect(
      validationIssueSummary({
        status: 'invalid',
        checked_at: '2026-08-06T00:00:00Z',
        error_count: 2,
        warning_count: 0,
        issues: [
          {
            severity: 'error',
            code: 'intrinsic_required',
            message: 'Intrinsic camera calibration is required.',
            node_key: 'undistort',
            field: 'calibration',
            metadata: {}
          },
          {
            severity: 'error',
            code: 'view_required',
            message: 'Canonical image view is required.',
            node_key: 'rectified_view',
            field: 'view_profile',
            metadata: {}
          }
        ]
      })
    ).toContain('undistort.calibration: Intrinsic camera calibration is required.');
  });

  it('normalizes and resolves one versioned canonical image view upstream', () => {
    const profile = {
      schema_version: 'image.view-profile.v1' as const,
      view_profile_id: 'stem-measurement-view-v1',
      status: 'valid' as const,
      source_space: 'UNDISTORTED_IMAGE' as const,
      source_channel_id: 'channel-camera',
      source_image_width: 1920,
      source_image_height: 1080,
      intrinsic_calibration_hash: 'a'.repeat(64),
      source_quadrilateral: [
        { x: 0.1, y: 0.15 },
        { x: 0.76, y: 0.08 },
        { x: 0.88, y: 0.84 },
        { x: 0.16, y: 0.96 }
      ],
      output_width: 1600,
      output_height: 640,
      transform_type: 'POLYGON_MASKED_CROP' as const,
      interpolation: 'LINEAR' as const,
      padding_mode: 'CONSTANT' as const,
      padding_value: 0,
      enabled: true,
      source_to_view_homography: [1, 0, 0, 0, 1, 0, 0, 0, 1],
      view_to_source_homography: [1, 0, 0, 0, 1, 0, 0, 0, 1],
      profile_checksum: 'c'.repeat(64),
      validation_status: 'VALID' as const,
      validated_at: '2026-08-05T10:00:00Z',
      provenance: {
        method: 'MANUAL_QUADRILATERAL' as const,
        created_at: '2026-08-05T10:00:00Z',
        notes: ''
      },
      metadata: {}
    };
    const graph = {
      nodes: [
        {
          node_key: 'view',
          node_type: 'preprocessing.process_coordinate_rectify',
          node_category: 'preprocessing',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { view_profile: profile },
          logical_model_ref: null,
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'segment',
          node_type: 'inference.segmentation',
          node_category: 'inference',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {},
          logical_model_ref: null,
          position: { x: 1, y: 0 }
        },
        {
          node_key: 'eligibility',
          node_type: 'analysis.stem_measurement_eligibility',
          node_category: 'analysis',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {},
          logical_model_ref: null,
          position: { x: 2, y: 0 }
        }
      ],
      edges: [
        { from_node_key: 'view', from_port: 'frame', to_node_key: 'segment', to_port: 'frame' },
        { from_node_key: 'view', from_port: 'frame', to_node_key: 'eligibility', to_port: 'frame' },
        { from_node_key: 'segment', from_port: 'segments', to_node_key: 'eligibility', to_port: 'segments' }
      ]
    } as RuntimePipelineGraph;

    expect(normalizeImageViewProfile(profile)?.output_width).toBe(1600);
    expect(normalizeImageViewProfile({ ...profile, profile_checksum: 'bad' })).toBeNull();
    expect(upstreamImageViewProfile(graph, 'segment')?.profile_checksum).toBe('c'.repeat(64));
    expect(commissioningPreviewTargetNodeKey(graph, 'view')).toBe('view');
    expect(commissioningPreviewTargetNodeKey(graph, 'eligibility')).toBe('view');
  });

  it('applies a canonical view profile ID atomically to its node and downstream consumers', () => {
    const profileV1 = {
      schema_version: 'image.view-profile.v1' as const,
      view_profile_id: 'stem-measurement-view-v1',
      status: 'valid' as const,
      source_space: 'UNDISTORTED_IMAGE' as const,
      source_channel_id: 'channel-camera',
      source_image_width: 1280,
      source_image_height: 720,
      intrinsic_calibration_hash: 'a'.repeat(64),
      source_quadrilateral: [
        { x: 0.1, y: 0.1 },
        { x: 0.9, y: 0.1 },
        { x: 0.9, y: 0.9 },
        { x: 0.1, y: 0.9 }
      ],
      output_width: 960,
      output_height: 540,
      transform_type: 'POLYGON_MASKED_CROP' as const,
      interpolation: 'LINEAR' as const,
      padding_mode: 'CONSTANT' as const,
      padding_value: 0,
      enabled: true,
      source_to_view_homography: [1, 0, 0, 0, 1, 0, 0, 0, 1],
      view_to_source_homography: [1, 0, 0, 0, 1, 0, 0, 0, 1],
      profile_checksum: 'b'.repeat(64),
      validation_status: 'VALID' as const,
      validated_at: '2026-08-17T10:00:00Z',
      provenance: {
        method: 'MANUAL_QUADRILATERAL' as const,
        created_at: '2026-08-17T10:00:00Z',
        notes: ''
      },
      metadata: {}
    };
    const profileV2: RuntimeImageViewProfile = {
      ...profileV1,
      view_profile_id: 'stem-measurement-view-v2',
      profile_checksum: 'c'.repeat(64)
    };
    const graph = {
      nodes: [
        {
          node_key: 'canonical_view',
          node_type: 'preprocessing.process_coordinate_rectify',
          node_category: 'preprocessing',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: {
            view_profile_id: 'stem-measurement-view-v2',
            view_profile: profileV1
          },
          logical_model_ref: null,
          position: { x: 0, y: 0 }
        },
        {
          node_key: 'measurement_eligibility',
          node_type: 'analysis.stem_measurement_eligibility',
          node_category: 'analysis',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { view_profile_id: 'stem-measurement-view-v1' },
          logical_model_ref: null,
          position: { x: 1, y: 0 }
        },
        {
          node_key: 'unrelated',
          node_type: 'analysis.stem_measurement_eligibility',
          node_category: 'analysis',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [],
          config: { view_profile_id: 'another-view' },
          logical_model_ref: null,
          position: { x: 2, y: 0 }
        }
      ],
      edges: [
        {
          from_node_key: 'canonical_view',
          from_port: 'frame',
          to_node_key: 'measurement_eligibility',
          to_port: 'frame'
        }
      ]
    } as RuntimePipelineGraph;

    const updated = applyImageViewProfileToGraph(
      graph,
      'canonical_view',
      'view_profile',
      profileV2
    );

    expect(updated.nodes[0].config.view_profile_id).toBe('stem-measurement-view-v2');
    expect(
      (updated.nodes[0].config.view_profile as RuntimeImageViewProfile).view_profile_id
    ).toBe('stem-measurement-view-v2');
    expect(updated.nodes[1].config.view_profile_id).toBe('stem-measurement-view-v2');
    expect(updated.nodes[2].config.view_profile_id).toBe('another-view');
  });

  it('finds the bound phase calibration and accepts only structured objective artifacts', () => {
    const phaseProfile = {
      schema_version: 'stem.surface-phase-calibration.v1',
      calibration_id: 'phase-v1',
      calibration_version: '1',
      status: 'valid',
      source_channel_id: 'channel-camera',
      image_width: 640,
      image_height: 480,
      intrinsic_calibration_hash: 'a'.repeat(64),
      process_calibration_hash: 'b'.repeat(64),
      rotation_reference_view: 'LOOKING_IN_CONVEYOR_DIRECTION',
      positive_rotation: 'CLOCKWISE',
      fit_model: 'LINEAR_THROUGH_ORIGIN',
      degrees_per_raw_phase: 360,
      observations: [0, 1, 2, 3].map((index) => ({
        observation_id: `phase-${index}`,
        raw_phase: index - 1.5,
        known_angle_deg: (index - 1.5) * 360,
        actual_marking: `ring-${index}`,
        diameter_mm: 300,
        image_center_x_px: 320,
        image_center_y_px: 240,
        weight: 1
      })),
      accepted_observation_count: 4,
      mean_abs_error_deg: 0,
      rms_error_deg: 0,
      maximum_abs_error_deg: 0,
      maximum_mean_abs_error_deg: 3,
      maximum_abs_error_limit_deg: 7.5,
      valid_diameter_min_mm: 300,
      valid_diameter_max_mm: 300,
      valid_image_center_x_min_px: 320,
      valid_image_center_x_max_px: 320,
      valid_image_center_y_min_px: 240,
      valid_image_center_y_max_px: 240,
      provenance: { method: 'MARKED_TEST_STEM', created_at: '2026-08-04T12:00:00Z', notes: '' },
      calibration_hash: 'c'.repeat(64)
    };
    const graph = {
      nodes: [
        { node_key: 'phase', node_type: 'analysis.stem_surface_phase', config: { surface_phase_calibration: phaseProfile } },
        { node_key: 'relay', node_type: 'analysis.stem_horizontal_curvature', config: {} },
        { node_key: 'solver', node_type: 'analysis.stem_curvature_phase_solver', config: {} }
      ],
      edges: [
        { from_node_key: 'phase', from_port: 'phase', to_node_key: 'relay', to_port: 'input' },
        { from_node_key: 'relay', from_port: 'output', to_node_key: 'solver', to_port: 'input' }
      ]
    } as unknown as RuntimePipelineGraph;
    const artifact = {
      schema_version: 'stem.minimum-principle-validation.v1',
      artifact_id: 'objective-acceptance',
      artifact_version: '1',
      status: 'VALIDATION_PASSED',
      source_channel_id: 'channel-camera',
      image_width: 640,
      image_height: 480,
      process_calibration_id: 'process-v1',
      process_calibration_hash: 'b'.repeat(64),
      phase_calibration_id: 'phase-v1',
      phase_calibration_version: '1',
      phase_calibration_hash: 'c'.repeat(64),
      algorithm: 'CURVATURE_PHASE_MINIMUM',
      algorithm_version: 'stem.minimum-principle-objective-validation.v1',
      thresholds: {},
      observations: [{}],
      metrics: {},
      checks: [{}],
      reason_codes: [],
      provenance: { method: 'OBJECTIVE_REFERENCE_BODY_VALIDATION', created_at: '2026-08-05T12:00:00Z', reference_asset_ids: [], notes: '' },
      report_markdown: '# report',
      report_html: '<html></html>',
      artifact_hash: 'd'.repeat(64)
    };

    expect(upstreamStemSurfacePhaseCalibrationProfile(graph, 'solver')?.calibration_id).toBe('phase-v1');
    expect(normalizeStemMinimumPrincipleValidationArtifact(artifact)?.status).toBe('VALIDATION_PASSED');
    expect(normalizeStemMinimumPrincipleValidationArtifact({ ...artifact, checks: [] })).toBeNull();
    expect(normalizeStemMinimumPrincipleValidationArtifact({ ...artifact, algorithm: 'SUBJECTIVE_TARGET_FRAME' })).toBeNull();
  });

  it('filters deployed model capabilities by runtime selection policy', () => {
    const stagedCapability: RuntimeModelCapabilityOption = {
      deployment_release_id: 'rel_staged',
      model_family_slug: 'defect-detector',
      display_name: 'Defect Detector',
      target_path: 'realtime',
      task_type: 'detection',
      model_version_id: 'mvr_staged',
      version_label: '2026.05',
      deployment_status: 'staged',
      architecture_family: 'yolo',
      architecture_version: 'v11',
      architecture_size: 'm',
      architecture_display_name: 'YOLOv11 Medium',
      contract_major: 1,
      active: true,
      capability: {}
    };

    expect(modelCapabilityMatchesSelectionPolicy(stagedCapability, 'active_production')).toBe(false);
    expect(modelCapabilityMatchesSelectionPolicy(stagedCapability, 'staged_test')).toBe(true);
    expect(modelCapabilityMatchesSelectionPolicy(stagedCapability, 'explicit_release')).toBe(true);

    expect(
      modelCapabilityMatchesSelectionPolicy(
        { ...stagedCapability, deployment_status: 'activated' },
        'active_production'
      )
    ).toBe(false);
    expect(
      modelCapabilityMatchesSelectionPolicy(
        { ...stagedCapability, deployment_status: 'production' },
        'active_production'
      )
    ).toBe(true);
  });

  it('resolves picker selections and structured boxes from stored values', () => {
    const modelRef: RuntimeLogicalModelRef = {
      ...defaultModelRef('detection'),
      model_family_slug: 'defect-detector'
    };
    const options: RuntimeModelCapabilityOption[] = [
      {
        deployment_release_id: 'rel_123',
        model_family_slug: 'defect-detector',
        display_name: 'Defect Detector',
        target_path: 'realtime',
        task_type: 'detection',
        model_version_id: 'mvr_123',
        version_label: '2026.05',
        deployment_status: 'production',
        architecture_family: 'yolo',
        architecture_version: 'v11',
        architecture_size: 'm',
        architecture_display_name: 'YOLOv11 Medium',
        contract_major: 1,
        active: true,
        capability: {}
      }
    ];

    expect(resolveSelectedModelReleaseId(modelRef, options)).toBe('rel_123');
    expect(
      normalizeBoxList([
        {
          label: 'Zone A',
          x_min: 0.1,
          y_min: 0.2,
          x_max: 0.4,
          y_max: 0.8,
          angle_degrees: -24
        }
      ])
    ).toEqual([
      {
        label: 'Zone A',
        x: 0.1,
        y: 0.2,
        width: 0.30000000000000004,
        height: 0.6000000000000001,
        angle_degrees: -24
      }
    ]);
    expect(
      normalizeTrackerRoiList([
        {
          label: 'Entry',
          points: [
            { x: 0.7, y: 0.1 },
            { x: 1, y: 0 },
            { x: 1, y: 1 },
            { x: 0.8, y: 0.9 }
          ]
        },
        { label: 'Legacy', x: 0.1, y: 0.2, width: 0.3, height: 0.4 }
      ])
    ).toEqual([
      {
        label: 'Entry',
        points: [
          { x: 0.7, y: 0.1 },
          { x: 1, y: 0 },
          { x: 1, y: 1 },
          { x: 0.8, y: 0.9 }
        ]
      },
      {
        label: 'Legacy',
        points: [
          { x: 0.1, y: 0.2 },
          { x: 0.4, y: 0.2 },
          { x: 0.4, y: 0.6000000000000001 },
          { x: 0.1, y: 0.6000000000000001 }
        ]
      }
    ]);
  });

  it('keeps runtime port compatibility aligned with backend rules', () => {
    const fromPort: RuntimeNodePort = { key: 'detections', data_type: 'detections', required: true };
    const toPort: RuntimeNodePort = { key: 'result', data_type: 'result', required: true };
    const nodeType: RuntimeNodeTypeSummary = {
      node_type: 'output.persisted_result',
      node_category: 'output',
      contract_version: 1,
      display_name: 'Persisted result',
      description: '',
      supported_modes: ['realtime'],
      input_ports: [toPort],
      output_ports: [],
      config_schema_ref: 'runtime.node.output.persisted_result.config.v1',
      property_schema: [],
      default_config: {},
      runtime_handler_ref: 'cv_runtime.nodes.builtin:output_persisted_result',
      compatibility_constraints: {}
    };

    expect(portsCompatible(fromPort, toPort, nodeType)).toBe(true);
    expect(
      portsCompatible(
        { key: 'frame', data_type: 'frame', required: true },
        { key: 'detections', data_type: 'detections', required: true },
        nodeType
      )
    ).toBe(false);
    expect(
      portsCompatible(
        { key: 'detections', data_type: 'detections', required: true },
        { key: 'objects', data_type: 'object_set', required: false },
        {
          ...nodeType,
          node_type: 'output.display_output',
          compatibility_constraints: {
            port_aliases: { object_set: ['detections', 'segments', 'region_set'] }
          }
        }
      )
    ).toBe(true);
    expect(
      portsCompatible(
        { key: 'free_region', data_type: 'free_region', required: true },
        toPort,
        nodeType
      )
    ).toBe(true);
    expect(
      portsCompatible(
        { key: 'edges', data_type: 'cv_edges', required: true },
        toPort,
        nodeType
      )
    ).toBe(true);
    expect(
      portsCompatible(
        { key: 'curvature', data_type: 'stem_horizontal_curvature', required: true },
        toPort,
        nodeType
      )
    ).toBe(true);
  });

  it('supports pipeline-based trigger node configuration helpers', () => {
    expect(
      normalizeResponseMappings([{ source_field: 'count', response_key: 'piece_count', required: true }])
    ).toEqual([{ source_field: 'count', response_key: 'piece_count', required: true }]);

    const triggerNode: RuntimeNodeSpec = {
      node_key: 'http_trigger',
      node_type: 'trigger.http_put',
      node_category: 'trigger',
      node_contract_version: 1,
      input_ports: [],
      output_ports: [{ key: 'trigger', data_type: 'trigger', required: true }],
      config: {},
      position: { x: 0, y: 0 }
    };
    const channelInputNode: RuntimeNodeSpec = {
      node_key: 'channel_input',
      node_type: 'source.channel_frame',
      node_category: 'source',
      node_contract_version: 1,
      input_ports: [{ key: 'trigger', data_type: 'trigger', required: false }],
      output_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      config: {},
      position: { x: 200, y: 0 }
    };
    const graph = {
      nodes: [triggerNode, channelInputNode],
      edges: []
    };
    const triggerType: RuntimeNodeTypeSummary = {
      node_type: 'trigger.http_put',
      node_category: 'trigger',
      contract_version: 1,
      display_name: 'HTTP trigger',
      description: '',
      supported_modes: ['trigger'],
      input_ports: [],
      output_ports: [{ key: 'trigger', data_type: 'trigger', required: true }],
      config_schema_ref: 'runtime.node.trigger.http_put.config.v1',
      property_schema: [],
      default_config: {},
      runtime_handler_ref: 'cv_runtime.nodes.builtin:trigger_http_put',
      compatibility_constraints: {}
    };
    const channelInputType: RuntimeNodeTypeSummary = {
      node_type: 'source.channel_frame',
      node_category: 'source',
      contract_version: 1,
      display_name: 'Channel frame input',
      description: '',
      supported_modes: ['realtime', 'trigger', 'interval'],
      input_ports: [{ key: 'trigger', data_type: 'trigger', required: false }],
      output_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      config_schema_ref: 'runtime.node.source.channel_frame.config.v1',
      property_schema: [],
      default_config: {},
      runtime_handler_ref: 'cv_runtime.nodes.builtin:source_channel_frame',
      compatibility_constraints: {}
    };

    expect(inferGraphMode(graph, 'realtime')).toBe('trigger');
    expect(
      buildCompatibleConnections(
        graph,
        triggerNode,
        triggerType,
        new Map([
          ['trigger.http_put', triggerType],
          ['source.channel_frame', channelInputType]
        ])
      ).map((item) => item.edge)
    ).toContainEqual({
      from_node_key: 'http_trigger',
      from_port: 'trigger',
      to_node_key: 'channel_input',
      to_port: 'trigger'
    });
  });

  it('filters the node palette by graph-compatible authoring rules', () => {
    const sourceType = testNodeType({
      node_type: 'source.channel_frame',
      node_category: 'source',
      display_name: 'Channel frame',
      supported_modes: ['realtime', 'trigger', 'interval'],
      input_ports: [{ key: 'trigger', data_type: 'trigger', required: false }],
      output_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      compatibility_constraints: {
        authoring: { starter: true, singleton: true, palette_reason: 'Supplies channel frames.' }
      }
    });
    const triggerType = testNodeType({
      node_type: 'trigger.http_put',
      node_category: 'trigger',
      display_name: 'HTTP trigger',
      supported_modes: ['trigger'],
      output_ports: [{ key: 'trigger', data_type: 'trigger', required: true }],
      compatibility_constraints: {
        authoring: {
          starter: true,
          singleton: true,
          blocks_with_node_types: ['trigger.interval'],
          palette_reason: 'Starts trigger executions.'
        }
      }
    });
    const intervalTriggerType = testNodeType({
      node_type: 'trigger.interval',
      node_category: 'trigger',
      display_name: 'Interval trigger',
      supported_modes: ['interval'],
      output_ports: [{ key: 'trigger', data_type: 'trigger', required: true }],
      compatibility_constraints: {
        authoring: { starter: true, singleton: true, blocks_with_node_types: ['trigger.http_put'] }
      }
    });
    const cropType = testNodeType({
      node_type: 'preprocessing.crop',
      node_category: 'preprocessing',
      display_name: 'Crop',
      input_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      output_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      compatibility_constraints: {
        authoring: { requires_upstream_data_types: ['frame'] }
      }
    });
    const detectionType = testNodeType({
      node_type: 'inference.detection',
      node_category: 'inference',
      display_name: 'Detection',
      input_ports: [{ key: 'frame', data_type: 'frame', required: true }],
      output_ports: [{ key: 'detections', data_type: 'detections', required: true }],
      compatibility_constraints: {
        authoring: { requires_upstream_data_types: ['frame'] }
      }
    });
    const resultType = testNodeType({
      node_type: 'output.result',
      node_category: 'output',
      display_name: 'Result',
      input_ports: [{ key: 'result', data_type: 'result', required: true }],
      output_ports: [],
      compatibility_constraints: {
        authoring: { requires_upstream_data_types: ['result'], terminal: true }
      }
    });
    const hiddenType = testNodeType({
      node_type: 'analysis.legacy',
      node_category: 'analysis',
      display_name: 'Legacy',
      compatibility_constraints: { hidden_from_palette: true }
    });
    const nodeTypes = [sourceType, triggerType, intervalTriggerType, cropType, detectionType, resultType, hiddenType];
    const nodeTypeMap = new Map(nodeTypes.map((item) => [item.node_type, item]));

    expect(
      eligibleNodeTypesForGraph({
        graph: { nodes: [], edges: [] },
        nodeTypes,
        nodeTypeMap,
        effectiveMode: 'realtime'
      }).map((item) => item.node_type)
    ).toEqual(['source.channel_frame', 'trigger.http_put', 'trigger.interval']);

    const sourceGraph: RuntimePipelineGraph = {
      nodes: [testGraphNode(sourceType, 'channel_input')],
      edges: []
    };
    const sourceEligible = eligibleNodeTypesForGraph({
      graph: sourceGraph,
      nodeTypes,
      nodeTypeMap,
      effectiveMode: 'realtime'
    }).map((item) => item.node_type);
    expect(sourceEligible).toContain('preprocessing.crop');
    expect(sourceEligible).toContain('inference.detection');
    expect(sourceEligible).toContain('trigger.http_put');
    expect(sourceEligible).not.toContain('source.channel_frame');
    expect(sourceEligible).not.toContain('output.result');

    const detectionGraph: RuntimePipelineGraph = {
      nodes: [
        testGraphNode(sourceType, 'channel_input'),
        testGraphNode(detectionType, 'detect')
      ],
      edges: []
    };
    expect(canAddNodeToGraph(detectionGraph, resultType, nodeTypeMap, 'realtime').eligible).toBe(true);
    expect(canAddNodeToGraph(detectionGraph, triggerType, nodeTypeMap, 'realtime').eligible).toBe(true);

    const triggerGraph: RuntimePipelineGraph = {
      nodes: [testGraphNode(triggerType, 'http_trigger')],
      edges: []
    };
    expect(canAddNodeToGraph(triggerGraph, intervalTriggerType, nodeTypeMap, 'trigger').eligible).toBe(false);
    expect(canAddNodeToGraph(triggerGraph, sourceType, nodeTypeMap, 'trigger').eligible).toBe(true);
    const csvSource = testNodeType({
      node_type: 'source.stem_csv_measurements', node_category: 'source', display_name: 'CSV measurements',
      supported_modes: ['trigger'], input_ports: [],
      output_ports: [{ key: 'measurement', data_type: 'stem_csv', required: true }],
      compatibility_constraints: { auxiliary_source: true, authoring: { starter: true } }
    });
    nodeTypeMap.set(csvSource.node_type, csvSource);
    const pairedGraph: RuntimePipelineGraph = {
      nodes: [...triggerGraph.nodes, testGraphNode(sourceType, 'photo')], edges: []
    };
    expect(canAddNodeToGraph(pairedGraph, csvSource, nodeTypeMap, 'trigger').eligible).toBe(true);
    pairedGraph.nodes.push(testGraphNode(csvSource, 'csv'));
    expect(canAddNodeToGraph(pairedGraph, sourceType, nodeTypeMap, 'trigger').eligible).toBe(false);
  });

  it('normalizes Siemens SPS mappings and groups the node under Industrial PLC', () => {
    expect(
      normalizeSiemensSpsMappings([
        {
          key: 'class_to_int',
          label: 'Class',
          source: { field: 'top_label.label' },
          target: { db_number: 101, byte_offset: 0, data_type: 'INT' },
          conversion: {
            mode: 'enum',
            enum_values: [
              { source: 'gut', value: '1' },
              { source: 'schlecht', value: '0' }
            ]
          }
        },
        {
          key: 'confidence_percent',
          source: { field: 'top_label.score' },
          target: { db_number: 101, byte_offset: 10, data_type: 'INT' },
          conversion: { mode: 'scale', multiplier: 100, rounding: 'nearest', clamp_min: 0, clamp_max: 100 }
        }
      ])
    ).toMatchObject([
      {
        key: 'class_to_int',
        source: { field: 'top_label.label' },
        target: { db_number: 101, byte_offset: 0, data_type: 'INT' },
        conversion: {
          mode: 'enum',
          enum_values: [
            { source: 'gut', value: 1 },
            { source: 'schlecht', value: 0 }
          ]
        }
      },
      {
        key: 'confidence_percent',
        source: { field: 'top_label.score' },
        target: { db_number: 101, byte_offset: 10, data_type: 'INT' },
        conversion: { mode: 'scale', multiplier: 100, rounding: 'nearest', clamp_min: 0, clamp_max: 100 }
      }
    ]);

    const outputNodes: RuntimeNodeTypeSummary[] = [
      {
        node_type: 'output.result',
        node_category: 'output',
        contract_version: 1,
        display_name: 'Result output',
        description: '',
        supported_modes: ['realtime', 'trigger', 'interval'],
        input_ports: [],
        output_ports: [],
        config_schema_ref: 'runtime.node.output.result.config.v1',
        property_schema: [],
        default_config: {},
        runtime_handler_ref: 'cv_runtime.nodes.builtin:output_result',
        compatibility_constraints: {}
      },
      {
        node_type: 'output.siemens_sps_write',
        node_category: 'output',
        contract_version: 1,
        display_name: 'Write to Siemens SPS',
        description: '',
        supported_modes: ['realtime', 'trigger', 'interval'],
        input_ports: [],
        output_ports: [],
        config_schema_ref: 'runtime.node.output.siemens_sps_write.config.v1',
        property_schema: [],
        default_config: {},
        runtime_handler_ref: 'cv_runtime.siemens_sps:output_siemens_sps_write',
        compatibility_constraints: { palette_group: 'Industrial / PLC' }
      }
    ];

    expect(paletteGroupsForCategory('output', outputNodes)).toEqual([
      { label: 'Output', items: [outputNodes[0]] },
      { label: 'Industrial / PLC', items: [outputNodes[1]] }
    ]);
  });

  it('builds guided builder groups from all visible catalog nodes', () => {
    const cannyType = testNodeType({
      node_type: 'computer_vision.canny_edges',
      node_category: 'computer_vision',
      display_name: 'Canny edges'
    });
    const lineType = testNodeType({
      node_type: 'computer_vision.hough_lines',
      node_category: 'computer_vision',
      display_name: 'Hough lines'
    });
    const hiddenLegacyType = testNodeType({
      node_type: 'computer_vision.legacy_debug',
      node_category: 'computer_vision',
      display_name: 'Legacy debug',
      compatibility_constraints: { hidden_from_palette: true }
    });
    const measurementType = testNodeType({
      node_type: 'analysis.feature_measurement',
      node_category: 'analysis',
      display_name: 'Feature measurement'
    });
    const qualityType = testNodeType({
      node_type: 'analysis.quality_rule_evaluation',
      node_category: 'analysis',
      display_name: 'Quality rules'
    });

    const cvGroups = guidedNodeGroupsForCategory('computer_vision', [
      cannyType,
      lineType,
      hiddenLegacyType,
      measurementType
    ]);
    expect(cvGroups).toEqual([
      { label: 'Computer Vision', items: [cannyType, lineType] }
    ]);

    const analysisGroups = guidedNodeGroupsForCategory('analysis', [
      cannyType,
      measurementType,
      qualityType
    ]);
    expect(analysisGroups).toEqual([
      { label: 'Measurement', items: [measurementType] },
      { label: 'Rules / Decision', items: [qualityType] }
    ]);
  });

  it('recognizes result output nodes as trigger responses', () => {
    const graph = {
      nodes: [
        {
          node_key: 'http_trigger',
          node_type: 'trigger.http_put',
          config: {
            endpoint_slug: 'yard-result',
            method: 'PUT',
            auth_required: true,
            timeout_ms: 3000
          }
        },
        { node_key: 'channel_input', node_type: 'source.channel_frame', config: {} },
        { node_key: 'result_output', node_type: 'output.result', config: {} }
      ],
      edges: []
    };

    expect(triggerablePipelineSummary(graph as unknown as RuntimePipelineGraph, undefined)).toEqual({
      method: 'PUT',
      path: '/api/v1/runtime/triggers/yard-result',
      authLabel: 'API key',
      timeoutMs: 3000
    });
  });

  it('converts normalized runtime graphs to minimal authoring graphs', () => {
    const graph: RuntimePipelineGraph = {
      nodes: [
        {
          node_key: 'channel_input',
          node_type: 'source.channel_frame',
          node_category: 'source',
          node_contract_version: 1,
          input_ports: [],
          output_ports: [{ key: 'frame', data_type: 'frame', required: true }],
          config: { channel_id: 'chn_1' },
          logical_model_ref: null,
          position: { x: 80, y: 120 }
        },
        {
          node_key: 'detect_objects',
          node_type: 'inference.detection',
          node_category: 'inference',
          node_contract_version: 1,
          input_ports: [{ key: 'frame', data_type: 'frame', required: true }],
          output_ports: [{ key: 'detections', data_type: 'detections', required: true }],
          config: { confidence_threshold: 0.5 },
          logical_model_ref: defaultModelRef('detection'),
          position: { x: 300, y: 120 }
        }
      ],
      edges: [
        {
          from_node_key: 'channel_input',
          from_port: 'frame',
          to_node_key: 'detect_objects',
          to_port: 'frame'
        }
      ]
    };

    expect(graphToAuthoringGraph(graph)).toEqual({
      nodes: [
        {
          node_key: 'channel_input',
          node_type: 'source.channel_frame',
          config: { channel_id: 'chn_1' },
          logical_model_ref: null,
          position: { x: 80, y: 120 }
        },
        {
          node_key: 'detect_objects',
          node_type: 'inference.detection',
          config: { confidence_threshold: 0.5 },
          logical_model_ref: defaultModelRef('detection'),
          position: { x: 300, y: 120 }
        }
      ],
      edges: graph.edges
    });
  });

  it('derives result output field choices from connected pipeline nodes', () => {
    const graph = {
      nodes: [
        {
          node_key: 'count_objects',
          node_type: 'analysis.object_counting',
          node_category: 'analysis',
          output_ports: [{ key: 'counts', data_type: 'measurements', required: true }],
          config: { group_by: 'label' }
        },
        {
          node_key: 'close_detection',
          node_type: 'analysis.close_detection',
          node_category: 'analysis',
          output_ports: [{ key: 'result', data_type: 'result', required: true }],
          config: {
            rules: [
              {
                primary_labels: ['LKW'],
                secondary_labels: ['Lader']
              }
            ]
          }
        },
        {
          node_key: 'result_output',
          node_type: 'output.result',
          node_category: 'output',
          output_ports: [],
          config: {}
        }
      ],
      edges: [
        {
          from_node_key: 'count_objects',
          from_port: 'counts',
          to_node_key: 'result_output',
          to_port: 'result'
        }
      ]
    };
    const nodeTypes = new Map<string, RuntimeNodeTypeSummary>([
      [
        'analysis.object_counting',
        {
          node_type: 'analysis.object_counting',
          node_category: 'analysis',
          contract_version: 1,
          display_name: 'Object counting',
          description: '',
          supported_modes: ['realtime'],
          input_ports: [],
          output_ports: [{ key: 'counts', data_type: 'measurements', required: true }],
          config_schema_ref: '',
          property_schema: [],
          default_config: {},
          runtime_handler_ref: '',
          compatibility_constraints: {}
        }
      ]
    ]);

    const options = resultFieldOptionsForNode(
      graph as unknown as RuntimePipelineGraph,
      graph.nodes[2] as unknown as RuntimeNodeSpec,
      nodeTypes
    ).map((option) => option.value);

    expect(options).toContain('count_LKW');
    expect(options).toContain('count_Lader');
    expect(options).toContain('result');
    expect(suggestedResponseKey('top_label.label')).toBe('class');
  });

  it('prefers output catalog field choices when available', () => {
    const graph = {
      nodes: [
        {
          node_key: 'detect_objects',
          node_type: 'inference.detection',
          node_category: 'inference',
          output_ports: [{ key: 'detections', data_type: 'detections', required: true }],
          config: {}
        },
        {
          node_key: 'result_output',
          node_type: 'output.result',
          node_category: 'output',
          output_ports: [],
          config: {}
        }
      ],
      edges: [
        {
          from_node_key: 'detect_objects',
          from_port: 'detections',
          to_node_key: 'result_output',
          to_port: 'result'
        }
      ]
    };
    const outputCatalog: RuntimePipelineOutputCatalog = {
      entries: [
        {
          node_key: 'detect_objects',
          node_type: 'inference.detection',
          output_port: 'detections',
          payload_type: 'detections',
          fields: ['objects'],
          field_options: [
            {
              key: 'objects',
              label: 'Detected objects',
              value_type: 'object_list',
              aggregate_ops: ['count', 'list'],
              filterable_fields: ['label', 'zone'],
              list_item_fields: ['label', 'position']
            }
          ]
        }
      ],
      destinations: ['response', 'history', 'live_view', 'overlay', 'plc', 'connector'],
      aggregate_ops: ['none', 'count', 'list', 'first']
    };

    const options = resultFieldOptionsForNode(
      graph as unknown as RuntimePipelineGraph,
      graph.nodes[1] as unknown as RuntimeNodeSpec,
      new Map(),
      null,
      outputCatalog
    );

    expect(options).toContainEqual(
      expect.objectContaining({
        value: 'objects',
        label: 'inference.detection / Detected objects',
        valueType: 'object_list'
      })
    );
  });

  it('normalizes result projections for flat response output', () => {
    expect(
      normalizeResultProjections([
        {
          key: 'skewed count',
          source: {
            node_key: 'orientation',
            output_port: 'result',
            path: 'matches',
            value_type: 'object_list'
          },
          filters: [{ path: 'skewed', op: 'truthy' }],
          aggregate: { op: 'count' },
          destinations: ['response']
        }
      ])
    ).toEqual([
      expect.objectContaining({
        key: 'skewed_count',
        source: expect.objectContaining({ node_key: 'orientation', path: 'matches' }),
        aggregate: { op: 'count', path: null },
        destinations: ['response']
      })
    ]);
  });

  it('offers Siemens SPS friendly source field aliases for classification outputs', () => {
    const graph = {
      nodes: [
        {
          node_key: 'classifier',
          node_type: 'inference.classification',
          node_category: 'inference',
          output_ports: [{ key: 'classifications', data_type: 'classifications', required: true }],
          config: {},
          logical_model_ref: {
            required_task_type: 'classification',
            target_path: 'onnx',
            contract_major: 1,
            selection_policy: 'latest_compatible'
          }
        },
        {
          node_key: 'sps_write',
          node_type: 'output.siemens_sps_write',
          node_category: 'output',
          input_ports: [{ key: 'result', data_type: 'result', required: true }],
          config: {}
        }
      ],
      edges: [
        {
          from_node_key: 'classifier',
          from_port: 'classifications',
          to_node_key: 'sps_write',
          to_port: 'result'
        }
      ]
    };
    const nodeTypes = new Map<string, RuntimeNodeTypeSummary>();

    const options = resultFieldOptionsForNode(
      graph as unknown as RuntimePipelineGraph,
      graph.nodes[1] as unknown as RuntimeNodeSpec,
      nodeTypes
    ).map((option) => option.value);

    expect(options).toContain('classification.top1.label');
    expect(options).toContain('classification.top1.score');
  });

  it('normalizes structured board measurement node configuration', () => {
    expect(
      normalizeEdgeRoleMappings([
        { feature_class: 'board_start_edge', edge_role: 'start_edge', bbox_side: 'left' },
        { class_name: 'board_side_edge', role: 'side_edge', bbox_side: 'top' }
      ])
    ).toEqual([
      { feature_class: 'board_start_edge', edge_role: 'start_edge', bbox_side: 'left' },
      { feature_class: 'board_side_edge', edge_role: 'side_edge', bbox_side: 'top' }
    ]);

    expect(
      normalizeMeasurementRules([
        { feature_type: 'knot', bases: ['bbox_width', 'axis_projected_length', 'unsupported'] }
      ])
    ).toEqual([{ feature_type: 'knot', bases: ['bbox_width', 'axis_projected_length'] }]);

    expect(
      normalizeClearanceRules([
        {
          feature_type: 'knot',
          measurement_basis: 'bbox_width',
          multiplier: '1.5',
          minimum_clearance_px: '2',
          apply_before: true,
          apply_after: false
        }
      ])
    ).toMatchObject([
      {
        feature_type: 'knot',
        enabled: true,
        measurement_basis: 'bbox_width',
        multiplier: 1.5,
        minimum_clearance_px: 2,
        apply_before: true,
        apply_after: false,
        merge_overlapping_zones: true
      }
    ]);

    expect(normalizeMetricApplyTo('free_region, selected_result_fields, nope')).toEqual([
      'free_region',
      'selected_result_fields'
    ]);

    expect(
      normalizeQualityRules([{ rule_type: 'free_region_distance', operator: '<=', threshold: '250' }])
    ).toEqual([
      {
        rule_type: 'free_region_distance',
        feature_type: '',
        measurement_basis: 'bbox_width',
        operator: '<=',
        threshold: 250,
        status_on_fail: 'warning'
      }
    ]);
  });

  it('normalizes Computer Vision edge selection configuration', () => {
    expect(
      normalizeCvLineSelectionRules([
        {
          edge_role: 'start_edge',
          target_angle_degrees: '90',
          angle_tolerance_degrees: '20',
          minimum_line_length_px: '40',
          region_preference: 'left',
          nearest_to_image_side: 'left',
          ranking: 'nearest_to_side',
          required: true
        },
        {
          role: 'side_edge',
          target_angle_degrees: 0,
          angle_tolerance_degrees: 15,
          minimum_line_length_px: 25,
          region_preference: 'top',
          nearest_to_image_side: 'top',
          ranking: 'longest'
        }
      ])
    ).toEqual([
      {
        edge_role: 'start_edge',
        target_angle_degrees: 90,
        angle_tolerance_degrees: 20,
        minimum_line_length_px: 40,
        region_preference: 'left',
        nearest_to_image_side: 'left',
        ranking: 'nearest_to_side',
        required: true
      },
      {
        edge_role: 'side_edge',
        target_angle_degrees: 0,
        angle_tolerance_degrees: 15,
        minimum_line_length_px: 25,
        region_preference: 'top',
        nearest_to_image_side: 'top',
        ranking: 'longest',
        required: true
      }
    ]);
  });

  it('normalizes object spatial signal rules', () => {
    expect(
      normalizeSpatialRules([
        {
          rule_key: 'person_forklift',
          relation: 'approaching',
          primary_labels: 'person',
          secondary_labels: ['forklift'],
          label_pairing: 'different_label',
          max_distance_percent: '25',
          min_closing_percent: '2.5',
          zone_match: 'box_overlaps',
          min_count: '2'
        }
      ])
    ).toEqual([
      {
        rule_key: 'person_forklift',
        relation: 'approaching',
        enabled: true,
        primary_labels: ['person'],
        secondary_labels: ['forklift'],
        label_pairing: 'different_label',
        max_distance_percent: 25,
        min_distance_percent: null,
        min_overlap_percent: 1,
        min_closing_percent: 2.5,
        zone_key: '',
        zone_match: 'box_overlaps',
        min_count: 2,
        max_count: null,
        signal_label: ''
      }
    ]);
  });

  it('normalizes color segmentation and direction reference configuration', () => {
    expect(
      normalizeColorProfiles([
        {
          label: 'board',
          hue_min: '-10',
          hue_max: '190',
          saturation_min: '0',
          saturation_max: '95',
          value_min: '140',
          value_max: '255',
          min_area_percent: '0.2',
          max_area_percent: '',
          min_aspect_ratio: '0.4',
          display_color: '#f3d7a5'
        }
      ])
    ).toEqual([
      {
        label: 'board',
        enabled: true,
        color_space: 'hsv',
        color_tolerance_percent: 55,
        hue_min: 0,
        hue_max: 179,
        saturation_min: 0,
        saturation_max: 180,
        value_min: 128,
        value_max: 255,
        min_area_percent: 0.2,
        max_area_percent: null,
        min_aspect_ratio: 1,
        display_color: '#f3d7a5'
      }
    ]);

    expect(
      normalizeColorProfiles([
        {
          label: 'red_part',
          display_color: '#ff0000',
          color_tolerance_percent: 10
        }
      ])[0]
    ).toMatchObject({
      label: 'red_part',
      color_space: 'hsv',
      color_tolerance_percent: 10,
      hue_min: 172,
      hue_max: 8,
      saturation_min: 217,
      saturation_max: 255,
      value_min: 209,
      value_max: 255,
      display_color: '#ff0000'
    });

    expect(
      normalizeDirectionReference({
        label: 'parallel',
        start: { x: -1, y: 0.25 },
        end: { x: 1.5, y: 0.25 }
      })
    ).toEqual({
      label: 'parallel',
      start: { x: 0, y: 0.25 },
      end: { x: 1, y: 0.25 }
    });
  });
});

function testNodeType(
  patch: Partial<RuntimeNodeTypeSummary> & Pick<RuntimeNodeTypeSummary, 'node_type' | 'node_category' | 'display_name'>
): RuntimeNodeTypeSummary {
  return {
    contract_version: 1,
    description: '',
    supported_modes: ['realtime'],
    input_ports: [],
    output_ports: [],
    config_schema_ref: '',
    property_schema: [],
    default_config: {},
    runtime_handler_ref: '',
    compatibility_constraints: {},
    ...patch
  };
}

function testGraphNode(nodeType: RuntimeNodeTypeSummary, nodeKey: string): RuntimeNodeSpec {
  return {
    node_key: nodeKey,
    node_type: nodeType.node_type,
    node_category: nodeType.node_category,
    node_contract_version: nodeType.contract_version,
    input_ports: nodeType.input_ports,
    output_ports: nodeType.output_ports,
    config: {},
    logical_model_ref: null,
    position: { x: 0, y: 0 }
  };
}
