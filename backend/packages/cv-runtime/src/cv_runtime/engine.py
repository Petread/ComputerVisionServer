from __future__ import annotations

import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import Any, Protocol, cast

from cv_contracts.runtime import RuntimeGraphEdge, RuntimeNodeSpec, RuntimePipelineGraph
from cv_status import RuntimeNodeCategory
from cv_utils import utc_now_iso

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeGraphBuildError, RuntimeNodeError, RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult, RuntimeNodeHandler
from cv_runtime.loader import load_runtime_handler
from cv_runtime.payloads import (
    AnomalyPayload,
    AnyRuntimePayload,
    BoardCutConstraintsPayload,
    BoardCutDecisionPayload,
    CaptureIntentsPayload,
    ClassificationPayload,
    ClearanceZonesPayload,
    CoordinateFramePayload,
    CvEdgesPayload,
    DetectionPayload,
    DisplayOutputPayload,
    EventsPayload,
    FeatureMeasurementsPayload,
    FramePayload,
    FreeRegionPayload,
    MeasurementsPayload,
    OverlayPayload,
    PositionsPayload,
    QualityDecisionPayload,
    ReferenceEdgesPayload,
    RegionSetPayload,
    ResultPayload,
    RoiSetPayload,
    RuntimeExecutionResult,
    SegmentPayload,
    SiemensSpsWriteStatusPayload,
    StemCurvaturePhaseSolutionPayload,
    StemHorizontalCurvaturePayload,
    StemSurfacePhasePayload,
    SynchronizedFrameSetPayload,
)
from cv_runtime.projections import RuntimeProjectionEvaluation, evaluate_output_projections


@dataclass(frozen=True)
class ExecutableNode:
    spec: RuntimeNodeSpec
    handler: RuntimeNodeHandler


@dataclass(frozen=True)
class _NodeExecutionOutcome:
    result: NodeExecutionResult | None
    materialized_outputs: dict[str, object] | None
    timing: dict[str, Any]
    error: RuntimeNodeError | None = None


class _OrderedNodeGate(Protocol):
    def enter(self, sequence: int, node_key: str) -> float: ...

    def complete_node(self, sequence: int, node_key: str) -> None: ...


@dataclass
class _OrderedStatefulNodes:
    """Serialize each configured stateful node independently across live frames.

    A later frame may advance past its tracker while an older frame still calculates unrelated
    geometry, but it can never execute the same stateful node before that node completed for the
    older frame.  The gate remains an executor concern supplied through metadata; the generic
    engine only brackets the selected handlers with enter/complete calls.
    """

    gate: _OrderedNodeGate | None
    frame_sequence: int | None
    node_keys: frozenset[str]
    entered_node_keys: set[str] = field(default_factory=set)
    completed_node_keys: set[str] = field(default_factory=set)
    wait_ms_by_node: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_context(cls, context: RuntimeExecutionContext) -> _OrderedStatefulNodes:
        gate = context.metadata.get("ordered_stateful_node_gate")
        raw_sequence = context.metadata.get("live_frame_sequence")
        try:
            frame_sequence = int(raw_sequence) if raw_sequence is not None else None
        except (TypeError, ValueError):
            frame_sequence = None
        raw_keys = context.metadata.get("ordered_stateful_node_keys")
        node_keys = (
            frozenset(str(item) for item in raw_keys if isinstance(item, str))
            if isinstance(raw_keys, list | tuple | set | frozenset)
            else frozenset()
        )
        enter = getattr(gate, "enter", None)
        complete_node = getattr(gate, "complete_node", None)
        if (
            frame_sequence is None
            or not node_keys
            or not callable(enter)
            or not callable(complete_node)
        ):
            return cls(gate=None, frame_sequence=None, node_keys=frozenset())
        return cls(
            gate=cast(_OrderedNodeGate, gate),
            frame_sequence=frame_sequence,
            node_keys=node_keys,
        )

    @property
    def enabled(self) -> bool:
        return self.gate is not None and self.frame_sequence is not None and bool(self.node_keys)

    def enter(self, node_key: str) -> bool:
        if node_key not in self.node_keys or not self.enabled:
            return False
        assert self.gate is not None and self.frame_sequence is not None
        waited = self.gate.enter(self.frame_sequence, node_key)
        try:
            wait_ms = max(float(waited), 0.0)
        except (TypeError, ValueError):
            wait_ms = 0.0
        with self._lock:
            self.entered_node_keys.add(node_key)
            self.wait_ms_by_node[node_key] = wait_ms
        return True

    def complete(self, node_key: str, *, entered: bool = True) -> None:
        if node_key not in self.node_keys or not self.enabled:
            return
        assert self.gate is not None and self.frame_sequence is not None
        with self._lock:
            if node_key in self.completed_node_keys:
                return
            self.completed_node_keys.add(node_key)
        self.gate.complete_node(self.frame_sequence, node_key)
        if not entered:
            with self._lock:
                self.wait_ms_by_node.setdefault(node_key, 0.0)

    @property
    def entered(self) -> bool:
        with self._lock:
            return bool(self.entered_node_keys)

    @property
    def wait_ms(self) -> float:
        with self._lock:
            return max(self.wait_ms_by_node.values(), default=0.0)

    def wait_timing(self) -> dict[str, float]:
        with self._lock:
            return {
                node_key: round(self.wait_ms_by_node.get(node_key, 0.0), 3)
                for node_key in sorted(self.node_keys)
                if node_key in self.wait_ms_by_node
            }


