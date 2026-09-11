from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from cv_contracts.runtime import (
    RuntimeGraphEdge,
    RuntimeNodePort,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.engine import ExecutableNode, ExecutablePipelineGraph
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import ResultPayload, RuntimeExecutionResult
from cv_status import RuntimeMode, RuntimeNodeCategory
from realtime_executor.live import _OrderedStatefulNodeGate


@dataclass
class _TimedResultHandler:
    node_type: str
    intervals: dict[str, tuple[float, float, str]]
    delay_seconds: float = 0.0
    fail: bool = False

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeNodeExecutionError(
                code="parallel_branch_failed",
                message="synthetic branch failure",
            )
        completed = time.perf_counter()
        self.intervals[node.node_key] = (
            started,
            completed,
            threading.current_thread().name,
        )
        frame_sequence = context.metadata.get("live_frame_sequence")
        input_frame_sequences = sorted(
            int(payload.result["frame_sequence"])
            for payload in inputs.values()
            if isinstance(payload, ResultPayload)
            and isinstance(payload.result.get("frame_sequence"), int)
        )
        return NodeExecutionResult(
            outputs={
                "result": ResultPayload(
                    decision=True,
                    result={
                        "node_key": node.node_key,
                        "input_ports": sorted(inputs),
                        "frame_sequence": frame_sequence,
                        "input_frame_sequences": input_frame_sequences,
                    },
                )
            }
        )


@dataclass
class _OrderedFrameStageHandler:
    node_type: str
    stage: str
    intervals: dict[tuple[str, int], tuple[float, float]]
    state_order: dict[str, list[int]]
    delays: dict[int, float]
    stateful: bool = False

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        sequence = int(context.metadata["live_frame_sequence"])
        started = time.perf_counter()
        if self.stateful:
            self.state_order.setdefault(self.stage, []).append(sequence)
            state = context.state.setdefault(node.node_key, {})
            state["last_sequence"] = sequence
        time.sleep(self.delays.get(sequence, 0.0))
        self.intervals[(self.stage, sequence)] = (started, time.perf_counter())
        return NodeExecutionResult(
            outputs={
                "result": ResultPayload(
                    decision=True,
                    result={"frame_sequence": sequence, "stage": self.stage},
                )
            }
        )


def test_selected_dag_branches_overlap_and_join_frame_synchronously() -> None:
    intervals: dict[str, tuple[float, float, str]] = {}
    executable = _parallel_graph(intervals)

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_parallel_stem_measurement",
            pipeline_version_id="plv_parallel_stem_measurement",
            mode=RuntimeMode.REALTIME,
            metadata={
                "parallel_node_keys": [
                    "eligibility",
                    "geometry",
                    "curvature",
                    "phase",
                ],
                "parallel_node_max_workers": 2,
                "live_frame_sequence": 41,
            },
        )
    )

    assert result.status == "completed", result.errors
    assert result.timing["node_execution_mode"] == "parallel_dag"
    assert result.timing["parallel_worker_count"] == 2
    assert result.timing["parallel_node_keys"] == [
        "eligibility",
        "geometry",
        "curvature",
        "phase",
    ]
    assert list(result.node_outputs) == [
        "source",
        "eligibility",
        "geometry",
        "curvature",
        "phase",
        "join",
    ]
    assert result.node_outputs["join"]["result"]["result"]["input_ports"] == [
        "curvature",
        "phase",
    ]
    assert result.node_outputs["join"]["result"]["result"]["input_frame_sequences"] == [41, 41]
    assert _overlap(intervals["eligibility"], intervals["geometry"])
    assert _overlap(intervals["curvature"], intervals["phase"])
    assert intervals["eligibility"][2].startswith("runtime-node-branch")
    assert intervals["phase"][2].startswith("runtime-node-branch")


