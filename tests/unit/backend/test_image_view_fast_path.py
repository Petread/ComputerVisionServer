from __future__ import annotations

import cv2
import cv_runtime.image_view as image_view_module
import numpy as np
from cv_contracts.runtime import (
    RuntimeImageViewProfileCalculateRequest,
    RuntimeNormalizedImagePoint,
)
from cv_runtime.image_view import (
    apply_image_view,
    calculate_image_view_profile,
    source_points_to_view,
)


def test_prepared_masked_crop_matches_reference_and_reuses_preparation() -> None:
    result = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="stem-measurement-view-v1",
            source_image_width=160,
            source_image_height=120,
            intrinsic_calibration_hash="a" * 64,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.1, y=0.15),
                RuntimeNormalizedImagePoint(x=0.9, y=0.1),
                RuntimeNormalizedImagePoint(x=0.85, y=0.9),
                RuntimeNormalizedImagePoint(x=0.15, y=0.85),
            ],
            output_width=100,
            output_height=60,
            transform_type="POLYGON_MASKED_CROP",
            interpolation="LINEAR",
        ),
        generated_at="2026-08-05T10:01:00Z",
    )
    assert result.profile is not None
    profile = result.profile
    matrix = np.asarray(profile.source_to_view_homography, dtype=np.float64).reshape(3, 3)
    yy, xx = np.indices((120, 160), dtype=np.uint16)
    image = np.stack(
        (
            (xx % 256).astype(np.uint8),
            (yy % 256).astype(np.uint8),
            ((xx * 3 + yy * 5) % 256).astype(np.uint8),
        ),
        axis=2,
    )
    reference = cv2.warpPerspective(
        image,
        matrix,
        (profile.output_width, profile.output_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=profile.padding_value,
    )
    source_polygon = np.asarray(
        [[point.x * 159.0, point.y * 119.0] for point in profile.source_quadrilateral],
        dtype=np.float64,
    )
    view_polygon = source_points_to_view(source_polygon, profile)
    polygon_mask = np.zeros((profile.output_height, profile.output_width), dtype=np.uint8)
    cv2.fillPoly(polygon_mask, [np.rint(view_polygon).astype(np.int32)], 255)
    reference = cv2.bitwise_and(reference, reference, mask=polygon_mask)

    image_view_module._prepared_image_view.cache_clear()
    first = apply_image_view(image, profile)
    second = apply_image_view(image, profile)
    cache = image_view_module._prepared_image_view.cache_info()

    assert np.array_equal(first, reference)
    assert np.array_equal(second, first)
    assert cache.misses == 1
    assert cache.hits == 1
