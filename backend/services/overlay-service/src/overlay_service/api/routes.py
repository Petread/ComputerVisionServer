from typing import Annotated

from cv_contracts.runtime import RuntimeBoundaryResponse, RuntimeLiveViewResponse
from cv_core import parse_machine_token
from cv_db import authenticate_machine_identity, get_runtime_live_view_response, session_scope
from cv_runtime import runtime_service_boundary
from cv_utils import AuthenticationError, PlatformError
from fastapi import APIRouter
from fastapi import Depends, Request

from overlay_service import SERVICE_NAME

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request):
    return request.app.state.settings


def require_overlay_access(request: Request, settings=Depends(get_settings)) -> None:
    if not settings.api_keys.enabled:
        return None
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is None:
        raise AuthenticationError("Machine authentication is required for overlay APIs.")
    key_id, secret = machine_token
    authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={"runtime.overlay.read"},
        allowed_subject_types={"service"},
    )
    return None


OverlayAccess = Annotated[None, Depends(require_overlay_access)]


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.get(
    "/runtime/channels/{channel_id}/overlay/latest",
    response_model=RuntimeLiveViewResponse,
)
def get_channel_overlay_latest(
    channel_id: str,
    _identity: OverlayAccess,
    settings=Depends(get_settings),
) -> RuntimeLiveViewResponse:
    with session_scope(settings) as session:
        try:
            return get_runtime_live_view_response(session, settings, channel_id=channel_id)
        except ValueError as exc:
            raise PlatformError(
                code="runtime_overlay_unavailable",
                message="The live overlay state could not be loaded.",
                detail=str(exc),
                status_code=404 if "does not exist" in str(exc) else 422,
            ) from exc
