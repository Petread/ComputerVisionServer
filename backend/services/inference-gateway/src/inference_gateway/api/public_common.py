from __future__ import annotations

from fastapi import Request

from cv_contracts import HealthCheck, HealthResponse, ResponseMeta
from cv_core.app import _http_ready_probe
from cv_status import HealthState, TargetPath
from cv_utils import PlatformError
from cv_utils import get_correlation_id, get_request_id, utc_now_iso


async def build_public_health_response(
    request: Request,
    *,
    target_path: TargetPath,
    ready: bool,
    model_count: int,
) -> HealthResponse:
    checks: list[HealthCheck] = []

    checks.append(
        HealthCheck(
            name="process",
            state=HealthState.OK,
            detail="Gateway process is running.",
            observed_at=utc_now_iso(),
        )
    )

    startup_completed = bool(getattr(request.app.state, "startup_completed", False))
    shutting_down = bool(getattr(request.app.state, "shutting_down", False))
    checks.append(
        HealthCheck(
            name="startup",
            state=HealthState.OK if startup_completed else HealthState.DOWN,
            detail=(
                "Startup validation completed." if startup_completed else "Startup validation has not completed yet."
            ),
            observed_at=utc_now_iso(),
        )
    )
    checks.append(
        HealthCheck(
            name="catalog",
            state=HealthState.OK if model_count >= 0 else HealthState.DOWN,
            detail=f"{model_count} public model entries are currently discoverable.",
            observed_at=utc_now_iso(),
        )
    )

    if ready:
        checks.append(
            HealthCheck(
                name="shutdown",
                state=HealthState.DOWN if shutting_down else HealthState.OK,
                detail=(
                    "Shutdown requested; service is not ready for new work."
                    if shutting_down
                    else "Service is accepting new work."
                ),
                observed_at=utc_now_iso(),
            )
        )
        settings = request.app.state.settings
        probe_name, probe_state, probe_detail = await _http_ready_probe(
            "triton_rt" if target_path == TargetPath.REALTIME else "triton_batch",
            settings.triton.realtime.http_url
            if target_path == TargetPath.REALTIME
            else settings.triton.batch.http_url,
        )
        checks.append(
            HealthCheck(
                name=probe_name,
                state=probe_state,
                detail=probe_detail,
                observed_at=utc_now_iso(),
            )
        )

    overall_state = HealthState.OK
    for check in checks:
        if check.state == HealthState.DOWN:
            overall_state = HealthState.DOWN
            break
        if check.state == HealthState.DEGRADED:
            overall_state = HealthState.DEGRADED

    return HealthResponse(
        service=request.app.title,
        state=overall_state,
        checks=checks,
        meta=ResponseMeta(
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
            generated_at=utc_now_iso(),
        ),
    )


async def reject_unexpected_form_fields(
    request: Request,
    *,
    allowed_fields: set[str],
) -> None:
    form = await request.form()
    unexpected = sorted({str(key) for key in form.keys()} - allowed_fields)
    if unexpected:
        field_name = unexpected[0]
        raise PlatformError(
            code="unsupported_parameter",
            message="The request contained multipart form fields that are not supported by this endpoint.",
            detail=field_name,
            status_code=422,
            retryable=False,
            validation_errors=[
                {
                    "field": field_name,
                    "location": ["body", field_name],
                    "message": f"Field '{field_name}' is not supported for this endpoint.",
                    "error_type": "unsupported_parameter",
                }
            ],
        )
