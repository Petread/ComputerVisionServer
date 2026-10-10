from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from cv_db import session_scope

logger = logging.getLogger(__name__)

_STARTUP_RELOAD_ATTEMPTS = 8
_STARTUP_RELOAD_RETRY_SECONDS = 5.0
_RECONCILE_INTERVAL_SECONDS = 5.0
_RECONCILE_RELOAD_ATTEMPTS = 3
_RECONCILE_RELOAD_RETRY_SECONDS = 5.0


async def start_active_release_reloader(app: FastAPI) -> None:
    await reload_active_releases_until_ready(
        app,
        attempts=_STARTUP_RELOAD_ATTEMPTS,
        retry_delay_seconds=_STARTUP_RELOAD_RETRY_SECONDS,
    )
    app.state.active_release_reloader_task = asyncio.create_task(
        _active_release_reconcile_loop(app)
    )


async def stop_active_release_reloader(app: FastAPI) -> None:
    task = getattr(app.state, "active_release_reloader_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def reload_active_releases_until_ready(
    app: FastAPI,
    *,
    attempts: int = 1,
    retry_delay_seconds: float = 0.0,
) -> bool:
    attempts = max(int(attempts or 1), 1)
    retry_delay_seconds = max(float(retry_delay_seconds or 0.0), 0.0)
    missing_after_reload = True
    for attempt in range(1, attempts + 1):
        try:
            await reload_active_releases(app)
            return False
        except Exception:
            logger.exception("Runtime model reconciliation attempt failed.")
        logger.warning(
            "Triton serving state still differs from channel demand after reload "
            "attempt %s/%s.",
            attempt,
            attempts,
        )
        if attempt < attempts and retry_delay_seconds > 0:
            await asyncio.sleep(retry_delay_seconds)
    return missing_after_reload


async def _active_release_reconcile_loop(app: FastAPI) -> None:
    while not getattr(app.state, "shutting_down", False):
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
        if getattr(app.state, "shutting_down", False):
            return
        try:
            await reload_active_releases_until_ready(
                app,
                attempts=_RECONCILE_RELOAD_ATTEMPTS,
                retry_delay_seconds=_RECONCILE_RELOAD_RETRY_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to reconcile models required by started channels.")


async def reload_active_releases(app: FastAPI) -> None:
    from cv_deploy.runtime import reconcile_runtime_models

    def reconcile():
        with session_scope(app.state.settings) as session:
            reconcile_runtime_models(session, app.state.settings)
    await asyncio.to_thread(reconcile)
