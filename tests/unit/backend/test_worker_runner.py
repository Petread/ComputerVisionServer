from __future__ import annotations

from collections.abc import Iterator

from cv_core import worker_runner
from pytest import MonkeyPatch


class _FakePipeline:
    def __init__(self) -> None:
        self.operations: list[tuple[str, str] | tuple[str]] = []

    def __enter__(self) -> _FakePipeline:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def delete(self, key: str) -> _FakePipeline:
        self.operations.append(("delete", key))
        return self

    def execute(self) -> list[object]:
        self.operations.append(("execute",))
        return []


class _FakeRedis:
    def __init__(self, *, exists: bool, has_death: bool) -> None:
        self._exists = exists
        self._has_death = has_death
        self.pipeline_instance = _FakePipeline()

    def exists(self, _key: str) -> bool:
        return self._exists

    def hexists(self, _key: str, _field: str) -> bool:
        return self._has_death

    def pipeline(self) -> _FakePipeline:
        return self.pipeline_instance


class _FakeWorker:
    name = "control-worker-container-control-worker-1"
    key = "rq:worker:control-worker-container-control-worker-1"


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, message: str, **kwargs: object) -> None:
        self.warnings.append((message, kwargs))


def test_discard_stale_worker_registration_removes_active_record(
    monkeypatch: MonkeyPatch,
) -> None:
    redis = _FakeRedis(exists=True, has_death=False)
    worker = _FakeWorker()
    logger = _FakeLogger()
    unregister_calls: list[tuple[object, object]] = []

    def fake_unregister(worker_arg: object, pipeline_arg: object) -> None:
        unregister_calls.append((worker_arg, pipeline_arg))

    monkeypatch.setattr(worker_runner.worker_registration, "unregister", fake_unregister)

    worker_runner._discard_stale_worker_registration(redis, worker, logger)  # noqa: SLF001

    assert unregister_calls == [(worker, redis.pipeline_instance)]
    assert redis.pipeline_instance.operations == [("delete", worker.key), ("execute",)]
    assert logger.warnings == [
        (
            "Removing stale RQ worker registration before startup.",
            {"worker_name": worker.name, "worker_key": worker.key},
        )
    ]


def test_discard_stale_worker_registration_keeps_absent_or_dead_records(
    monkeypatch: MonkeyPatch,
) -> None:
    unregister_calls: list[tuple[object, object]] = []

    def fake_unregister(worker_arg: object, pipeline_arg: object) -> None:
        unregister_calls.append((worker_arg, pipeline_arg))

    monkeypatch.setattr(worker_runner.worker_registration, "unregister", fake_unregister)

    for redis in _redis_without_stale_registration():
        logger = _FakeLogger()
        worker_runner._discard_stale_worker_registration(redis, _FakeWorker(), logger)  # noqa: SLF001
        assert redis.pipeline_instance.operations == []
        assert logger.warnings == []

    assert unregister_calls == []


def _redis_without_stale_registration() -> Iterator[_FakeRedis]:
    yield _FakeRedis(exists=False, has_death=False)
    yield _FakeRedis(exists=True, has_death=True)
