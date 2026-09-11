from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx


class LabelStudioError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LabelStudioClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._access_token_expires_at: int = 0

    def list_projects(self) -> list[dict[str, Any]]:
        path: str | None = "/api/projects/"
        projects: list[dict[str, Any]] = []
        while path:
            payload = self._request("GET", path)
            if isinstance(payload, dict):
                results = payload.get("results")
                if isinstance(results, list):
                    projects.extend([item for item in results if isinstance(item, dict)])
                    next_path = payload.get("next")
                    path = self._api_request_path(str(next_path)) if next_path else None
                    continue
            if isinstance(payload, list):
                projects.extend([item for item in payload if isinstance(item, dict)])
                break
            raise LabelStudioError("Label Studio returned an unexpected project listing payload.")
        return projects

    def create_project(
        self,
        *,
        title: str,
        label_config: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/projects/",
            json={
                "title": title,
                "label_config": label_config,
                "description": description,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise LabelStudioError("Label Studio did not return created project metadata.")
        return payload

    def get_project(self, project_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/api/projects/{project_id}/")
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise LabelStudioError("Label Studio returned unexpected project metadata.")
        return payload

    def update_project(
        self,
        project_id: int,
        *,
        title: str,
        label_config: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/api/projects/{project_id}/",
            json={
                "title": title,
                "label_config": label_config,
                "description": description,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise LabelStudioError("Label Studio did not return updated project metadata.")
        return payload

    def create_export_snapshot(self, project_id: int) -> dict[str, Any]:
        payload = self._request("POST", f"/api/projects/{project_id}/exports/", json={})
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio did not return export snapshot metadata.")
        return payload

    def get_export_snapshot(self, project_id: int, export_id: int) -> dict[str, Any]:
        payload = self._request("GET", f"/api/projects/{project_id}/exports/{export_id}")
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio returned an unexpected export snapshot payload.")
        return payload

    def download_export_snapshot(self, project_id: int, export_id: int, export_type: str) -> bytes:
        response = self._raw_request(
            "GET",
            f"/api/projects/{project_id}/exports/{export_id}/download",
            params={"exportType": export_type},
        )
        return response.content

    def download_asset(self, asset_ref: str) -> tuple[bytes, str | None]:
        response = self._raw_request("GET", self._asset_request_path(asset_ref))
        return response.content, response.headers.get("content-type")

    def delete_project(self, project_id: int) -> None:
        try:
            self._raw_request("DELETE", f"/api/projects/{project_id}/")
        except LabelStudioError as exc:
            if exc.status_code == 404:
                return
            raise

    def import_tasks(self, project_id: int, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._request("POST", f"/api/projects/{project_id}/import", json=tasks)
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio did not return task import metadata.")
        return payload

    def import_tasks_with_ids(
        self,
        project_id: int,
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import tasks and request their IDs from Label Studio's bulk import API."""

        payload = self._request(
            "POST",
            f"/api/projects/{project_id}/import",
            params={"return_task_ids": "true"},
            json=tasks,
        )
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio did not return task import metadata.")
        return payload

    def get_project_import(self, project_id: int, import_id: int) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/api/projects/{project_id}/imports/{import_id}",
        )
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio returned unexpected import metadata.")
        return payload

    def list_project_tasks(
        self,
        project_id: int,
        *,
        maximum_tasks: int | None = None,
    ) -> list[dict[str, Any]]:
        """List project tasks for idempotent import recovery."""

        page = 1
        tasks: list[dict[str, Any]] = []
        while maximum_tasks is None or len(tasks) < maximum_tasks:
            payload = self._request(
                "GET",
                "/api/tasks/",
                params={"project": project_id, "page": page, "page_size": 100},
            )
            if isinstance(payload, list):
                tasks.extend(item for item in payload if isinstance(item, dict))
                break
            if not isinstance(payload, dict):
                raise LabelStudioError(
                    "Label Studio returned an unexpected task listing payload."
                )
            raw_items = payload.get("tasks")
            if not isinstance(raw_items, list):
                raw_items = payload.get("results")
            if not isinstance(raw_items, list):
                raise LabelStudioError(
                    "Label Studio returned an unexpected task listing payload."
                )
            items = [item for item in raw_items if isinstance(item, dict)]
            tasks.extend(items)
            total = payload.get("total")
            if not isinstance(total, int):
                total = payload.get("count")
            if (
                not payload.get("next")
                and not (isinstance(total, int) and len(tasks) < total)
            ) or not items:
                break
            page += 1
        return tasks if maximum_tasks is None else tasks[:maximum_tasks]

    def predict_with_ml_backend(self, backend_url: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        response = self._raw_request("POST", backend_url, json={"tasks": tasks})
        try:
            payload = response.json()
        except ValueError as exc:
            raise LabelStudioError("Label Studio ML backend returned a non-JSON response.") from exc
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio ML backend returned an unexpected prediction payload.")
        return payload

    def list_ml_backends(self, project_id: int) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/ml/", params={"project": project_id})
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]
        raise LabelStudioError("Label Studio returned an unexpected ML backend listing payload.")

    def create_ml_backend(
        self,
        *,
        project_id: int,
        url: str,
        title: str,
        description: str | None = None,
        extra_params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
        is_interactive: bool = False,
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/ml/",
            json={
                "project": project_id,
                "url": url,
                "title": title,
                "description": description,
                "auth_method": "NONE",
                "auto_update": True,
                "is_interactive": is_interactive,
                "timeout": timeout_seconds,
                "extra_params": extra_params or {},
            },
        )
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio did not return ML backend metadata.")
        return payload

    def update_ml_backend(self, backend_id: int, **fields: object) -> dict[str, Any]:
        payload = self._request("PATCH", f"/api/ml/{backend_id}", json=fields)
        if not isinstance(payload, dict):
            raise LabelStudioError("Label Studio did not return ML backend metadata.")
        return payload

    def _request(self, method: str, path: str, **kwargs: object) -> object:
        response = self._raw_request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise LabelStudioError("Label Studio returned a non-JSON response.") from exc

    def _raw_request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        raw_headers = kwargs.pop("headers", {})
        headers = dict(raw_headers) if isinstance(raw_headers, dict) else {}
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout_seconds) as client:
                headers["Authorization"] = self._authorization_header(client)
                response = client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise LabelStudioError(f"Label Studio request failed: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:400]
            raise LabelStudioError(
                f"Label Studio request failed with status {exc.response.status_code}: {detail}",
                status_code=exc.response.status_code,
            ) from exc
        return response

    def _authorization_header(self, client: httpx.Client) -> str:
        if not self._looks_like_jwt(self._token):
            return f"Token {self._token}"
        access_token = self._current_access_token(client)
        return f"Bearer {access_token}"

    def _asset_request_path(self, asset_ref: str) -> str:
        return self._proxied_request_path(asset_ref)

    def _api_request_path(self, api_ref: str) -> str:
        return self._proxied_request_path(api_ref)

    def _proxied_request_path(self, ref: str) -> str:
        parsed = urlparse(ref)
        path = parsed.path or ref
        base_path = urlparse(self._base_url).path.rstrip("/")
        if not base_path and path.startswith("/label-studio/"):
            path = path.removeprefix("/label-studio")
        if not path.startswith("/"):
            path = f"/{path}"
        if parsed.query:
            return f"{path}?{parsed.query}"
        return path

    def _current_access_token(self, client: httpx.Client) -> str:
        now = int(time.time())
        if self._access_token and now < self._access_token_expires_at - 30:
            return self._access_token
        refreshed_access_token = self._refresh_access_token(client)
        if refreshed_access_token is not None:
            return refreshed_access_token
        return self._token

    def _refresh_access_token(self, client: httpx.Client) -> str | None:
        try:
            response = client.request("POST", "/api/token/refresh/", json={"refresh": self._token})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        access_token = payload.get("access")
        if not isinstance(access_token, str) or not access_token:
            return None
        self._access_token = access_token
        self._access_token_expires_at = self._jwt_exp(access_token)
        return access_token

    @staticmethod
    def _looks_like_jwt(token: str) -> bool:
        return token.count(".") == 2

    @staticmethod
    def _jwt_exp(token: str) -> int:
        try:
            payload_segment = token.split(".")[1]
            padded_payload = payload_segment + "=" * (-len(payload_segment) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded_payload).decode("utf-8"))
            exp = payload.get("exp")
        except (IndexError, ValueError, TypeError, json.JSONDecodeError, binascii.Error):
            return 0
        return int(exp) if isinstance(exp, int) else 0