@dataclass(frozen=True)
class ExecutablePipelineGraph:
    pipeline_version_id: str
    graph: RuntimePipelineGraph
    nodes: list[ExecutableNode]
    incoming_edges: dict[str, list[RuntimeGraphEdge]]

    @classmethod
    def build(
        cls,
        *,
        pipeline_version_id: str,
        graph: RuntimePipelineGraph,
        handler_refs: dict[str, str],
    ) -> ExecutablePipelineGraph:
        order = _topological_order(graph)
        nodes_by_key = {node.node_key: node for node in graph.nodes}
        executable_nodes: list[ExecutableNode] = []
        for node_key in order:
            spec = nodes_by_key[node_key]
            handler_ref = handler_refs.get(spec.node_type)
            if not handler_ref:
                raise RuntimeGraphBuildError(
                    f"No handler reference is registered for {spec.node_type}."
                )
            executable_nodes.append(
                ExecutableNode(
                    spec=spec,
                    handler=load_runtime_handler(handler_ref, expected_node_type=spec.node_type),
                )
            )
        incoming_edges: dict[str, list[RuntimeGraphEdge]] = defaultdict(list)
        for edge in graph.edges:
            incoming_edges[edge.to_node_key].append(edge)
        return cls(
            pipeline_version_id=pipeline_version_id,
            graph=graph,
            nodes=executable_nodes,
            incoming_edges=dict(incoming_edges),
        )

    def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        started_at = utc_now_iso()
        started = time.perf_counter()
        ordered_stateful_nodes = _OrderedStatefulNodes.from_context(context)
        errors: list[RuntimeNodeError] = []
        values: dict[tuple[str, str], object] = {}
        node_outputs: dict[str, dict[str, object]] = {}
        executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]] = []
        skipped_nodes: set[str] = set()
        node_timings: list[dict[str, Any]] = []
        materialize_frame_node_outputs = bool(
            context.metadata.get("materialize_frame_node_outputs", True)
        )
        parallel_node_keys = _parallel_node_keys(context, self.nodes)
        parallel_worker_count = _parallel_worker_count(context, parallel_node_keys)
        if len(parallel_node_keys) >= 2 and parallel_worker_count >= 2:
            (
                errors,
                values,
                node_outputs,
                executed_payloads,
                skipped_nodes,
                node_timings,
            ) = self._execute_with_parallel_branches(
                context,
                parallel_node_keys=parallel_node_keys,
                max_workers=parallel_worker_count,
                materialize_frame_node_outputs=materialize_frame_node_outputs,
                ordered_stateful_nodes=ordered_stateful_nodes,
            )
        else:
            parallel_node_keys = set()
            parallel_worker_count = 1
            for executable in self.nodes:
                node = executable.spec
                node_started = time.perf_counter()
                incoming_edges = self.incoming_edges.get(node.node_key, [])
                if any(edge.from_node_key in skipped_nodes for edge in incoming_edges):
                    ordered_stateful_nodes.complete(node.node_key, entered=False)
                    skipped_nodes.add(node.node_key)
                    node_timings.append(
                        _node_timing(
                            node,
                            status="skipped",
                            started_perf=node_started,
                            skip_reason="upstream_skipped",
                        )
                    )
                    continue
                collect_started = time.perf_counter()
                inputs = _collect_inputs(node, incoming_edges, values)
                input_collect_ms = _elapsed_ms(collect_started)
                missing = _missing_required_inputs(node, inputs)
                if missing:
                    ordered_stateful_nodes.complete(node.node_key, entered=False)
                    skipped_nodes.add(node.node_key)
                    node_timings.append(
                        _node_timing(
                            node,
                            status="skipped",
                            started_perf=node_started,
                            input_collect_ms=input_collect_ms,
                            missing_inputs=missing,
                            skip_reason="missing_required_inputs",
                        )
                    )
                    continue
                entered_ordered_node = ordered_stateful_nodes.enter(node.node_key)
                execute_started = time.perf_counter()
                try:
                    result = executable.handler.execute(context, node, inputs)
                except RuntimeNodeExecutionError as exc:
                    error = exc.error.model_copy(
                        update={"node_key": node.node_key, "node_type": node.node_type}
                    )
                    errors.append(error)
                    node_timings.append(
                        _node_timing(
                            node,
                            status="failed",
                            started_perf=node_started,
                            input_collect_ms=input_collect_ms,
                            execute_ms=_elapsed_ms(execute_started),
                            error_code=error.code,
                            error_metadata=error.metadata,
                        )
                    )
                    break
                except Exception as exc:
                    errors.append(
                        RuntimeNodeError(
                            code="node_execution_failed",
                            message=str(exc),
                            node_key=node.node_key,
                            node_type=node.node_type,
                            retryable=False,
                        )
                    )
                    node_timings.append(
                        _node_timing(
                            node,
                            status="failed",
                            started_perf=node_started,
                            input_collect_ms=input_collect_ms,
                            execute_ms=_elapsed_ms(execute_started),
                            error_code="node_execution_failed",
                        )
                    )
                    break
                finally:
                    if entered_ordered_node:
                        ordered_stateful_nodes.complete(node.node_key)
                execute_ms = _elapsed_ms(execute_started)
                if not result.outputs:
                    skipped_nodes.add(node.node_key)
                    node_timings.append(
                        _node_timing(
                            node,
                            status="skipped",
                            started_perf=node_started,
                            input_collect_ms=input_collect_ms,
                            execute_ms=execute_ms,
                            output_count=0,
                            output_ports=[],
                            skip_reason="empty_outputs",
                        )
                    )
                    continue
                materialize_started = time.perf_counter()
                node_outputs[node.node_key] = {}
                for port, payload in result.outputs.items():
                    node_outputs[node.node_key][port] = _node_output_payload(
                        payload,
                        materialize_frame_outputs=materialize_frame_node_outputs,
                    )
                output_materialize_ms = _elapsed_ms(materialize_started)
                for port, payload in result.outputs.items():
                    values[(node.node_key, port)] = payload
                    executed_payloads.append((node, port, payload))
                node_timings.append(
                    _node_timing(
                        node,
                        status="completed",
                        started_perf=node_started,
                        input_collect_ms=input_collect_ms,
                        execute_ms=execute_ms,
                        output_materialize_ms=output_materialize_ms,
                        output_count=len(result.outputs),
                        output_ports=list(result.outputs),
                    )
                )

        completed_at = utc_now_iso()
        projection_evaluation = evaluate_output_projections(
            self.graph.nodes,
            executed_payloads,
            context,
        )
        final_response = _final_response(executed_payloads, projection_evaluation)
        http_trigger_responses = _http_trigger_responses(executed_payloads, projection_evaluation)
        persisted_results = _persisted_results(executed_payloads, projection_evaluation)
        internal_events = _internal_events(executed_payloads)
        display_outputs = [
            _payload_entry(node, port, payload)
            for node, port, payload in executed_payloads
            if isinstance(payload, DisplayOutputPayload)
        ]
        if context.metadata.get("materialize_live_display_frame") and not display_outputs:
            default_display_output = _default_display_output(executed_payloads)
            if default_display_output is not None:
                display_outputs.append(default_display_output)
        status = "failed" if errors else ("skipped" if not values else "completed")
        execution_result = RuntimeExecutionResult(
            status=status,
            channel_id=context.channel_id,
            pipeline_version_id=self.pipeline_version_id,
            runtime_execution_id=context.runtime_execution_id,
            correlation_id=context.correlation_id,
            started_at=started_at,
            completed_at=completed_at,
            node_outputs=node_outputs,
            raw_inference_outputs=[
                _raw_inference_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(
                    payload,
                    DetectionPayload | ClassificationPayload | SegmentPayload | AnomalyPayload,
                )
            ],
            normalized_inference_outputs=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(
                    payload,
                    DetectionPayload | ClassificationPayload | SegmentPayload | AnomalyPayload,
                )
            ],
            derived_outputs=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(
                    payload,
                    PositionsPayload
                    | MeasurementsPayload
                    | ReferenceEdgesPayload
                    | CvEdgesPayload
                    | CoordinateFramePayload
                    | FeatureMeasurementsPayload
                    | ClearanceZonesPayload
                    | FreeRegionPayload
                    | BoardCutConstraintsPayload
                    | BoardCutDecisionPayload
                    | QualityDecisionPayload
                    | StemCurvaturePhaseSolutionPayload
                    | StemHorizontalCurvaturePayload
                    | StemSurfacePhasePayload
                    | EventsPayload
                    | ResultPayload,
                )
                and node.node_category
                in {RuntimeNodeCategory.ANALYSIS, RuntimeNodeCategory.COMPUTER_VISION}
            ],
            image_contexts=_image_contexts(executed_payloads),
            object_sets=_object_sets(executed_payloads),
            enriched_object_annotations=_enriched_object_annotations(executed_payloads),
            reference_edges=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, ReferenceEdgesPayload)
            ],
            cv_edges=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, CvEdgesPayload)
            ],
            coordinate_frames=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, CoordinateFramePayload)
            ],
            feature_measurements=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, FeatureMeasurementsPayload)
            ],
            clearance_zones=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, ClearanceZonesPayload)
            ],
            free_regions=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, FreeRegionPayload)
            ],
            synchronized_frame_sets=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, SynchronizedFrameSetPayload)
            ],
            board_cut_constraints=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, BoardCutConstraintsPayload)
            ],
            board_cut_decisions=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, BoardCutDecisionPayload)
            ],
            quality_decisions=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, QualityDecisionPayload)
            ],
            overlay_primitives=_overlay_primitives(executed_payloads),
            overlay_outputs=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, OverlayPayload)
            ],
            display_outputs=display_outputs,
            capture_intents=[
                {
                    **_payload_entry(node, port, payload),
                    "intent": intent.model_dump(mode="json"),
                }
                for node, port, payload in executed_payloads
                if isinstance(payload, CaptureIntentsPayload)
                for intent in payload.intents
            ],
            http_trigger_responses=http_trigger_responses,
            persisted_results=persisted_results,
            internal_events=internal_events,
            plc_write_outputs=[
                _payload_entry(node, port, payload)
                for node, port, payload in executed_payloads
                if isinstance(payload, SiemensSpsWriteStatusPayload)
            ],
            projected_results=(
                projection_evaluation.projections if projection_evaluation is not None else []
            ),
            final_response=final_response,
            timing={
                "total_ms": _elapsed_ms(started),
                "node_timings_ms": node_timings,
                "stem_stage_timings_ms": dict(context.stem_stage_timings_ms),
                "slowest_node": _slowest_node_timing(node_timings),
                "inference_timings_ms": _inference_timings(executed_payloads),
                "node_execution_mode": ("parallel_dag" if parallel_node_keys else "sequential"),
                "parallel_node_keys": [
                    executable.spec.node_key
                    for executable in self.nodes
                    if executable.spec.node_key in parallel_node_keys
                ],
                "parallel_worker_count": parallel_worker_count,
                "ordered_stateful_execution": ordered_stateful_nodes.enabled,
                "ordered_stateful_mode": (
                    "per_node" if ordered_stateful_nodes.enabled else None
                ),
                "ordered_stateful_turn_entered": ordered_stateful_nodes.entered,
                "ordered_stateful_wait_ms": round(ordered_stateful_nodes.wait_ms, 3),
                "ordered_stateful_wait_by_node_ms": ordered_stateful_nodes.wait_timing(),
                "ordered_stateful_node_keys": [
                    executable.spec.node_key
                    for executable in self.nodes
                    if executable.spec.node_key in ordered_stateful_nodes.node_keys
                ],
            },
            errors=[error.model_dump(mode="json") for error in errors],
        )
        execution_result.set_recording_frame(_intrinsically_undistorted_frame(executed_payloads))
        return execution_result

    def _execute_with_parallel_branches(
        self,
        context: RuntimeExecutionContext,
        *,
        parallel_node_keys: set[str],
        max_workers: int,
        materialize_frame_node_outputs: bool,
        ordered_stateful_nodes: _OrderedStatefulNodes,
    ) -> tuple[
        list[RuntimeNodeError],
        dict[tuple[str, str], object],
        dict[str, dict[str, object]],
        list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
        set[str],
        list[dict[str, Any]],
    ]:
        """Execute only explicitly selected independent DAG branches concurrently.

        This scheduler overlaps handlers from the same frame after all of their declared input
        edges are complete. If the outer realtime executor also overlaps complete frames, each
        configured stateful handler enters and completes its own cross-frame turn. Selected state
        dictionaries are created before worker threads start, so node-local state registration
        does not race within a frame.
        """

        executable_by_key = {item.spec.node_key: item for item in self.nodes}
        order = [item.spec.node_key for item in self.nodes]
        order_index = {node_key: index for index, node_key in enumerate(order)}
        predecessors = {
            node_key: {edge.from_node_key for edge in self.incoming_edges.get(node_key, [])}
            for node_key in order
        }
        values: dict[tuple[str, str], object] = {}
        skipped_nodes: set[str] = set()
        finished_nodes: set[str] = set()
        remaining_nodes = set(order)
        outcomes: dict[str, _NodeExecutionOutcome] = {}
        errors_by_key: dict[str, RuntimeNodeError] = {}
        running: dict[Future[_NodeExecutionOutcome], str] = {}
        aborted = False

        def accept(node_key: str, outcome: _NodeExecutionOutcome) -> None:
            nonlocal aborted
            outcomes[node_key] = outcome
            finished_nodes.add(node_key)
            if outcome.error is not None:
                errors_by_key[node_key] = outcome.error
                aborted = True
                return
            if outcome.result is None or not outcome.result.outputs:
                skipped_nodes.add(node_key)
                return
            for port, payload in outcome.result.outputs.items():
                values[(node_key, port)] = payload

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="runtime-node-branch",
        ) as executor:
            while remaining_nodes or running:
                ready = [
                    node_key
                    for node_key in order
                    if node_key in remaining_nodes
                    and predecessors[node_key].issubset(finished_nodes)
                ]

                if not aborted:
                    skipped_ready = [
                        node_key for node_key in ready if predecessors[node_key] & skipped_nodes
                    ]
                    for node_key in skipped_ready:
                        node = executable_by_key[node_key].spec
                        remaining_nodes.remove(node_key)
                        ordered_stateful_nodes.complete(node_key, entered=False)
                        skipped_nodes.add(node_key)
                        finished_nodes.add(node_key)
                        outcomes[node_key] = _NodeExecutionOutcome(
                            result=None,
                            materialized_outputs=None,
                            timing=_node_timing(
                                node,
                                status="skipped",
                                started_perf=time.perf_counter(),
                                skip_reason="upstream_skipped",
                            ),
                        )
                    if skipped_ready:
                        continue

                    available_workers = max_workers - len(running)
                    parallel_ready = [
                        node_key for node_key in ready if node_key in parallel_node_keys
                    ][:available_workers]
                    for node_key in parallel_ready:
                        executable = executable_by_key[node_key]
                        node = executable.spec
                        node_started = time.perf_counter()
                        collect_started = time.perf_counter()
                        inputs = _collect_inputs(
                            node,
                            self.incoming_edges.get(node_key, []),
                            values,
                        )
                        input_collect_ms = _elapsed_ms(collect_started)
                        missing = _missing_required_inputs(node, inputs)
                        remaining_nodes.remove(node_key)
                        if missing:
                            ordered_stateful_nodes.complete(node_key, entered=False)
                            skipped_nodes.add(node_key)
                            finished_nodes.add(node_key)
                            outcomes[node_key] = _NodeExecutionOutcome(
                                result=None,
                                materialized_outputs=None,
                                timing=_node_timing(
                                    node,
                                    status="skipped",
                                    started_perf=node_started,
                                    input_collect_ms=input_collect_ms,
                                    missing_inputs=missing,
                                    skip_reason="missing_required_inputs",
                                ),
                            )
                            continue
                        future = executor.submit(
                            _execute_node,
                            executable,
                            context,
                            inputs,
                            node_started=node_started,
                            input_collect_ms=input_collect_ms,
                            materialize_frame_node_outputs=materialize_frame_node_outputs,
                            ordered_stateful_nodes=ordered_stateful_nodes,
                        )
                        running[future] = node_key

                    if parallel_ready:
                        continue

                    if not running and ready:
                        node_key = ready[0]
                        executable = executable_by_key[node_key]
                        node = executable.spec
                        node_started = time.perf_counter()
                        collect_started = time.perf_counter()
                        inputs = _collect_inputs(
                            node,
                            self.incoming_edges.get(node_key, []),
                            values,
                        )
                        input_collect_ms = _elapsed_ms(collect_started)
                        missing = _missing_required_inputs(node, inputs)
                        remaining_nodes.remove(node_key)
                        if missing:
                            ordered_stateful_nodes.complete(node_key, entered=False)
                            skipped_nodes.add(node_key)
                            finished_nodes.add(node_key)
                            outcomes[node_key] = _NodeExecutionOutcome(
                                result=None,
                                materialized_outputs=None,
                                timing=_node_timing(
                                    node,
                                    status="skipped",
                                    started_perf=node_started,
                                    input_collect_ms=input_collect_ms,
                                    missing_inputs=missing,
                                    skip_reason="missing_required_inputs",
                                ),
                            )
                            continue
                        accept(
                            node_key,
                            _execute_node(
                                executable,
                                context,
                                inputs,
                                node_started=node_started,
                                input_collect_ms=input_collect_ms,
                                materialize_frame_node_outputs=(materialize_frame_node_outputs),
                                ordered_stateful_nodes=ordered_stateful_nodes,
                            ),
                        )
                        continue

                if running:
                    completed, _pending = (
                        wait(running, return_when=FIRST_COMPLETED) if not aborted else wait(running)
                    )
                    for future in sorted(
                        completed,
                        key=lambda item: order_index[running[item]],
                    ):
                        node_key = running.pop(future)
                        accept(node_key, future.result())
                    continue
                break

        node_outputs: dict[str, dict[str, object]] = {}
        executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]] = []
        node_timings: list[dict[str, Any]] = []
        errors: list[RuntimeNodeError] = []
        for executable in self.nodes:
            node_key = executable.spec.node_key
            outcome = outcomes.get(node_key)
            if outcome is not None:
                node_timings.append(outcome.timing)
                if outcome.materialized_outputs is not None:
                    node_outputs[node_key] = outcome.materialized_outputs
                if outcome.result is not None:
                    executed_payloads.extend(
                        (executable.spec, port, payload)
                        for port, payload in outcome.result.outputs.items()
                    )
            error = errors_by_key.get(node_key)
            if error is not None:
                errors.append(error)
        return (
            errors,
            values,
            node_outputs,
            executed_payloads,
            skipped_nodes,
            node_timings,
        )


