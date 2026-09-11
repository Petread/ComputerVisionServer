from cv_contracts.runtime import RuntimeBoundaryResponse
from cv_runtime import runtime_service_boundary
from fastapi import APIRouter

from camera_media import SERVICE_NAME

router = APIRouter(tags=["runtime-boundary"])


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )
