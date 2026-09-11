from __future__ import annotations

from cv_status import (
    RuntimeChannelDesiredState,
    RuntimeExecutionStatus,
    RuntimeObservedState,
    RuntimePipelineStatus,
)

ALLOWED_CHANNEL_DESIRED_TRANSITIONS: dict[
    RuntimeChannelDesiredState,
    tuple[RuntimeChannelDesiredState, ...],
] = {
    RuntimeChannelDesiredState.DRAFT: (
        RuntimeChannelDesiredState.STOPPED,
        RuntimeChannelDesiredState.DISABLED,
    ),
    RuntimeChannelDesiredState.STOPPED: (
        RuntimeChannelDesiredState.START_REQUESTED,
        RuntimeChannelDesiredState.DISABLED,
    ),
    RuntimeChannelDesiredState.START_REQUESTED: (
        RuntimeChannelDesiredState.RUNNING,
        RuntimeChannelDesiredState.STOP_REQUESTED,
        RuntimeChannelDesiredState.DISABLED,
    ),
    RuntimeChannelDesiredState.RUNNING: (
        RuntimeChannelDesiredState.STOP_REQUESTED,
        RuntimeChannelDesiredState.DISABLED,
    ),
    RuntimeChannelDesiredState.STOP_REQUESTED: (
        RuntimeChannelDesiredState.STOPPED,
        RuntimeChannelDesiredState.DISABLED,
    ),
    RuntimeChannelDesiredState.DISABLED: (
        RuntimeChannelDesiredState.STOPPED,
    ),
}

ALLOWED_EXECUTION_TRANSITIONS: dict[
    RuntimeExecutionStatus,
    tuple[RuntimeExecutionStatus, ...],
] = {
    RuntimeExecutionStatus.REQUESTED: (
        RuntimeExecutionStatus.ADMITTED,
        RuntimeExecutionStatus.VALIDATING,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.ADMITTED: (
        RuntimeExecutionStatus.VALIDATING,
        RuntimeExecutionStatus.STARTING,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.VALIDATING: (
        RuntimeExecutionStatus.LOADING_PIPELINE,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.LOADING_PIPELINE: (
        RuntimeExecutionStatus.STARTING,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.STARTING: (
        RuntimeExecutionStatus.RUNNING,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.RUNNING: (
        RuntimeExecutionStatus.DRAINING,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.DRAINING: (
        RuntimeExecutionStatus.STOPPED,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
    ),
    RuntimeExecutionStatus.STOPPED: (),
    RuntimeExecutionStatus.FAILED_RETRYABLE: (
        RuntimeExecutionStatus.RECOVERED,
        RuntimeExecutionStatus.FAILED_TERMINAL,
    ),
    RuntimeExecutionStatus.FAILED_TERMINAL: (),
    RuntimeExecutionStatus.RECOVERED: (),
}

ALLOWED_PIPELINE_VERSION_TRANSITIONS: dict[
    RuntimePipelineStatus,
    tuple[RuntimePipelineStatus, ...],
] = {
    RuntimePipelineStatus.DRAFT: (
        RuntimePipelineStatus.VALIDATED,
        RuntimePipelineStatus.ARCHIVED,
    ),
    RuntimePipelineStatus.VALIDATED: (
        RuntimePipelineStatus.PUBLISHED,
        RuntimePipelineStatus.DRAFT,
        RuntimePipelineStatus.ARCHIVED,
    ),
    RuntimePipelineStatus.PUBLISHED: (
        RuntimePipelineStatus.SUPERSEDED,
        RuntimePipelineStatus.ARCHIVED,
    ),
    RuntimePipelineStatus.SUPERSEDED: (
        RuntimePipelineStatus.ARCHIVED,
    ),
    RuntimePipelineStatus.ARCHIVED: (),
}

RECOVERABLE_OBSERVED_STATES = frozenset(
    {
        RuntimeObservedState.DEGRADED,
        RuntimeObservedState.FAILED,
        RuntimeObservedState.RECOVERING,
    }
)


def can_transition_channel_desired(
    current: RuntimeChannelDesiredState,
    target: RuntimeChannelDesiredState,
) -> bool:
    return target in ALLOWED_CHANNEL_DESIRED_TRANSITIONS[current]


def can_transition_execution(
    current: RuntimeExecutionStatus,
    target: RuntimeExecutionStatus,
) -> bool:
    return target in ALLOWED_EXECUTION_TRANSITIONS[current]


def can_transition_pipeline_version(
    current: RuntimePipelineStatus,
    target: RuntimePipelineStatus,
) -> bool:
    return target in ALLOWED_PIPELINE_VERSION_TRANSITIONS[current]
