from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from cv_contracts.runtime import RuntimeStemRotationPriorityControl

from cv_runtime.payloads import StemRotationPriorityPayload


class PriorityModeProvider(Protocol):
    """Source boundary for the one active stem-rotation priority."""

    source: str

    def resolve(
        self,
        control: RuntimeStemRotationPriorityControl,
    ) -> StemRotationPriorityPayload: ...


class FrontendPriorityModeProvider:
    source = "FRONTEND"

    def resolve(
        self,
        control: RuntimeStemRotationPriorityControl,
    ) -> StemRotationPriorityPayload:
        if not control.optimization_enable:
            return _payload(control, valid=False, reason_code="OPTIMIZATION_DISABLED")
        if control.priority_mode == "NONE":
            return _payload(control, valid=False, reason_code="INVALID_PRIORITY_MODE")
        return _payload(control, valid=True, reason_code="OK")


class PlcPriorityModeProvider:
    """Fail-closed adapter boundary until an authenticated PLC read path exists."""

    source = "PLC"

    def resolve(
        self,
        control: RuntimeStemRotationPriorityControl,
    ) -> StemRotationPriorityPayload:
        return _payload(
            control.model_copy(update={"priority_mode": "NONE"}),
            valid=False,
            reason_code="PLC_PRIORITY_PROVIDER_NOT_CONNECTED",
        )


def resolve_stem_rotation_priority(
    raw_control: Mapping[str, Any] | RuntimeStemRotationPriorityControl | None,
    *,
    default_source: str = "FRONTEND",
) -> StemRotationPriorityPayload:
    """Resolve exactly one configured source without falling back across sources."""
    fallback = RuntimeStemRotationPriorityControl(priority_source=_source(default_source))
    try:
        control = (
            raw_control
            if isinstance(raw_control, RuntimeStemRotationPriorityControl)
            else RuntimeStemRotationPriorityControl.model_validate(raw_control or fallback)
        )
    except ValueError:
        return _payload(fallback, valid=False, reason_code="PRIORITY_CONTROL_INVALID")
    provider: PriorityModeProvider
    if control.priority_source == "FRONTEND":
        provider = FrontendPriorityModeProvider()
    else:
        provider = PlcPriorityModeProvider()
    return provider.resolve(control)


def _payload(
    control: RuntimeStemRotationPriorityControl,
    *,
    valid: bool,
    reason_code: str,
) -> StemRotationPriorityPayload:
    axis = {
        "VORSCHNITT_KRITISCH": "VERTICAL_Z",
        "NACHSCHNITT_KRITISCH": "HORIZONTAL_Y",
    }.get(control.priority_mode, "NONE")
    return StemRotationPriorityPayload(
        priority_source=control.priority_source,
        priority_mode=control.priority_mode,
        priority_axis=axis,
        optimization_enable=control.optimization_enable,
        revision=control.revision,
        valid=valid,
        updated_at=control.updated_at,
        updated_by=control.updated_by,
        reason_code=reason_code,
    )


def _source(value: str) -> str:
    return value if value in {"FRONTEND", "PLC"} else "FRONTEND"
