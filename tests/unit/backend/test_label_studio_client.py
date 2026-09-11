from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cv_datasets.label_studio import LabelStudioClient, LabelStudioError


def _jwt_with_exp(offset_seconds: int) -> str:
    payload = {"exp": int(time.time()) + offset_seconds}
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


class _StubClient:
    def __init__(self, responder):
        self._responder = responder

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def request(self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs):
        return self._responder(method, path, headers or {}, **kwargs)


def test_label_studio_client_uses_legacy_token_for_non_jwt(monkeypatch) -> None:
    observed_headers: list[str] = []
    observed_params: list[object] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed_headers.append(headers["Authorization"])
        observed_params.append(kwargs.get("params"))
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        return httpx.Response(200, request=request, json={"count": 0, "next": None, "results": []})

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")
    projects = client.list_projects()

    assert projects == []
    assert observed_headers == ["Token legacy-api-token"]
    assert observed_params == [None]


def test_label_studio_client_refreshes_jwt_and_uses_bearer_access_token(monkeypatch) -> None:
    refresh_token = _jwt_with_exp(3600)
    access_token = _jwt_with_exp(3600)
    observed_headers: list[str] = []
    observed_params: list[object] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if path == "/api/token/refresh/":
            return httpx.Response(200, request=request, json={"access": access_token})
        observed_headers.append(headers["Authorization"])
        observed_params.append(kwargs.get("params"))
        return httpx.Response(
            200,
            request=request,
            json={"count": 1, "next": None, "results": [{"id": 7, "title": "Test Project"}]},
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token=refresh_token)
    projects = client.list_projects()

    assert projects == [{"id": 7, "title": "Test Project"}]
    assert observed_headers == [f"Bearer {access_token}"]
    assert observed_params == [None]


def test_label_studio_client_falls_back_to_direct_bearer_when_refresh_fails(monkeypatch) -> None:
    direct_bearer_token = _jwt_with_exp(3600)
    observed_headers: list[str] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if path == "/api/token/refresh/":
            return httpx.Response(401, request=request, json={"detail": "invalid refresh"})
        observed_headers.append(headers["Authorization"])
        return httpx.Response(200, request=request, json={"count": 0, "next": None, "results": []})

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token=direct_bearer_token)
    projects = client.list_projects()

    assert projects == []
    assert observed_headers == [f"Bearer {direct_bearer_token}"]