def test_parallel_execution_does_not_reuse_outputs_from_the_previous_frame() -> None:
    intervals: dict[str, tuple[float, float, str]] = {}
    executable = _parallel_graph(intervals)
    metadata = {
        "parallel_node_keys": ["eligibility", "geometry", "curvature", "phase"],
        "parallel_node_max_workers": 2,
        "live_frame_sequence": 71,
    }

    first = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_parallel_frame_71",
            pipeline_version_id="plv_parallel_stem_measurement",
            mode=RuntimeMode.REALTIME,
            metadata=metadata,
        )
    )
    second = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_parallel_frame_72",
            pipeline_version_id="plv_parallel_stem_measurement",
            mode=RuntimeMode.REALTIME,
            metadata={**metadata, "live_frame_sequence": 72},
        )
    )

    assert first.node_outputs["join"]["result"]["result"]["input_frame_sequences"] == [71, 71]
    assert second.node_outputs["join"]["result"]["result"]["input_frame_sequences"] == [72, 72]


def test_graph_execution_remains_sequential_without_explicit_parallel_selection() -> None:
    intervals: dict[str, tuple[float, float, str]] = {}
    executable = _parallel_graph(intervals)

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_sequential_fallback",
            pipeline_version_id="plv_parallel_stem_measurement",
            mode=RuntimeMode.REALTIME,
        )
    )

    assert result.status == "completed"
    assert result.timing["node_execution_mode"] == "sequential"
    assert result.timing["parallel_node_keys"] == []
    assert not _overlap(intervals["eligibility"], intervals["geometry"])
    assert not _overlap(intervals["curvature"], intervals["phase"])


def test_parallel_branch_failure_prevents_join_and_fails_closed() -> None:
    intervals: dict[str, tuple[float, float, str]] = {}
    executable = _parallel_graph(intervals, failing_node="geometry")

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_parallel_stem_failure",
            pipeline_version_id="plv_parallel_stem_measurement",
            mode=RuntimeMode.REALTIME,
            metadata={
                "parallel_node_keys": [
                    "eligibility",
                    "geometry",
                    "curvature",
                    "phase",
                ],
                "parallel_node_max_workers": 2,
            },
        )
    )

    assert result.status == "failed"
    assert result.errors[0]["code"] == "parallel_branch_failed"
    assert result.errors[0]["node_key"] == "geometry"
    assert "join" not in result.node_outputs


def test_cross_frame_stateful_nodes_are_ordered_independently() -> None:
    intervals: dict[tuple[str, int], tuple[float, float]] = {}
    state_order: dict[str, list[int]] = {}
    prefix = _node("prefix", "source.prefix_test", inputs=(), outputs=("result",))
    tracker = _node(
        "tracker",
        "analysis.tracker_test",
        inputs=("input",),
        outputs=("result",),
    )
    slow_geometry = _node(
        "geometry",
        "analysis.geometry_test",
        inputs=("input",),
        outputs=("result",),
    )
    builder = _node(
        "builder",
        "analysis.builder_test",
        inputs=("input",),
        outputs=("result",),
    )
    edges = [
        _edge("prefix", "result", "tracker", "input"),
        _edge("tracker", "result", "geometry", "input"),
        _edge("geometry", "result", "builder", "input"),
    ]
    executable = ExecutablePipelineGraph(
        pipeline_version_id="plv_ordered_overlap",
        graph=RuntimePipelineGraph(
            nodes=[prefix, tracker, slow_geometry, builder],
            edges=edges,
        ),
        nodes=[
            ExecutableNode(
                spec=prefix,
                handler=_OrderedFrameStageHandler(
                    node_type=prefix.node_type,
                    stage="prefix",
                    intervals=intervals,
                    state_order=state_order,
                    delays={1: 0.05, 2: 0.01},
                ),
            ),
            ExecutableNode(
                spec=tracker,
                handler=_OrderedFrameStageHandler(
                    node_type=tracker.node_type,
                    stage="tracker",
                    intervals=intervals,
                    state_order=state_order,
                    delays={1: 0.01, 2: 0.01},
                    stateful=True,
                ),
            ),
            ExecutableNode(
                spec=slow_geometry,
                handler=_OrderedFrameStageHandler(
                    node_type=slow_geometry.node_type,
                    stage="geometry",
                    intervals=intervals,
                    state_order=state_order,
                    delays={1: 0.1, 2: 0.01},
                ),
            ),
            ExecutableNode(
                spec=builder,
                handler=_OrderedFrameStageHandler(
                    node_type=builder.node_type,
                    stage="builder",
                    intervals=intervals,
                    state_order=state_order,
                    delays={1: 0.01, 2: 0.01},
                    stateful=True,
                ),
            ),
        ],
        incoming_edges={
            "tracker": [edges[0]],
            "geometry": [edges[1]],
            "builder": [edges[2]],
        },
    )
    gate = _OrderedStatefulNodeGate(("tracker", "builder"))
    shared_state: dict[str, object] = {}
    gate.register(1)
    gate.register(2)

    def execute(sequence: int) -> RuntimeExecutionResult:
        try:
            return executable.execute(
                RuntimeExecutionContext(
                    correlation_id=f"ordered_{sequence}",
                    pipeline_version_id="plv_ordered_overlap",
                    mode=RuntimeMode.REALTIME,
                    state=shared_state,
                    metadata={
                        "live_frame_sequence": sequence,
                        "ordered_stateful_node_gate": gate,
                        "ordered_stateful_node_keys": ("tracker", "builder"),
                    },
                )
            )
        finally:
            gate.complete_frame(sequence)

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(execute, 1)
        newer = executor.submit(execute, 2)
        older_result = older.result(timeout=2)
        newer_result = newer.result(timeout=2)

    assert state_order == {"tracker": [1, 2], "builder": [1, 2]}
    assert _overlap_without_thread(intervals[("prefix", 1)], intervals[("prefix", 2)])
    assert intervals[("tracker", 2)][0] < intervals[("geometry", 1)][1]
    assert intervals[("geometry", 2)][1] < intervals[("geometry", 1)][1]
    assert older_result.timing["ordered_stateful_execution"] is True
    assert newer_result.timing["ordered_stateful_execution"] is True
    assert newer_result.timing["ordered_stateful_wait_ms"] >= 20
    assert newer_result.timing["ordered_stateful_mode"] == "per_node"
    assert newer_result.timing["ordered_stateful_wait_by_node_ms"]["builder"] >= 20
    assert shared_state["tracker"] == {"last_sequence": 2}
    assert shared_state["builder"] == {"last_sequence": 2}


