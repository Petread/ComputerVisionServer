from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from cv_contracts import SystemStatusResponse
from cv_contracts import SessionUser
from cv_status import UserRole

from admin_api.application.system_status import build_system_status
from admin_api.dependencies import get_settings, require_permission

router = APIRouter(prefix="/system", tags=["system"])

SystemReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "system.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
            UserRole.VIEWER,
        )
    ),
]


@router.get("/status", response_model=SystemStatusResponse)
async def system_status(
    _current_user: SystemReader,
    settings=Depends(get_settings),
) -> SystemStatusResponse:
    return await build_system_status(settings)
