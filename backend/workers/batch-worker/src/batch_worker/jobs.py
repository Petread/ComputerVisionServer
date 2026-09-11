from __future__ import annotations

import asyncio

from cv_core import load_platform_settings
from cv_status import TargetPath
from cv_utils import utc_now_iso
from inference_gateway.runtime.service import BatchGatewayService


def materialize_batch_input(batch_job_id: str) -> dict[str, object]:
    service = _service()
    result = service.materialize_batch_input(batch_job_id)
    return {
        "batch_job_id": batch_job_id,
        "status": result["status"],
        "chunk_count": result["chunk_count"],
        "finished_at": utc_now_iso(),
    }


def run_batch_chunk(batch_job_id: str, chunk_no: int) -> dict[str, object]:
    service = _service()
    result = asyncio.run(service.run_batch_chunk(batch_job_id, chunk_no))
    return {**result, "finished_at": utc_now_iso()}


def aggregate_batch_results(batch_job_id: str) -> dict[str, object]:
    service = _service()
    result = service.aggregate_batch_results(batch_job_id)
    return {**result, "finished_at": utc_now_iso()}


def publish_batch_outputs(batch_job_id: str) -> dict[str, object]:
    service = _service()
    result = service.publish_batch_outputs(batch_job_id)
    return {**result, "finished_at": utc_now_iso()}


def _service() -> BatchGatewayService:
    settings = load_platform_settings("batch-worker")
    return BatchGatewayService(
        settings,
        service_name="inference-gateway-batch",
        target_path=TargetPath.BATCH,
    )