def test_label_studio_client_strips_proxy_prefix_from_paginated_project_links(
    monkeypatch,
) -> None:
    observed_paths: list[str] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed_paths.append(path)
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if path == "/api/projects/":
            return httpx.Response(
                200,
                request=request,
                json={
                    "count": 2,
                    "next": "http://localhost/label-studio/api/projects/?page=2",
                    "results": [{"id": 1, "title": "First"}],
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={"count": 2, "next": None, "results": [{"id": 2, "title": "Second"}]},
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")
    projects = client.list_projects()

    assert projects == [{"id": 1, "title": "First"}, {"id": 2, "title": "Second"}]
    assert observed_paths == ["/api/projects/", "/api/projects/?page=2"]


def test_label_studio_client_download_asset_strips_proxy_prefix(monkeypatch) -> None:
    observed_paths: list[str] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed_paths.append(path)
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        return httpx.Response(
            200,
            request=request,
            content=b"asset-bytes",
            headers={"content-type": "image/jpeg"},
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")
    content, content_type = client.download_asset(
        "/label-studio/data/upload/2/b66d87a2-Image_48.jpg"
    )

    assert content == b"asset-bytes"
    assert content_type == "image/jpeg"
    assert observed_paths == ["/data/upload/2/b66d87a2-Image_48.jpg"]


def test_label_studio_client_manages_ml_backends(monkeypatch) -> None:
    observed: list[tuple[str, str, object]] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed.append((method, path, kwargs.get("json") or kwargs.get("params")))
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if method == "GET":
            return httpx.Response(200, request=request, json=[{"id": 12, "title": "CV Platform"}])
        return httpx.Response(
            200 if method == "PATCH" else 201,
            request=request,
            json={"id": 12, "project": 7, "url": "http://adapter/projects/lpr_1"},
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")

    assert client.list_ml_backends(7) == [{"id": 12, "title": "CV Platform"}]
    assert client.create_ml_backend(
        project_id=7,
        url="http://adapter/projects/lpr_1",
        title="CV Platform",
        extra_params={"label_project_id": "lpr_1"},
    )["id"] == 12
    assert client.update_ml_backend(12, url="http://adapter/projects/lpr_1")["id"] == 12
    assert observed == [
        ("GET", "/api/ml/", {"project": 7}),
        (
            "POST",
            "/api/ml/",
            {
                "project": 7,
                "url": "http://adapter/projects/lpr_1",
                "title": "CV Platform",
                "description": None,
                "auth_method": "NONE",
                "auto_update": True,
                "is_interactive": False,
                "timeout": 30,
                "extra_params": {"label_project_id": "lpr_1"},
            },
        ),
        ("PATCH", "/api/ml/12", {"url": "http://adapter/projects/lpr_1"}),
    ]


def test_label_studio_client_calls_prediction_backend_with_absolute_url(monkeypatch) -> None:
    observed: list[tuple[str, str, object]] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed.append((method, path, kwargs.get("json")))
        request = httpx.Request(method, path)
        return httpx.Response(
            200,
            request=request,
            json={"model_version": "detector:v1", "predictions": [{"result": []}]},
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )

    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")
    payload = client.predict_with_ml_backend(
        "http://adapter/api/v1/projects/lpr_1/predict",
        [{"data": {"image": "/data/local-files/?d=runtime/capture.jpg"}}],
    )

    assert payload["model_version"] == "detector:v1"
    assert observed == [
        (
            "POST",
            "http://adapter/api/v1/projects/lpr_1/predict",
            {"tasks": [{"data": {"image": "/data/local-files/?d=runtime/capture.jpg"}}]},
        )
    ]


def test_label_studio_client_manages_projects_with_supported_http_contract(monkeypatch) -> None:
    observed: list[tuple[str, str, object]] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed.append((method, path, kwargs.get("json")))
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if method == "DELETE":
            return httpx.Response(204, request=request)
        status = 201 if method == "POST" else 200
        return httpx.Response(
            status,
            request=request,
            json={
                "id": 73,
                "title": "Stem sequence",
                "label_config": '<View><Video name="video" value="$video"/></View>',
            },
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )
    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")
    label_config = '<View><Video name="video" value="$video"/></View>'

    assert client.create_project(
        title="Stem sequence",
        label_config=label_config,
        description="provisioning marker",
    )["id"] == 73
    assert client.get_project(73)["id"] == 73
    assert client.update_project(
        73,
        title="Stem sequence",
        label_config=label_config,
        description="provisioning marker",
    )["id"] == 73
    client.delete_project(73)

    expected_payload = {
        "title": "Stem sequence",
        "label_config": label_config,
        "description": "provisioning marker",
    }
    assert observed == [
        ("POST", "/api/projects/", expected_payload),
        ("GET", "/api/projects/73/", None),
        ("PATCH", "/api/projects/73/", expected_payload),
        ("DELETE", "/api/projects/73/", None),
    ]


def test_label_studio_client_exposes_http_status_and_treats_delete_404_as_compensated(
    monkeypatch,
) -> None:
    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        return httpx.Response(404, request=request, json={"detail": "not found"})

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )
    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")

    with pytest.raises(LabelStudioError) as error:
        client.get_project(404)
    assert error.value.status_code == 404

    client.delete_project(404)


def test_label_studio_client_imports_with_ids_and_lists_project_tasks(monkeypatch) -> None:
    observed: list[tuple[str, str, object]] = []

    def responder(method: str, path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
        observed.append((method, path, kwargs.get("params") or kwargs.get("json")))
        request = httpx.Request(method, f"http://label-studio:8080{path}")
        if method == "POST":
            return httpx.Response(201, request=request, json={"task_ids": [41]})
        page = (kwargs.get("params") or {}).get("page")
        return httpx.Response(
            200,
            request=request,
            json={
                "total": 2,
                "tasks": [{"id": 41}] if page == 1 else [{"id": 42}],
                "next": "next" if page == 1 else None,
            },
        )

    monkeypatch.setattr(
        "cv_datasets.label_studio.httpx.Client",
        lambda *args, **kwargs: _StubClient(responder),
    )
    client = LabelStudioClient(base_url="http://label-studio:8080", token="legacy-api-token")

    assert client.import_tasks_with_ids(7, [{"data": {"video": "asset.mp4"}}]) == {
        "task_ids": [41]
    }
    assert client.list_project_tasks(7) == [{"id": 41}, {"id": 42}]
    assert observed == [
        (
            "POST",
            "/api/projects/7/import",
            {"return_task_ids": "true"},
        ),
        (
            "GET",
            "/api/tasks/",
            {"project": 7, "page": 1, "page_size": 100},
        ),
        (
            "GET",
            "/api/tasks/",
            {"project": 7, "page": 2, "page_size": 100},
        ),
    ]
