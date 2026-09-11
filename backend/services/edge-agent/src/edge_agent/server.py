from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import uvicorn

from edge_agent.app import app as control_app
from edge_agent.plant import create_plant_app


async def _serve() -> None:
    state = control_app.state.edge_agent
    control_app.state.close_edge_state_on_shutdown = False
    plant_app = create_plant_app(state=state)
    control = uvicorn.Server(
        uvicorn.Config(
            control_app,
            host=str(state.config.address.management_ip),
            port=state.config.address.control_port,
            log_level="info",
        )
    )
    plant = uvicorn.Server(
        uvicorn.Config(
            plant_app,
            host=str(state.config.address.management_ip),
            port=state.config.address.plant_api_port,
            log_level="info",
        )
    )
    control.install_signal_handlers = lambda: None
    plant.install_signal_handlers = lambda: None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, request_stop)
    tasks = [asyncio.create_task(control.serve()), asyncio.create_task(plant.serve())]
    stop_task = asyncio.create_task(stop_event.wait())

    async def watch_launcher() -> None:
        while not stop_event.is_set():
            if (
                os.getenv("YBB_EDGE_SOFTWARE_LAUNCHER") == "1"
                and (Path(state.config.data_root) / "software" / "stop").exists()
            ):
                stop_event.set()
            await asyncio.sleep(0.5)

    launcher_task = asyncio.create_task(watch_launcher())
    done, _pending = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done or any(task in done for task in tasks):
        control.should_exit = True
        plant.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    stop_task.cancel()
    launcher_task.cancel()
    state.close()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
