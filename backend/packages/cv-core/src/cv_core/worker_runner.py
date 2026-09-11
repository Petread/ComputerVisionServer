from __future__ import annotations

import signal
import socket
import threading
from collections.abc import Callable

from cv_observability import configure_logging, get_logger
from redis import Redis
from rq import Connection, Worker, worker_registration

from cv_core.settings import PlatformSettings

HeartbeatCallback = Callable[[str, str, dict[str, object]], None]


def _discard_stale_worker_registration(redis: Redis, worker: Worker, logger) -> None:  # noqa: ANN001
    if not redis.exists(worker.key) or redis.hexists(worker.key, "death"):
        return

    logger.warning(
        "Removing stale RQ worker registration before startup.",
        worker_name=worker.name,
        worker_key=worker.key,
    )
    with redis.pipeline() as pipeline:
        worker_registration.unregister(worker, pipeline)
        pipeline.delete(worker.key)
        pipeline.execute()


def run_rq_worker(
    *,
    settings: PlatformSettings,
    service_name: str,
    queue_names: list[str],
    heartbeat_callback: HeartbeatCallback | None = None,
) -> None:
    instance_id = f"{socket.gethostname()}-{service_name}-{threading.get_native_id()}"
    configure_logging(service_name, settings.env.value, instance_id=instance_id)
    logger = get_logger(service_name).bind(entity_type="worker", entity_id=instance_id)
    redis = Redis.from_url(settings.redis.url())
    worker = Worker(queue_names, connection=redis, name=f"{service_name}-{instance_id}")
    stop_event = threading.Event()
    state = {"status": "starting"}

    def emit_heartbeat(status: str, details: dict[str, object]) -> None:
        state["status"] = status
        if heartbeat_callback is not None:
            try:
                heartbeat_callback(instance_id, status, details)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worker heartbeat update failed.", exception_message=str(exc))

    def heartbeat_loop() -> None:
        while not stop_event.wait(timeout=max(settings.training.heartbeat_interval_seconds, 5)):
            emit_heartbeat(
                state["status"],
                {
                    "queues": queue_names,
                    "worker_name": worker.name,
                },
            )

    def request_stop(_signum: int, _frame) -> None:  # noqa: ANN001
        logger.warning("Worker shutdown requested.")
        emit_heartbeat("stopping", {"queues": queue_names, "worker_name": worker.name})
        stop_event.set()
        stop_method = getattr(worker, "request_stop", None)
        if callable(stop_method):
            stop_method()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, request_stop)

    _discard_stale_worker_registration(redis, worker, logger)

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, name=f"{service_name}-heartbeat", daemon=True
    )
    emit_heartbeat("running", {"queues": queue_names, "worker_name": worker.name})
    heartbeat_thread.start()
    logger.info("Worker process started.")
    try:
        with Connection(redis):
            worker.work(with_scheduler=True)
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=2)
        emit_heartbeat("stopped", {"queues": queue_names, "worker_name": worker.name})
        logger.info("Worker process stopped.")
