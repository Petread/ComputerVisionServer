from __future__ import annotations

import http.client
import io
import json
import socket
import tarfile
from pathlib import Path
from urllib.parse import quote


class DockerApiError(RuntimeError):
    pass


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerSocketClient:
    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    @property
    def available(self) -> bool:
        return Path(self.socket_path).exists()

    def containers_by_service(self, project_name: str, service_name: str) -> list[dict[str, object]]:
        filters = {
            "label": [
                f"com.docker.compose.project={project_name}",
                f"com.docker.compose.service={service_name}",
            ]
        }
        path = f"/containers/json?all=true&filters={quote(json.dumps(filters))}"
        payload = self._request_json("GET", path)
        if not isinstance(payload, list):
            raise DockerApiError("Docker returned an unexpected container list payload.")
        return [item for item in payload if isinstance(item, dict)]

    def copy_files_to_service(
        self,
        *,
        project_name: str,
        service_name: str,
        repo_root: Path,
        relative_paths: list[str],
        target_root: str = "/workspace",
    ) -> None:
        containers = self.containers_by_service(project_name, service_name)
        if not containers:
            raise DockerApiError(f"No container found for service {service_name}.")
        archive = self._build_archive(repo_root=repo_root, relative_paths=relative_paths)
        if archive is None:
            return
        for container in containers:
            container_id = str(container.get("Id") or "")
            if not container_id:
                continue
            path = f"/containers/{container_id}/archive?path={quote(target_root)}"
            self._request_bytes(
                "PUT",
                path,
                archive,
                headers={"Content-Type": "application/x-tar"},
                expected=(200,),
            )

    def restart_service(self, *, project_name: str, service_name: str, timeout_seconds: int = 10) -> None:
        containers = self.containers_by_service(project_name, service_name)
        if not containers:
            raise DockerApiError(f"No container found for service {service_name}.")
        for container in containers:
            container_id = str(container.get("Id") or "")
            if not container_id:
                continue
            self._request_bytes(
                "POST",
                f"/containers/{container_id}/restart?t={timeout_seconds}",
                b"",
                expected=(204,),
            )

    def _request_json(self, method: str, path: str) -> object:
        body = self._request_bytes(method, path, b"", expected=(200,))
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _request_bytes(
        self,
        method: str,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...],
    ) -> bytes:
        connection = UnixSocketHTTPConnection(self.socket_path)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read()
        finally:
            connection.close()
        if response.status not in expected:
            detail = response_body.decode("utf-8", errors="ignore")
            raise DockerApiError(f"Docker API {method} {path} failed with HTTP {response.status}: {detail}")
        return response_body

    def _build_archive(self, *, repo_root: Path, relative_paths: list[str]) -> bytes | None:
        buffer = io.BytesIO()
        added = 0
        resolved_root = repo_root.resolve()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for relative_path in sorted(set(relative_paths)):
                source = (repo_root / relative_path).resolve()
                if resolved_root not in source.parents and source != resolved_root:
                    raise DockerApiError(f"Refusing to copy path outside repository: {relative_path}")
                if not source.is_file() or source.is_symlink():
                    continue
                info = archive.gettarinfo(str(source), arcname=relative_path)
                info.uid = 0
                info.gid = 0
                info.mode = 0o644
                with source.open("rb") as handle:
                    archive.addfile(info, handle)
                added += 1
        if added == 0:
            return None
        return buffer.getvalue()
