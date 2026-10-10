from contextvars import ContextVar

from cv_utils import PlatformError

runtime_inference_releases: ContextVar[frozenset[str] | None] = ContextVar(
    "runtime_inference_releases",
    default=None,
)

operator_direct_inference: ContextVar[bool] = ContextVar(
    "operator_direct_inference",
    default=False,
)


def require_bound_release(release_id: str | None = None) -> None:
    allowed = runtime_inference_releases.get()
    if allowed is None or (release_id is not None and release_id not in allowed):
        raise PlatformError(
            code="runtime_pipeline_required",
            status_code=409,
            message="Execute inference through a started channel's runtime pipeline.",
        )
