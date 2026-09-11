from __future__ import annotations

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from admin_api.application.system_status import build_system_status
from cv_db import resolve_active_user
from cv_core import build_session_serializer, read_session_token


def register_job_feed(app: FastAPI) -> None:
    @app.websocket("/api/v1/jobs/events/ws")
    async def job_events(websocket: WebSocket) -> None:
        settings = app.state.settings
        token = websocket.cookies.get(settings.session.cookie_name)
        if not token:
            await websocket.close(code=1008, reason="Authentication required")
            return
        try:
            identity = read_session_token(
                serializer=build_session_serializer(settings.session.secret()),
                token=token,
                max_age_seconds=settings.session.max_age_seconds,
            )
        except Exception:
            await websocket.close(code=1008, reason="Invalid or expired session")
            return
        if (
            resolve_active_user(
                settings,
                username=identity.username,
                session_version=identity.session_version,
            )
            is None
        ):
            await websocket.close(code=1008, reason="Inactive account")
            return
        await websocket.accept()
        try:
            while True:
                payload = await build_system_status(settings)
                await websocket.send_json(payload.model_dump(mode="json"))
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            return
