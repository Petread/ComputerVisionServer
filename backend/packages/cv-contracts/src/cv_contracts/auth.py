from __future__ import annotations

from pydantic import BaseModel, Field

from cv_status import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=8, max_length=256)


class SessionUser(BaseModel):
    user_id: str
    username: str
    display_name: str | None = None
    role: UserRole


class SessionResponse(BaseModel):
    authenticated: bool
    user: SessionUser | None = None
    csrf_token: str | None = None
