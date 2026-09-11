from pathlib import Path

from cv_contracts import ApiError, PublicBatchJobCreateRequest, PublicModelDetailResponse
from inference_gateway.main_batch import app as batch_app
from inference_gateway.main_realtime import app as realtime_app


def test_public_inference_openapi_paths_exist() -> None:
    realtime_openapi = realtime_app.openapi()
    batch_openapi = batch_app.openapi()

    assert "/api/v1/health" in realtime_openapi["paths"]
    assert "/api/v1/ready" in realtime_openapi["paths"]
    assert "/api/v1/models" in realtime_openapi["paths"]
    assert "/api/v1/models/{model_id}" in realtime_openapi["paths"]
    assert "/api/v1/infer/classify" in realtime_openapi["paths"]
    assert "/api/v1/infer/detect" in realtime_openapi["paths"]

    assert "/api/v1/health" in batch_openapi["paths"]
    assert "/api/v1/ready" in batch_openapi["paths"]
    assert "/api/v1/models" in batch_openapi["paths"]
    assert "/api/v1/models/{model_id}" in batch_openapi["paths"]
    assert "/api/v1/batch/jobs" in batch_openapi["paths"]
    assert "/api/v1/batch/jobs/{job_id}" in batch_openapi["paths"]
    assert "/api/v1/batch/jobs/{job_id}/results" in batch_openapi["paths"]
    assert "/api/v1/batch/jobs/{job_id}" in batch_openapi["paths"]


def test_public_contract_models_expose_required_fields() -> None:
    assert "retryable" in ApiError.model_fields
    assert "validation_errors" in ApiError.model_fields
    assert "model_id" in PublicBatchJobCreateRequest.model_fields
    assert "item" in PublicModelDetailResponse.model_fields


def test_integrator_documentation_and_examples_exist() -> None:
    integrator_doc = Path("docs/integrations/inference-gateway-api.md")
    python_example = Path("examples/inference/python_client.py")
    csharp_example = Path("examples/inference/CSharpClient.cs")
    batch_example = Path("examples/inference/batch-job.request.v1.json")

    assert integrator_doc.exists()
    assert python_example.exists()
    assert csharp_example.exists()
    assert batch_example.exists()