def _execute_node(
    executable: ExecutableNode,
    context: RuntimeExecutionContext,
    inputs: NodeInputs,
    *,
    node_started: float,
    input_collect_ms: float,
    materialize_frame_node_outputs: bool,
    ordered_stateful_nodes: _OrderedStatefulNodes,
) -> _NodeExecutionOutcome:
    node = executable.spec
    entered_ordered_node = ordered_stateful_nodes.enter(node.node_key)
    execute_started = time.perf_counter()
    try:
        result = executable.handler.execute(context, node, inputs)
    except RuntimeNodeExecutionError as exc:
        error = exc.error.model_copy(
            update={"node_key": node.node_key, "node_type": node.node_type}
        )
        return _NodeExecutionOutcome(
            result=None,
            materialized_outputs=None,
            error=error,
            timing=_node_timing(
                node,
                status="failed",
                started_perf=node_started,
                input_collect_ms=input_collect_ms,
                execute_ms=_elapsed_ms(execute_started),
                error_code=error.code,
                error_metadata=error.metadata,
            ),
        )
    except Exception as exc:
        error = RuntimeNodeError(
            code="node_execution_failed",
            message=str(exc),
            node_key=node.node_key,
            node_type=node.node_type,
            retryable=False,
        )
        return _NodeExecutionOutcome(
            result=None,
            materialized_outputs=None,
            error=error,
            timing=_node_timing(
                node,
                status="failed",
                started_perf=node_started,
                input_collect_ms=input_collect_ms,
                execute_ms=_elapsed_ms(execute_started),
                error_code=error.code,
            ),
        )
    finally:
        if entered_ordered_node:
            ordered_stateful_nodes.complete(node.node_key)

    execute_ms = _elapsed_ms(execute_started)
    if not result.outputs:
        return _NodeExecutionOutcome(
            result=result,
            materialized_outputs=None,
            timing=_node_timing(
                node,
                status="skipped",
                started_perf=node_started,
                input_collect_ms=input_collect_ms,
                execute_ms=execute_ms,
                output_count=0,
                output_ports=[],
                skip_reason="empty_outputs",
            ),
        )

    materialize_started = time.perf_counter()
    try:
        materialized_outputs = {
            port: _node_output_payload(
                payload,
                materialize_frame_outputs=materialize_frame_node_outputs,
            )
            for port, payload in result.outputs.items()
        }
    except Exception as exc:
        error = RuntimeNodeError(
            code="node_output_materialization_failed",
            message=str(exc),
            node_key=node.node_key,
            node_type=node.node_type,
            retryable=False,
        )
        return _NodeExecutionOutcome(
            result=None,
            materialized_outputs=None,
            error=error,
            timing=_node_timing(
                node,
                status="failed",
                started_perf=node_started,
                input_collect_ms=input_collect_ms,
                execute_ms=execute_ms,
                output_materialize_ms=_elapsed_ms(materialize_started),
                error_code=error.code,
            ),
        )
    return _NodeExecutionOutcome(
        result=result,
        materialized_outputs=materialized_outputs,
        timing=_node_timing(
            node,
            status="completed",
            started_perf=node_started,
            input_collect_ms=input_collect_ms,
            execute_ms=execute_ms,
            output_materialize_ms=_elapsed_ms(materialize_started),
            output_count=len(result.outputs),
            output_ports=list(result.outputs),
        ),
    )


