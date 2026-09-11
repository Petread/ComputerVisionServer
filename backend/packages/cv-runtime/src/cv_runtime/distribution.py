"""Installed runtime identity and the portable node catalogue used by server and Edge."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from cv_status import RuntimeNodeCategory

from cv_runtime.registry import builtin_node_types

# These sources require a central media lookup or coordinated camera acquisition.
# Their absence is advertised, rather than claiming a capability without its adapter.
EDGE_UNAVAILABLE_NODES = frozenset(
    {
        "source.synchronized_camera_frames",
        "source.channel_snapshot",
        "source.trigger_snapshot",
        "source.test_photo",
    }
)


@lru_cache(maxsize=1)
def runtime_code_digest() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode())
        digest.update(b"\0")
    return digest.hexdigest()


def edge_node_contracts() -> dict[str, int]:
    return {
        node.node_type: node.contract_version
        for node in builtin_node_types()
        if node.node_type not in EDGE_UNAVAILABLE_NODES
        and node.node_category != RuntimeNodeCategory.CAPTURE
        and node.compatibility_constraints.get("edge_supported", True)
    }