def test_parallel_branches_keep_independent_cross_frame_node_order() -> None:
    intervals: dict[tuple[str, int], tuple[float, float]] = {}
    state_order: dict[str, list[int]] = {}
    prefix = _node("prefix", "source.prefix_test", inputs=(), outputs=("result",))
    curvature = _node(
        "curvature",
        "analysis.curvature_test",
        inputs=("input",),
        outputs=("result",),
    )
    phase = _node(
        "phase",
        "analysis.phase_test",
        inputs=("input",),
        outputs=("result",),
    )
    join = _node(
        "join",
        "analysis.join_test",
        inputs=("curvature", "phase"),
        outputs=("result",),
    )
    edges = [
        _edge("prefix", "result", "curvature", "input"),
        _edge("prefix", "result", "phase", "input"),
        _edge("curvature", "result", "join", "curvature"),
        _edge("phase", "result", "join", "phase"),
    ]
    nodes = [prefix, curvature, phase, join]
    executable = ExecutablePipelineGraph(
        pipeline_version_id="plv_ordered_parallel_branches",
        graph=RuntimePipelineGraph(nodes=nodes, edges=edges),
        nodes=[
            ExecutableNode(
                spec=node,
                handler=_OrderedFrameStageHandler(
                    node_type=node.node_type,
                    stage=node.node_key,
                    intervals=intervals,
                    state_order=state_order,
                    delays=(
                        {1: 0.05, 2: 0.01}
                        if node.node_key == "prefix"
                        else {1: 0.04, 2: 0.01}
                    ),
                    stateful=node.node_key in {"curvature", "phase"},
                ),
            )
            for node in nodes
        ],
        incoming_edges={
            "curvature": [edges[0]],
            "phase": [edges[1]],
            "join": edges[2:],
        },
    )
    gate = _OrderedStatefulNodeGate(("curvature", "phase"))
    shared_state: dict[str, object] = {}
    gate.register(1)
    gate.register(2)

    def execute(sequence: int) -> RuntimeExecutionResult:
        try:
            return executable.execute(
                RuntimeExecutionContext(
                    correlation_id=f"ordered_parallel_{sequence}",
                    pipeline_version_id="plv_ordered_parallel_branches",
                    mode=RuntimeMode.REALTIME,
                    state=shared_state,
                    metadata={
                        "live_frame_sequence": sequence,
                        "ordered_stateful_node_gate": gate,
                        "ordered_stateful_node_keys": ("curvature", "phase"),
                        "parallel_node_keys": ("curvature", "phase"),
                        "parallel_node_max_workers": 2,
                    },
                )
            )
        finally:
            gate.complete_frame(sequence)

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(execute, 1)
        newer = executor.submit(execute, 2)
        older_result = older.result(timeout=2)
        newer_result = newer.result(timeout=2)

    assert older_result.status == "completed"
    assert newer_result.status == "completed"
    assert state_order == {"curvature": [1, 2], "phase": [1, 2]}
    assert _overlap_without_thread(
        intervals[("curvature", 1)], intervals[("phase", 1)]
    )
    assert _overlap_without_thread(
        intervals[("curvature", 2)], intervals[("phase", 2)]
    )
    assert set(newer_result.timing["ordered_stateful_wait_by_node_ms"]) == {
        "curvature",
        "phase",
    }


