from pathlib import Path


def test_prod_reverse_proxy_sets_security_headers_and_blocks_internal_paths() -> None:
    content = Path("infra/reverse-proxy/Caddyfile.prod").read_text(encoding="utf-8")

    assert 'X-Content-Type-Options "nosniff"' in content
    assert 'Referrer-Policy "same-origin"' in content
    assert 'X-Frame-Options "SAMEORIGIN"' in content
    assert "/datasets-service*" in content
    assert "/training-orchestrator*" in content
    assert "/model-registry*" in content
    assert "/deployment-controller*" in content
    assert "/webhook-receiver*" in content
    assert "/inference-batch/api/v1/batch/internal/*" in content


def test_prod_frontend_env_does_not_point_operators_at_blocked_internal_paths() -> None:
    content = Path("infra/env/platform.prod.env.example").read_text(encoding="utf-8")

    assert "VITE_ADMIN_API_URL=" in content
    assert "VITE_RUNTIME_ADMIN_API_URL=/runtime-api" in content
    assert "VITE_INFERENCE_GATEWAY_RT_URL=/inference-rt" in content
    assert "VITE_INFERENCE_GATEWAY_BATCH_URL=/inference-batch" in content
    assert "VITE_FRONTEND_BASE_PATH=/frontend/" in content
    assert "/datasets-service" not in content
    assert "/training-orchestrator" not in content
    assert "/model-registry" not in content
    assert "/deployment-controller" not in content


def test_reverse_proxy_exposes_label_studio_asset_routes() -> None:
    dev_content = Path("infra/reverse-proxy/Caddyfile.dev").read_text(encoding="utf-8")
    prod_content = Path("infra/reverse-proxy/Caddyfile.prod").read_text(encoding="utf-8")

    for content in (dev_content, prod_content):
        assert "@label_studio_prefixed_local_files path /label-studio/data/local-files /label-studio/data/local-files/" in content
        assert "handle @label_studio_prefixed_local_files" in content
        assert "@label_studio_local_files path /data/local-files /data/local-files/" in content
        assert "handle @label_studio_local_files" in content
        assert "handle /data/upload/*" in content
        assert "handle /static/*" in content
        assert "handle /react-app/*" in content
        assert "handle /label-studio/ {" in content
        assert "handle_path /label-studio/*" in content
        assert "handle /frontend/assets/*" in content
        assert "handle_path /frontend/*" in content
        assert "redir / /frontend/ 308" in content
        assert "handle_path /label-studio/* {\n    uri strip_prefix /label-studio" not in content
        assert 'header Access-Control-Allow-Origin "*"' in content
        assert "file_server" in content
        assert content.count("reverse_proxy label-studio:8080") >= 4
