from pathlib import Path

from admin_api.main import app as admin_app
from cv_contracts import AuthSupportSummary, SessionResponse
from cv_core.settings import PlatformSettings
from inference_gateway.main_batch import app as batch_app
from inference_gateway.main_realtime import app as realtime_app


def test_runtime_openapi_includes_security_relevant_paths() -> None:
    admin_openapi = admin_app.openapi()
    realtime_openapi = realtime_app.openapi()
    batch_openapi = batch_app.openapi()

    assert "/api/v1/auth/login" in admin_openapi["paths"]
    assert "/api/v1/auth/logout" in admin_openapi["paths"]
    assert "/api/v1/datasets/{dataset_id}/upload-sessions" in admin_openapi["paths"]
    assert "/api/v1/upload-sessions/{upload_session_id}" in admin_openapi["paths"]
    assert "/api/v1/upload-sessions/{upload_session_id}/parts/{part_no}" in admin_openapi["paths"]
    assert "/api/v1/upload-sessions/{upload_session_id}:complete" in admin_openapi["paths"]
    assert "/api/v1/realtime/classify" in realtime_openapi["paths"]
    assert "/api/v1/batch/internal/chunks/{batch_job_id}/{chunk_no}:execute" in batch_openapi["paths"]


def test_python_contract_models_include_csrf_and_auth_support_fields() -> None:
    assert "csrf_token" in SessionResponse.model_fields
    assert "csrf_cookie_name" in AuthSupportSummary.model_fields
    assert "csrf_header_name" in AuthSupportSummary.model_fields
    assert "api_key_auth_enabled" in AuthSupportSummary.model_fields
    assert "api_key_bootstrap_count" in AuthSupportSummary.model_fields


def test_platform_settings_schema_includes_security_sections() -> None:
    schema = PlatformSettings.model_json_schema()
    properties = schema["properties"]
    session_properties = schema["$defs"]["SessionConfig"]["properties"]
    label_studio_properties = schema["$defs"]["LabelStudioConfig"]["properties"]

    assert "api_keys" in properties
    assert "cors" in properties
    assert "request_limits" in properties
    assert "uploads" in properties
    assert "csrf_cookie_name" in session_properties
    assert "csrf_header_name" in session_properties
    assert "same_site" in session_properties
    assert "local_files_root" in label_studio_properties
    assert "local_files_url_prefix" in label_studio_properties


def test_exported_contract_files_include_security_artifacts() -> None:
    config_schema = Path("config/schemas/platform-settings.schema.json").read_text(encoding="utf-8")
    admin_openapi = Path("contracts/openapi/admin-api.v1.yaml").read_text(encoding="utf-8")

    assert '"api_keys"' in config_schema
    assert '"cors"' in config_schema
    assert '"request_limits"' in config_schema
    assert '"uploads"' in config_schema
    assert "upload-sessions" in admin_openapi
    assert "auth/me" in admin_openapi