def _parallel_graph(
    intervals: dict[str, tuple[float, float, str]],
    *,
    failing_node: str | None = None,
) -> ExecutablePipelineGraph:
    nodes = [
        _node("source", "source.parallel_test", inputs=(), outputs=("result",)),
        _node(
            "eligibility",
            "analysis.stem_measurement_eligibility",
            inputs=("source",),
            outputs=("result",),
        ),
        _node(
            "geometry",
            "analysis.stem_mask_geometry",
            inputs=("source",),
            outputs=("result",),
        ),
        _node(
            "curvature",
            "analysis.stem_horizontal_curvature",
            inputs=("eligibility",),
            outputs=("result",),
        ),
        _node(
            "phase",
            "analysis.stem_surface_phase",
            inputs=("geometry",),
            outputs=("result",),
        ),
        _node(
            "join",
            "analysis.parallel_join",
            inputs=("curvature", "phase"),
            outputs=("result",),
        ),
    ]
    edges = [
        _edge("source", "result", "eligibility", "source"),
        _edge("source", "result", "geometry", "source"),
        _edge("eligibility", "result", "curvature", "eligibility"),
        _edge("geometry", "result", "phase", "geometry"),
        _edge("curvature", "result", "join", "curvature"),
        _edge("phase", "result", "join", "phase"),
    ]
    graph = RuntimePipelineGraph(nodes=nodes, edges=edges)
    incoming: dict[str, list[RuntimeGraphEdge]] = {}
    for edge in edges:
        incoming.setdefault(edge.to_node_key, []).append(edge)
    executable_nodes = [
        ExecutableNode(
            spec=node,
            handler=_TimedResultHandler(
                node_type=node.node_type,
                intervals=intervals,
                delay_seconds=(0.05 if node.node_key in _PARALLEL_KEYS else 0.0),
                fail=node.node_key == failing_node,
            ),
        )
        for node in nodes
    ]
    return ExecutablePipelineGraph(
        pipeline_version_id="plv_parallel_stem_measurement",
        graph=graph,
        nodes=executable_nodes,
        incoming_edges=incoming,
    )


_PARALLEL_KEYS = {"eligibility", "geometry", "curvature", "phase"}


def _node(
    node_key: str,
    node_type: str,
    *,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
) -> RuntimeNodeSpec:
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=(
            RuntimeNodeCategory.SOURCE
            if node_type.startswith("source.")
            else RuntimeNodeCategory.ANALYSIS
        ),
        input_ports=[RuntimeNodePort(key=key, data_type="result", required=True) for key in inputs],
        output_ports=[
            RuntimeNodePort(key=key, data_type="result", required=True) for key in outputs
        ],
    )


def _edge(
    from_node_key: str,
    from_port: str,
    to_node_key: str,
    to_port: str,
) -> RuntimeGraphEdge:
    return RuntimeGraphEdge(
        from_node_key=from_node_key,
        from_port=from_port,
        to_node_key=to_node_key,
        to_port=to_port,
    )


def _overlap(
    left: tuple[float, float, str],
    right: tuple[float, float, str],
) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def _overlap_without_thread(
    left: tuple[float, float],
    right: tuple[float, float],
) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])
