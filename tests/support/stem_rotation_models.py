from __future__ import annotations

import math
import random
from collections.abc import Callable

from cv_runtime.stem_rotation_types import StemSliceGeometry, StemSliceModel


def build_stem_model(
    builder: Callable[[int, float], StemSliceGeometry],
    *,
    count: int = 9,
    spacing_mm: float = 50.0,
    model_quality: float = 1.0,
) -> StemSliceModel:
    return StemSliceModel(
        slices=tuple(builder(index, index * spacing_mm) for index in range(count)),
        model_quality=model_quality,
    )


def straight_round_stem(*, count: int = 9, radius_mm: float = 100.0) -> StemSliceModel:
    return build_stem_model(
        lambda _index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=0.0,
            cz_mm=0.0,
            a_mm=radius_mm,
            b_mm=radius_mm,
            phi_rad=0.0,
        ),
        count=count,
    )


def strongly_oval_stem(*, count: int = 9) -> StemSliceModel:
    return build_stem_model(
        lambda _index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=0.0,
            cz_mm=0.0,
            a_mm=130.0,
            b_mm=70.0,
            phi_rad=0.0,
        ),
        count=count,
    )


def strongly_curved_stem(*, count: int = 9) -> StemSliceModel:
    middle = (count - 1) / 2.0
    return build_stem_model(
        lambda index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=35.0 * math.sin((index - middle) / max(middle, 1.0) * math.pi / 2.0),
            cz_mm=18.0 * math.cos((index - middle) / max(middle, 1.0) * math.pi / 2.0),
            a_mm=105.0,
            b_mm=105.0,
            phi_rad=0.0,
        ),
        count=count,
    )


def aligned_curvature_and_ovality_stem(*, count: int = 9) -> StemSliceModel:
    return build_stem_model(
        lambda index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=4.0 * index,
            cz_mm=0.0,
            a_mm=125.0,
            b_mm=80.0,
            phi_rad=0.0,
        ),
        count=count,
    )


def opposed_curvature_and_ovality_stem(*, count: int = 9) -> StemSliceModel:
    return build_stem_model(
        lambda index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=4.0 * index,
            cz_mm=0.0,
            a_mm=125.0,
            b_mm=80.0,
            phi_rad=math.pi / 2.0,
        ),
        count=count,
    )


def local_bottleneck_stem(*, count: int = 9) -> StemSliceModel:
    bottleneck_index = count // 2
    return build_stem_model(
        lambda index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=0.0,
            cz_mm=0.0,
            a_mm=100.0 if index != bottleneck_index else 78.0,
            b_mm=90.0 if index != bottleneck_index else 68.0,
            phi_rad=0.0,
        ),
        count=count,
    )


def noisy_stem(*, count: int = 13, seed: int = 20260820) -> StemSliceModel:
    random_source = random.Random(seed)
    return build_stem_model(
        lambda _index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=random_source.gauss(0.0, 2.0),
            cz_mm=random_source.gauss(0.0, 2.0),
            a_mm=110.0 + random_source.gauss(0.0, 2.5),
            b_mm=85.0 + random_source.gauss(0.0, 2.0),
            phi_rad=math.radians(25.0 + random_source.gauss(0.0, 2.0)),
            quality=0.9,
        ),
        count=count,
    )


def stem_with_short_gap(*, count: int = 9) -> StemSliceModel:
    gap_index = count // 2
    return build_stem_model(
        lambda index, x_mm: StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=0.1 * x_mm,
            cz_mm=-0.05 * x_mm,
            a_mm=110.0,
            b_mm=90.0,
            phi_rad=math.radians(20.0),
            valid=index != gap_index,
            quality=1.0 if index != gap_index else 0.0,
        ),
        count=count,
    )
