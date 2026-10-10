import os

SERVICE_NAME = "realtime-executor"


def _configure_native_threads() -> None:
    """Apply an explicit executor limit before importing its numerical modules."""
    configured = os.environ.get("YBB_REALTIME_EXECUTOR_NATIVE_THREADS")
    if configured is None:
        return
    if not configured.isascii() or not configured.isdecimal() or int(configured) < 1:
        raise ValueError("YBB_REALTIME_EXECUTOR_NATIVE_THREADS must be a positive integer")
    for name in (
        "OPENBLAS_NUM_THREADS", "GOTO_NUM_THREADS", "BLIS_NUM_THREADS",
        "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OPENCV_FOR_THREADS_NUM",
    ):
        os.environ[name] = configured
    import cv2

    cv2.setNumThreads(int(configured))


_configure_native_threads()