def _parallel_node_keys(
    context: RuntimeExecutionContext,
    nodes: list[ExecutableNode],
) -> set[str]:
    configured = context.metadata.get("parallel_node_keys")
    if not isinstance(configured, list | tuple | set | frozenset):
        return set()
    known = {item.spec.node_key for item in nodes}
    return {
        str(node_key) for node_key in configured if isinstance(node_key, str) and node_key in known
    }


def _parallel_worker_count(
    context: RuntimeExecutionContext,
    parallel_node_keys: set[str],
) -> int:
    try:
        configured = int(context.metadata.get("parallel_node_max_workers", 2))
    except (TypeError, ValueError):
        configured = 2
    return min(max(configured, 1), max(len(parallel_node_keys), 1), 4)


def _intrinsically_undistorted_frame(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> FramePayload | None:
    for node, _port, payload in reversed(executed_payloads):
        if node.node_type == "preprocessing.camera_undistort" and isinstance(
            payload, FramePayload
        ):
            return payload
    return None


def _topological_order(graph: RuntimePipelineGraph) -> list[str]:
    nodes = [node.node_key for node in graph.nodes]
    order_index = {node_key: index for index, node_key in enumerate(nodes)}
    node_set = set(nodes)
    indegree = {node_key: 0 for node_key in nodes}
    adjacency: dict[str, list[str]] = {node_key: [] for node_key in nodes}
    for edge in graph.edges:
        if edge.from_node_key not in node_set or edge.to_node_key not in node_set:
            raise RuntimeGraphBuildError("Graph contains an edge referencing an unknown node.")
        adjacency[edge.from_node_key].append(edge.to_node_key)
        indegree[edge.to_node_key] += 1
    queue: list[tuple[int, str]] = []
    for node_key, degree in indegree.items():
        if degree == 0:
            heappush(queue, (order_index[node_key], node_key))
    order: list[str] = []
    while queue:
        _, node_key = heappop(queue)
        order.append(node_key)
        for next_key in adjacency[node_key]:
            indegree[next_key] -= 1
            if indegree[next_key] == 0:
                heappush(queue, (order_index[next_key], next_key))
    if len(order) != len(nodes):
        raise RuntimeGraphBuildError("Pipeline graph contains a cycle and is not executable.")
    return order


def _collect_inputs(
    node: RuntimeNodeSpec,
    incoming_edges: list[RuntimeGraphEdge],
    values: dict[tuple[str, str], object],
) -> NodeInputs:
    inputs: NodeInputs = {}
    for edge in incoming_edges:
        value = values.get((edge.from_node_key, edge.from_port))
        if value is not None:
            inputs[edge.to_port] = value  # type: ignore[assignment]
    return inputs


def _missing_required_inputs(node: RuntimeNodeSpec, inputs: NodeInputs) -> list[str]:
    return [port.key for port in node.input_ports if port.required and port.key not in inputs]


def _elapsed_ms(started_perf: float) -> float:
    return round(max((time.perf_counter() - started_perf) * 1000, 0.0), 3)


def _node_timing(
    node: RuntimeNodeSpec,
    *,
    status: str,
    started_perf: float,
    input_collect_ms: float | None = None,
    execute_ms: float | None = None,
    output_materialize_ms: float | None = None,
    output_count: int | None = None,
    output_ports: list[str] | None = None,
    missing_inputs: list[str] | None = None,
    skip_reason: str | None = None,
    error_code: str | None = None,
    error_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timing: dict[str, Any] = {
        "node_key": node.node_key,
        "node_type": node.node_type,
        "node_category": node.node_category.value,
        "status": status,
        "total_ms": _elapsed_ms(started_perf),
    }
    optional_values = {
        "input_collect_ms": input_collect_ms,
        "execute_ms": execute_ms,
        "output_materialize_ms": output_materialize_ms,
        "output_count": output_count,
        "output_ports": output_ports,
        "missing_inputs": missing_inputs,
        "skip_reason": skip_reason,
        "error_code": error_code,
        "error_metadata": error_metadata,
    }
    timing.update({key: value for key, value in optional_values.items() if value is not None})
    return timing


def _node_output_payload(
    payload: AnyRuntimePayload,
    *,
    materialize_frame_outputs: bool,
) -> dict[str, Any]:
    if isinstance(payload, FramePayload) and not materialize_frame_outputs:
        return {
            "payload_type": payload.payload_type,
            "mime_type": payload.mime_type,
            "width": payload.width,
            "height": payload.height,
            "source_uri": payload.source_uri,
            "timestamp": payload.timestamp,
            "image_context": (
                payload.image_context.model_dump(mode="json")
                if payload.image_context is not None
                else None
            ),
            "metadata": {
                **payload.metadata,
                "image_base64_deferred": True,
            },
        }
    return payload.model_dump(mode="json")


def _default_display_output(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> dict[str, object] | None:
    frame_entry = _preferred_display_frame_entry(executed_payloads)
    if frame_entry is not None:
        node, _port, payload = frame_entry
        display_payload = DisplayOutputPayload(
            display_context_id=payload.image_context.context_id,
            display_context=payload.image_context,
            frame=payload,
            layer_policy={"selection": "default_primary_visual_context"},
        )
        return {
            "node_key": node.node_key,
            "node_type": node.node_type,
            "node_category": node.node_category.value,
            "output_port": "display",
            "payload_type": display_payload.payload_type,
            "payload": display_payload.model_dump(mode="json"),
        }
    return None


def _preferred_display_frame_entry(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> tuple[RuntimeNodeSpec, str, FramePayload] | None:
    frame_entries = [
        (node, port, payload)
        for node, port, payload in executed_payloads
        if isinstance(payload, FramePayload) and payload.image_context is not None
    ]
    if not frame_entries:
        return None
    for node, port, payload in reversed(frame_entries):
        if payload.image_context is None:
            continue
        if payload.image_context.kind not in {"processed", "masked"}:
            return node, port, payload
    return frame_entries[-1]


def _slowest_node_timing(node_timings: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        timing
        for timing in node_timings
        if timing.get("status") in {"completed", "failed"}
        and isinstance(timing.get("total_ms"), int | float)
    ]
    if not candidates:
        return None
    slowest = max(candidates, key=lambda timing: float(timing["total_ms"]))
    return {
        "node_key": slowest.get("node_key"),
        "node_type": slowest.get("node_type"),
        "status": slowest.get("status"),
        "total_ms": slowest.get("total_ms"),
        "execute_ms": slowest.get("execute_ms"),
    }


def _inference_timings(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, Any]]:
    timings: list[dict[str, Any]] = []
    for node, port, payload in executed_payloads:
        if not node.node_type.startswith("inference."):
            continue
        if not isinstance(
            payload,
            DetectionPayload | ClassificationPayload | SegmentPayload | AnomalyPayload,
        ):
            continue
        runtime_timings = payload.metadata.get("runtime_timings_ms")
        if not isinstance(runtime_timings, dict) or not runtime_timings:
            continue
        timings.append(
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "node_category": node.node_category.value,
                "output_port": port,
                **runtime_timings,
            }
        )
    return timings


def _payload_entry(
    node: RuntimeNodeSpec,
    port: str,
    payload: AnyRuntimePayload,
) -> dict[str, object]:
    return {
        "node_key": node.node_key,
        "node_type": node.node_type,
        "node_category": node.node_category.value,
        "output_port": port,
        "payload_type": payload.payload_type,
        "payload": payload.model_dump(mode="json"),
    }


def _raw_inference_entry(
    node: RuntimeNodeSpec,
    port: str,
    payload: DetectionPayload | ClassificationPayload | SegmentPayload | AnomalyPayload,
) -> dict[str, object]:
    entry = _payload_entry(node, port, payload)
    return {
        "node_key": entry["node_key"],
        "node_type": entry["node_type"],
        "node_category": entry["node_category"],
        "output_port": entry["output_port"],
        "payload_type": entry["payload_type"],
        "gateway_response": entry["payload"],
    }


def _image_contexts(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, object]]:
    contexts: dict[str, dict[str, object]] = {}
    for node, port, payload in executed_payloads:
        for context in _contexts_from_payload(payload):
            contexts[context["context_id"]] = {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "output_port": port,
                **context,
            }
    return list(contexts.values())


def _contexts_from_payload(payload: AnyRuntimePayload) -> list[dict[str, object]]:
    contexts: list[dict[str, object]] = []
    if isinstance(payload, FramePayload) and payload.image_context is not None:
        contexts.append(payload.image_context.model_dump(mode="json"))
    if isinstance(payload, RoiSetPayload):
        for roi in payload.rois:
            if roi.image_context is not None:
                contexts.append(roi.image_context.model_dump(mode="json"))
            elif roi.frame.image_context is not None:
                contexts.append(roi.frame.image_context.model_dump(mode="json"))
    if isinstance(payload, DisplayOutputPayload):
        contexts.append(payload.display_context.model_dump(mode="json"))
    if isinstance(payload, CvEdgesPayload) and payload.edge_mask_frame is not None:
        if payload.edge_mask_frame.image_context is not None:
            contexts.append(payload.edge_mask_frame.image_context.model_dump(mode="json"))
    return contexts


def _object_sets(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for node, port, payload in executed_payloads:
        objects: list[dict[str, object]] = []
        image_context_id: str | None = None
        if isinstance(payload, DetectionPayload | SegmentPayload):
            objects = [item.model_dump(mode="json") for item in payload.objects]
            image_context_id = payload.image_context_id
        if isinstance(payload, RegionSetPayload):
            objects = [
                {
                    "object_id": region.source_object_id or region.key or f"region_{index}",
                    "object_type": "region",
                    "source_image_context_id": region.source_image_context_id,
                    "geometry": region.metadata.get("geometry")
                    if isinstance(region.metadata.get("geometry"), dict)
                    else region.box.model_dump(mode="json"),
                    "coordinate_system": "normalized_xy",
                    "label": region.label,
                    "score": region.score,
                    "producer_node_key": region.source_node_key,
                    "metadata": region.metadata,
                }
                for index, region in enumerate(payload.regions, start=1)
            ]
            image_context_id = next(
                (
                    region.source_image_context_id
                    for region in payload.regions
                    if region.source_image_context_id
                ),
                None,
            )
        if not objects:
            continue
        entries.append(
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "output_port": port,
                "payload_type": "object_set",
                "image_context_id": image_context_id,
                "objects": objects,
            }
        )
    return entries


def _enriched_object_annotations(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, object]]:
    annotations: list[dict[str, object]] = []
    for node, port, payload in executed_payloads:
        enrichments = []
        if isinstance(payload, ClassificationPayload):
            enrichments = [item.model_dump(mode="json") for item in payload.enrichments]
        if not enrichments:
            continue
        annotations.append(
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "output_port": port,
                "payload_type": "object_enrichment",
                "annotations": enrichments,
            }
        )
    return annotations


