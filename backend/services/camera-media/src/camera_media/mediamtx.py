from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any
from urllib.error import URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from cv_core import PlatformSettings


def load_mediamtx_paths(
    settings: PlatformSettings,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    api_url = settings.runtime.stream_mediamtx_api_url.rstrip("/")
    if not api_url:
        return None, "MediaMTX API URL is not configured."
    try:
        request = mediamtx_request(f"{api_url}/v3/paths/list")
        with urlopen(request, timeout=0.4) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
        return None, f"MediaMTX API is unreachable: {exc}"
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None, "MediaMTX API returned an invalid path response."
    paths: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            paths[name] = item
    return paths, None


def mediamtx_request(url: str) -> Request:
    parts = urlsplit(url)
    if parts.username is None:
        return Request(url)
    username = unquote(parts.username)
    password = unquote(parts.password or "")
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    clean_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return Request(clean_url, headers={"Authorization": f"Basic {token}"})


def path_is_ready(path_info: dict[str, Any] | None) -> bool:
    if not isinstance(path_info, dict):
        return False
    return bool(path_info.get("ready", path_info.get("sourceReady", False)))


def path_reader_count(path_info: dict[str, Any] | None) -> int:
    if not isinstance(path_info, dict):
        return 0
    readers = path_info.get("readers")
    if isinstance(readers, list):
        return len(readers)
    value = path_info.get("readerCount")
    return max(int(value), 0) if isinstance(value, int | float) else 0
