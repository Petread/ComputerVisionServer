import asyncio
import logging


async def start_local_model_reconciler(app) -> None:
    async def run():
        while True:
            await asyncio.sleep(1)
            for name in ("realtime_gateway_service", "batch_gateway_service"):
                service = getattr(app.state, name, None)
                if service is not None:
                    try:
                        await asyncio.to_thread(service.release_unused_local_models)
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Local model cache reconciliation failed; will retry."
                        )

    app.state.local_model_reconciler = asyncio.create_task(run())


async def stop_local_model_reconciler(app) -> None:
    task = getattr(app.state, "local_model_reconciler", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
