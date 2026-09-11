from __future__ import annotations

from cv_core import PlatformSettings


def gpu_admission_allowed(settings: PlatformSettings) -> bool:
    return settings.gpu.allow_training_start

