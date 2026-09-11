from __future__ import annotations

from dataclasses import dataclass

from cv_status import QueueName


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    retry_intervals_seconds: tuple[int, ...]


DEFAULT_RETRY_POLICY = RetryPolicy(max_retries=3, retry_intervals_seconds=(5, 30, 120))

QUEUE_BINDINGS: dict[str, tuple[QueueName, ...]] = {
    "control-worker": (
        QueueName.DEPLOY,
        QueueName.WEBHOOK,
        QueueName.DATASET,
        QueueName.MAINTENANCE,
    ),
    "training-worker": (QueueName.TRAINING,),
    "batch-worker": (QueueName.BATCH,),
    "maintenance-worker": (QueueName.MAINTENANCE,),
}
