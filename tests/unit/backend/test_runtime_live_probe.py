from __future__ import annotations

from scripts.diagnostics.runtime_live_probe import _sample, _summary


def test_runtime_live_probe_extracts_tracker_and_solver_transitions() -> None:
    document = {
        "state": {
            "actual_state": "running",
            "available": True,
            "stale": False,
            "updated_at": "2026-08-12T10:00:00Z",
            "frame": {
                "sequence": 42,
                "source_timestamp": "2026-08-12T10:00:00Z",
            },
            "layers_available": ["stem_tracking_status", "stem_capture_eligibility"],
            "overlay": [
                {
                    "layer": "stem_tracking_status",
                    "metadata": {
                        "track_id": "trk_1",
                        "status": "active",
                        "selection_reason": "temporal_continuity",
                        "candidate_count": 2,
                        "eligible_candidate_count": 2,
                        "process_zone": "longitudinal_conveyor",
                        "track_age_frames": 7,
                    },
                },
                {
                    "layer": "stem_capture_eligibility",
                    "metadata": {"status": "ACCEPTED", "reason_codes": []},
                },
                {
                    "layer": "stem_curvature_status",
                    "metadata": {
                        "valid": True,
                        "observation_state": "observed",
                        "reason_codes": [],
                    },
                },
                {
                    "layer": "stem_curvature_phase_diagnostic",
                    "metadata": {"solver_state": "FITTED"},
                },
                {
                    "layer": "stem_curvature_phase_diagnostic",
                    "metadata": {"decision_gate_reached": False},
                },
                {
                    "layer": "stem_curvature_phase_diagnostic",
                    "metadata": {
                        "decision": None,
                        "reason_codes": ["DECISION_GATE_NOT_REACHED"],
                        "diagnostic_graph": {},
                    },
                },
            ],
            "diagnostics": {
                "pipeline_node_timings": [
                    {"node_key": "active_stem", "status": "completed"}
                ]
            },
        }
    }

    sample = _sample(document)
    summary = _summary([sample, sample])

    assert sample["track_id"] == "trk_1"
    assert sample["track_status"] == "active"
    assert sample["track_candidate_count"] == 2
    assert sample["track_process_zone"] == "longitudinal_conveyor"
    assert sample["curvature_valid"] is True
    assert sample["solver_state"] == "FITTED"
    assert sample["solver_decision_gate_reached"] is False
    assert summary["unique_frame_count"] == 1
    assert summary["eligibility_counts"] == {"ACCEPTED": 1}
