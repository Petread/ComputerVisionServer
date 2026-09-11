from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Poll one running YBB runtime channel without mutating server state. "
            "The API token is read only from YBB_RUNTIME_API_KEY."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--interval-seconds", type=float, default=0.25)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--timings-only",
        action="store_true",
        help="Print only the compact node timing summary.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow a self-signed HTTPS certificate on a trusted plant LAN.",
    )
    return parser


def _request_json(url: str, *, token: str, insecure: bool) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ybb-runtime-live-probe/1",
        },
        method="GET",
    )
    context = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        return json.load(response)


def _overlay_metadata(
    state: dict[str, Any],
    layer: str,
    *,
    required_key: str | None = None,
) -> dict[str, Any]:
    primitives = state.get("overlay")
    if not isinstance(primitives, list):
        return {}
    for primitive in reversed(primitives):
        if isinstance(primitive, dict) and primitive.get("layer") == layer:
            metadata = primitive.get("metadata")
            if isinstance(metadata, dict) and (
                required_key is None or required_key in metadata
            ):
                return metadata
    return {}


def _node_statuses(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    diagnostics = state.get("diagnostics")
    timings = diagnostics.get("pipeline_node_timings") if isinstance(diagnostics, dict) else None
    if not isinstance(timings, list):
        return {}
    return {
        str(item.get("node_key")): {
            key: item.get(key)
            for key in (
                "status",
                "skip_reason",
                "error_code",
                "error_metadata",
                "total_ms",
                "execute_ms",
            )
            if item.get(key) is not None
        }
        for item in timings
        if isinstance(item, dict) and item.get("node_key")
    }


def _sample(document: dict[str, Any]) -> dict[str, Any]:
    state = document.get("state") if isinstance(document.get("state"), dict) else {}
    frame = state.get("frame") if isinstance(state.get("frame"), dict) else {}
    eligibility = _overlay_metadata(state, "stem_capture_eligibility")
    tracking = _overlay_metadata(state, "stem_tracking_status")
    tracking_candidate = _overlay_metadata(state, "stem_tracking_candidates")
    tracking_movement = (
        tracking.get("movement") if isinstance(tracking.get("movement"), dict) else {}
    )
    curvature = _overlay_metadata(state, "stem_curvature_status")
    phase = _overlay_metadata(state, "stem_surface_phase")
    solver_fit = _overlay_metadata(
        state,
        "stem_curvature_phase_diagnostic",
        required_key="solver_state",
    )
    solver_gate = _overlay_metadata(
        state,
        "stem_curvature_phase_diagnostic",
        required_key="decision_gate_reached",
    )
    solver_summary = _overlay_metadata(
        state,
        "stem_curvature_phase_diagnostic",
        required_key="diagnostic_graph",
    )
    layers = state.get("layers_available")
    reason_codes = eligibility.get("reason_codes")
    source_health = (
        state.get("source_health") if isinstance(state.get("source_health"), dict) else {}
    )
    metrics = state.get("metrics") if isinstance(state.get("metrics"), dict) else {}
    diagnostics = (
        state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {}
    )
    rotation_view = (
        state.get("stem_rotation_view")
        if isinstance(state.get("stem_rotation_view"), dict)
        else {}
    )
    rotation_diagnostics = (
        rotation_view.get("diagnostics")
        if isinstance(rotation_view.get("diagnostics"), dict)
        else {}
    )
    rotation_fit_diagnostics = rotation_diagnostics.get("fit_diagnostics")
    if not isinstance(rotation_fit_diagnostics, list):
        rotation_fit_diagnostics = []
    frame_metadata = frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
    return {
        "observed_at_unix": time.time(),
        "updated_at": state.get("updated_at"),
        "actual_state": state.get("actual_state"),
        "available": state.get("available"),
        "stale": state.get("stale"),
        "frame_sequence": frame.get("sequence"),
        "frame_timestamp": frame.get("source_timestamp"),
        "display_context_id": state.get("display_context_id"),
        "recovery_hint": state.get("recovery_hint"),
        "source_health_state": source_health.get("state"),
        "source_health_connected": source_health.get("connected"),
        "source_health_status": source_health.get("status_text"),
        "source_health_error": source_health.get("last_error"),
        "fps": metrics.get("fps"),
        "source_fps": metrics.get("source_fps"),
        "processed_fps": metrics.get("processed_fps"),
        "drop_rate_percent": metrics.get("drop_rate_percent"),
        "dropped_frame_count": metrics.get("dropped_frame_count"),
        "latency_ms": metrics.get("latency_ms"),
        "frame_drop_error": frame_metadata.get("frame_drop_error"),
        "diagnostic_warnings": diagnostics.get("warnings") or [],
        "layers": sorted(str(item) for item in layers) if isinstance(layers, list) else [],
        "overlay_count": len(state.get("overlay") or []),
        "eligibility": eligibility.get("status"),
        "eligibility_reasons": (
            [str(item) for item in reason_codes] if isinstance(reason_codes, list) else []
        ),
        "track_id": tracking.get("track_id"),
        "track_status": tracking.get("status") or tracking_candidate.get("status"),
        "track_selection_reason": (
            tracking.get("selection_reason")
            or tracking_candidate.get("selection_reason")
        ),
        "track_candidate_count": tracking.get("candidate_count"),
        "track_eligible_candidate_count": tracking.get("eligible_candidate_count"),
        "track_process_zone": (
            tracking.get("process_zone") or tracking_candidate.get("process_zone")
        ),
        "track_process_zone_source": tracking_movement.get("process_zone_source"),
        "track_age_frames": tracking.get("track_age_frames"),
        "track_process_motion_axis": tracking_movement.get("process_motion_axis"),
        "track_process_motion_confidence": tracking_movement.get(
            "process_motion_axis_confidence"
        ),
        "track_longitudinal_motion_streak": tracking_movement.get(
            "longitudinal_motion_streak"
        ),
        "track_delta_x_mm": tracking_movement.get("delta_x_mm"),
        "track_delta_y_mm": tracking_movement.get("delta_y_mm"),
        "track_velocity_x_mm_per_frame": tracking_movement.get(
            "velocity_x_mm_per_frame"
        ),
        "track_velocity_y_mm_per_frame": tracking_movement.get(
            "velocity_y_mm_per_frame"
        ),
        "curvature_valid": curvature.get("valid"),
        "curvature_reasons": curvature.get("reason_codes"),
        "curvature_signed_bow_mm": curvature.get("signed_bow_mm"),
        "curvature_confidence": curvature.get("geometry_confidence"),
        "curvature_visible_length_ratio": curvature.get("visible_length_ratio"),
        "phase_mode": phase.get("tracking_mode"),
        "phase_locked": phase.get("phase_locked"),
        "phase_calibrated_valid": phase.get("calibrated_phase_valid"),
        "phase_raw": phase.get("raw_phase"),
        "phase_cumulative_deg": phase.get("cumulative_phase_deg"),
        "phase_confidence": phase.get("confidence"),
        "phase_feature_count": phase.get("feature_count"),
        "phase_inlier_ratio": phase.get("inlier_ratio"),
        "phase_diameter_mm": phase.get("diameter_mm"),
        "phase_image_center_x_px": phase.get("image_center_x_px"),
        "phase_image_center_y_px": phase.get("image_center_y_px"),
        "phase_reasons": phase.get("reason_codes"),
        "rotation_state": rotation_view.get("state"),
        "rotation_result_ready": rotation_view.get("result_ready"),
        "rotation_result_valid": rotation_view.get("result_valid"),
        "rotation_should_rotate": rotation_view.get("should_rotate"),
        "rotation_delta_deg": rotation_view.get("rotation_delta_deg"),
        "rotation_gain_mm": rotation_view.get("gain_mm"),
        "rotation_model_quality": rotation_view.get("model_quality"),
        "rotation_stem_id": rotation_view.get("stem_id"),
        "rotation_session_id": rotation_view.get("session_id"),
        "rotation_model_hash": rotation_view.get("model_hash"),
        "rotation_reason_code": rotation_view.get("reason_code"),
        "rotation_reason_codes": rotation_view.get("reason_codes") or [],
        "rotation_valid_slice_count": rotation_diagnostics.get("valid_slice_count"),
        "rotation_evaluated_slice_count": rotation_diagnostics.get(
            "evaluated_slice_count"
        ),
        "rotation_fit_diagnostics": rotation_fit_diagnostics,
        "rotation_minimum_phase_coverage_ratio": rotation_diagnostics.get(
            "minimum_phase_coverage_ratio"
        ),
        "rotation_mean_center_residual_rms_mm": rotation_diagnostics.get(
            "mean_center_residual_rms_mm"
        ),
        "rotation_mean_width_residual_rms_mm": rotation_diagnostics.get(
            "mean_width_residual_rms_mm"
        ),
        "solver_state": solver_fit.get("solver_state"),
        "solver_observation_count": solver_fit.get("observation_count"),
        "solver_observed_phase_span_deg": solver_fit.get("observed_phase_span_deg"),
        "solver_confidence": solver_fit.get("confidence"),
        "solver_curvature_frozen": solver_fit.get("curvature_frozen"),
        "solver_decision_gate_reached": solver_gate.get("decision_gate_reached"),
        "solver_decision": solver_summary.get("decision"),
        "solver_target_phase_deg": solver_summary.get("target_phase_deg"),
        "solver_current_phase_deg": solver_summary.get("current_phase_deg"),
        "solver_correction_angle_deg": solver_summary.get("correction_angle_deg"),
        "solver_reasons": solver_summary.get("reason_codes"),
        "node_statuses": _node_statuses(state),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    unique_frames: dict[Any, dict[str, Any]] = {}
    for sample in samples:
        unique_frames[sample.get("frame_sequence")] = sample
    frames = list(unique_frames.values())
    node_timing_values: dict[str, list[float]] = defaultdict(list)
    for item in frames:
        statuses = item.get("node_statuses")
        if not isinstance(statuses, dict):
            continue
        for node_key, status in statuses.items():
            if not isinstance(status, dict):
                continue
            value = status.get("total_ms")
            if isinstance(value, int | float):
                node_timing_values[str(node_key)].append(float(value))
    node_timing_stats = {}
    for node_key, values in node_timing_values.items():
        ordered = sorted(values)
        p50_index = round((len(ordered) - 1) * 0.50)
        p95_index = round((len(ordered) - 1) * 0.95)
        node_timing_stats[node_key] = {
            "sample_count": len(ordered),
            "mean_ms": sum(ordered) / len(ordered),
            "p50_ms": ordered[p50_index],
            "p95_ms": ordered[p95_index],
            "max_ms": ordered[-1],
        }
    eligibility = Counter(str(item.get("eligibility")) for item in frames)
    reasons = Counter(
        reason
        for item in frames
        for reason in item.get("eligibility_reasons", [])
    )
    track_statuses = Counter(str(item.get("track_status")) for item in frames)
    process_zones = Counter(str(item.get("track_process_zone")) for item in frames)
    curvature_validity = Counter(str(item.get("curvature_valid")) for item in frames)
    curvature_reasons = Counter(
        str(reason)
        for item in frames
        for reason in (item.get("curvature_reasons") or [])
    )
    phase_modes = Counter(str(item.get("phase_mode")) for item in frames)
    phase_reasons = Counter(
        str(reason)
        for item in frames
        for reason in (item.get("phase_reasons") or [])
    )
    rotation_states = Counter(str(item.get("rotation_state")) for item in frames)
    rotation_reason_codes = Counter(
        str(item.get("rotation_reason_code")) for item in frames
    )
    rotation_reasons = Counter(
        str(reason)
        for item in frames
        for reason in (item.get("rotation_reason_codes") or [])
    )
    session_summaries: dict[str, dict[str, Any]] = {}
    for item in frames:
        session_id = item.get("rotation_session_id")
        if not session_id:
            continue
        summary = session_summaries.setdefault(
            str(session_id),
            {
                "first_frame_sequence": item.get("frame_sequence"),
                "last_frame_sequence": item.get("frame_sequence"),
                "states": Counter(),
                "reason_codes": Counter(),
                "reasons": Counter(),
                "result_ready": False,
                "result_valid": False,
                "should_rotate": False,
                "maximum_model_quality": 0.0,
                "maximum_valid_slice_count": 0,
                "maximum_evaluated_slice_count": 0,
                "fit_diagnostic_count": 0,
                "fit_valid_station_count": 0,
                "fit_reason_counts": Counter(),
                "maximum_station_observation_count": 0,
                "maximum_station_unique_phase_count": 0,
                "maximum_station_phase_span_deg": 0.0,
                "maximum_station_fit_quality": 0.0,
                "accepted_phase_min_deg": None,
                "accepted_phase_max_deg": None,
                "maximum_phase_confidence": 0.0,
                "rotation_delta_deg": None,
                "gain_mm": None,
            },
        )
        summary["last_frame_sequence"] = item.get("frame_sequence")
        summary["states"][str(item.get("rotation_state"))] += 1
        summary["reason_codes"][str(item.get("rotation_reason_code"))] += 1
        summary["reasons"].update(
            str(reason) for reason in (item.get("rotation_reason_codes") or [])
        )
        summary["result_ready"] = bool(
            summary["result_ready"] or item.get("rotation_result_ready")
        )
        summary["result_valid"] = bool(
            summary["result_valid"] or item.get("rotation_result_valid")
        )
        summary["should_rotate"] = bool(
            summary["should_rotate"] or item.get("rotation_should_rotate")
        )
        summary["maximum_model_quality"] = max(
            float(summary["maximum_model_quality"]),
            float(item.get("rotation_model_quality") or 0.0),
        )
        summary["maximum_valid_slice_count"] = max(
            int(summary["maximum_valid_slice_count"]),
            int(item.get("rotation_valid_slice_count") or 0),
        )
        summary["maximum_evaluated_slice_count"] = max(
            int(summary["maximum_evaluated_slice_count"]),
            int(item.get("rotation_evaluated_slice_count") or 0),
        )
        fit_diagnostics = item.get("rotation_fit_diagnostics") or []
        if len(fit_diagnostics) >= int(summary["fit_diagnostic_count"]):
            summary["fit_diagnostic_count"] = len(fit_diagnostics)
            summary["fit_valid_station_count"] = sum(
                bool(fit.get("valid"))
                for fit in fit_diagnostics
                if isinstance(fit, dict)
            )
            summary["fit_reason_counts"] = Counter(
                str(reason)
                for fit in fit_diagnostics
                if isinstance(fit, dict)
                for reason in (fit.get("reason_codes") or [])
            )
            summary["maximum_station_observation_count"] = max(
                (
                    int(fit.get("observation_count") or 0)
                    for fit in fit_diagnostics
                    if isinstance(fit, dict)
                ),
                default=0,
            )
            summary["maximum_station_unique_phase_count"] = max(
                (
                    int(fit.get("unique_phase_count") or 0)
                    for fit in fit_diagnostics
                    if isinstance(fit, dict)
                ),
                default=0,
            )
            summary["maximum_station_phase_span_deg"] = max(
                (
                    float(fit.get("phase_span_deg") or 0.0)
                    for fit in fit_diagnostics
                    if isinstance(fit, dict)
                ),
                default=0.0,
            )
            summary["maximum_station_fit_quality"] = max(
                (
                    float(fit.get("fit_quality") or 0.0)
                    for fit in fit_diagnostics
                    if isinstance(fit, dict)
                ),
                default=0.0,
            )
        if item.get("rotation_delta_deg") is not None:
            summary["rotation_delta_deg"] = item.get("rotation_delta_deg")
        if item.get("rotation_gain_mm") is not None:
            summary["gain_mm"] = item.get("rotation_gain_mm")
        summary["maximum_phase_confidence"] = max(
            float(summary["maximum_phase_confidence"]),
            float(item.get("phase_confidence") or 0.0),
        )
        accepted_phase = item.get("phase_cumulative_deg")
        if (
            item.get("eligibility") == "ACCEPTED"
            and item.get("curvature_valid") is True
            and item.get("phase_locked") is True
            and accepted_phase is not None
        ):
            value = float(accepted_phase)
            current_min = summary["accepted_phase_min_deg"]
            current_max = summary["accepted_phase_max_deg"]
            summary["accepted_phase_min_deg"] = (
                value if current_min is None else min(float(current_min), value)
            )
            summary["accepted_phase_max_deg"] = (
                value if current_max is None else max(float(current_max), value)
            )
    rendered_sessions = []
    for session_id, summary in session_summaries.items():
        phase_min = summary["accepted_phase_min_deg"]
        phase_max = summary["accepted_phase_max_deg"]
        rendered_sessions.append(
            {
                **summary,
                "session_id": session_id,
                "states": dict(summary["states"]),
                "reason_codes": dict(summary["reason_codes"]),
                "reasons": dict(summary["reasons"]),
                "fit_reason_counts": dict(summary["fit_reason_counts"]),
                "accepted_phase_span_deg": (
                    float(phase_max) - float(phase_min)
                    if phase_min is not None and phase_max is not None
                    else 0.0
                ),
            }
        )
    layer_sets = Counter("|".join(item.get("layers", [])) for item in frames)
    transitions = []
    previous: tuple[Any, ...] | None = None
    for item in frames:
        current = (
            item.get("track_id"),
            item.get("track_status"),
            item.get("eligibility"),
            tuple(item.get("eligibility_reasons", [])),
        )
        if current != previous:
            transitions.append(
                {
                    "frame_sequence": item.get("frame_sequence"),
                    "track_id": item.get("track_id"),
                    "track_status": item.get("track_status"),
                    "track_selection_reason": item.get("track_selection_reason"),
                    "candidate_count": item.get("track_candidate_count"),
                    "eligible_candidate_count": item.get("track_eligible_candidate_count"),
                    "process_zone": item.get("track_process_zone"),
                    "eligibility": item.get("eligibility"),
                    "eligibility_reasons": item.get("eligibility_reasons"),
                }
            )
            previous = current
    return {
        "poll_count": len(samples),
        "unique_frame_count": len(frames),
        "first_frame_sequence": frames[0].get("frame_sequence") if frames else None,
        "last_frame_sequence": frames[-1].get("frame_sequence") if frames else None,
        "node_timing_stats": node_timing_stats,
        "eligibility_counts": dict(eligibility),
        "eligibility_reason_counts": dict(reasons),
        "track_status_counts": dict(track_statuses),
        "process_zone_counts": dict(process_zones),
        "curvature_validity_counts": dict(curvature_validity),
        "curvature_reason_counts": dict(curvature_reasons),
        "phase_mode_counts": dict(phase_modes),
        "phase_reason_counts": dict(phase_reasons),
        "rotation_state_counts": dict(rotation_states),
        "rotation_reason_code_counts": dict(rotation_reason_codes),
        "rotation_reason_counts": dict(rotation_reasons),
        "rotation_result_ready_frame_count": sum(
            bool(item.get("rotation_result_ready")) for item in frames
        ),
        "rotation_result_valid_frame_count": sum(
            bool(item.get("rotation_result_valid")) for item in frames
        ),
        "rotation_sessions": rendered_sessions,
        "layer_set_counts": dict(layer_sets),
        "transitions": transitions,
    }


def main() -> int:
    args = _parser().parse_args()
    token = os.environ.get("YBB_RUNTIME_API_KEY", "").strip()
    if not token:
        print("YBB_RUNTIME_API_KEY is required", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")
    channel = urllib.parse.quote(args.channel_id, safe="")
    url = f"{base}/api/v1/runtime/channels/{channel}/live-view/latest?include_frame=false"
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(args.duration_seconds, 0.1)
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            samples.append(_sample(_request_json(url, token=token, insecure=args.insecure)))
            remaining = args.interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Runtime probe failed: {exc}", file=sys.stderr)
        return 1
    summary = _summary(samples)
    if args.timings_only:
        summary = {
            key: summary[key]
            for key in (
                "poll_count",
                "unique_frame_count",
                "first_frame_sequence",
                "last_frame_sequence",
                "node_timing_stats",
            )
        }
    payload = {"summary": summary, "samples": samples}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