def _overlay_primitives(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, object]]:
    primitives: list[dict[str, object]] = []
    for node, port, payload in executed_payloads:
        if not isinstance(payload, OverlayPayload):
            continue
        for primitive in payload.primitives:
            primitives.append(
                {
                    "node_key": node.node_key,
                    "node_type": node.node_type,
                    "output_port": port,
                    **primitive.model_dump(mode="json"),
                }
            )
    return primitives


def _final_response(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
    projection_evaluation: RuntimeProjectionEvaluation | None = None,
) -> dict[str, object]:
    if projection_evaluation is not None:
        return projection_evaluation.response
    explicit_responses = [
        payload
        for node, _port, payload in executed_payloads
        if node.node_type == "output.http_trigger_response" and isinstance(payload, ResultPayload)
    ]
    explicit_result_outputs = [
        payload
        for node, _port, payload in executed_payloads
        if node.node_type == "output.result" and isinstance(payload, ResultPayload)
    ]
    fallback_results = [
        payload
        for node, _port, payload in executed_payloads
        if isinstance(payload, ResultPayload)
        and node.node_type
        not in {"output.persisted_result", "output.http_trigger_response", "output.result"}
    ]
    response_payloads = explicit_responses or explicit_result_outputs[-1:] or fallback_results[-1:]
    final_response: dict[str, object] = {}
    for payload in response_payloads:
        final_response.update(payload.result)
        if payload.decision is not None and not explicit_responses:
            final_response.setdefault("result", payload.decision)
    return final_response


