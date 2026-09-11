from __future__ import annotations

from cv_core import QUEUE_BINDINGS, load_platform_settings, prepare_runtime_paths, run_rq_worker
from cv_db import record_service_heartbeat, session_scope


def _heartbeat_callback(settings):
    def _callback(instance_id: str, status: str, details: dict[str, object]) -> None:
        with session_scope(settings) as session:
            record_service_heartbeat(
                session,
                service_name="training-worker",
                instance_id=instance_id,
                status=status,
                details=details,
            )

    return _callback


def main() -> None:
    settings = load_platform_settings("training-worker")
    prepare_runtime_paths(settings)
    queue_names = [queue.value for queue in QUEUE_BINDINGS["training-worker"]]
    run_rq_worker(
        settings=settings,
        service_name="training-worker",
        queue_names=queue_names,
        heartbeat_callback=_heartbeat_callback(settings),
    )


if __name__ == "__main__":
    main()