def _persisted_results(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
    projection_evaluation: RuntimeProjectionEvaluation | None = None,
) -> list[dict[str, object]]:
    if projection_evaluation is not None:
        history = projection_evaluation.history
        if not history:
            return []
        return [
            _result_payload_entry(
                node_key=projection_evaluation.output_node_key,
                node_type="output.result",
                output_port="result",
                payload=ResultPayload(
                    result=history,
                    metadata={"projection_destination": "history"},
                ),
            )
        ]
    return [
        _payload_entry(node, port, payload)
        for node, port, payload in executed_payloads
        if node.node_type in {"output.persisted_result", "output.result"}
        and isinstance(payload, ResultPayload)
    ]


def _http_trigger_responses(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
    projection_evaluation: RuntimeProjectionEvaluation | None = None,
) -> list[dict[str, object]]:
    if projection_evaluation is not None:
        response = projection_evaluation.response
        if not response:
            return []
        return [
            _result_payload_entry(
                node_key=projection_evaluation.output_node_key,
                node_type="output.result",
                output_port="response",
                payload=ResultPayload(
                    result=response,
                    metadata={"projection_destination": "response"},
                ),
            )
        ]
    return [
        _payload_entry(node, port, payload)
        for node, port, payload in executed_payloads
        if node.node_type == "output.http_trigger_response" and isinstance(payload, ResultPayload)
    ]


def _result_payload_entry(
    *,
    node_key: str,
    node_type: str,
    output_port: str,
    payload: ResultPayload,
) -> dict[str, object]:
    return {
        "node_key": node_key,
        "node_type": node_type,
        "node_category": RuntimeNodeCategory.OUTPUT.value,
        "output_port": output_port,
        "payload_type": payload.payload_type,
        "payload": payload.model_dump(mode="json"),
    }


def _internal_events(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for node, port, payload in executed_payloads:
        if node.node_type != "output.internal_event" or not isinstance(payload, EventsPayload):
            continue
        for event in payload.events:
            entries.append(
                {
                    "node_key": node.node_key,
                    "node_type": node.node_type,
                    "node_category": node.node_category.value,
                    "output_port": port,
                    "event": event.model_dump(mode="json"),
                }
            )
    return entries
