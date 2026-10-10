"""Pure geometry and metrology for the end-face photograph/CSV workflow.

No registration here constitutes an independent calibration certificate. The
scanner polygon and image boundaries have separate, externally measured errors.
"""

# ruff: noqa: ANN401 -- NumPy/OpenCV arrays are runtime dependencies.
from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from cv_contracts.bark import BarkRadialSample, StemCsvMeasurement, StemCsvSlice


def polygon_centroid(points: Any) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    following = np.roll(points, -1, axis=0)
    cross = points[:, 0] * following[:, 1] - following[:, 0] * points[:, 1]
    if abs(float(cross.sum())) < 1e-6:
        raise ValueError("csv_polygon_degenerate")
    return ((points + following) * cross[:, None]).sum(axis=0) / (3 * cross.sum())


def validate_polygon(points: Any) -> None:
    points = np.asarray(points, dtype=float)
    if not np.isfinite(points).all() or len(np.unique(points, axis=0)) != len(points):
        raise ValueError("csv_polygon_invalid_points")
    polygon_centroid(points)

    # Including collinear contacts: a self-touching outline is not a simple slice.
    def cross(a: Any, b: Any, c: Any) -> float:
        ab, ac = b - a, c - a
        return float(ab[0] * ac[1] - ab[1] * ac[0])

    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        for j in range(i + 2, len(points)):
            if i == 0 and j == len(points) - 1:
                continue
            c, d = points[j], points[(j + 1) % len(points)]
            if (
                np.maximum(np.minimum(a, b), np.minimum(c, d))
                > np.minimum(np.maximum(a, b), np.maximum(c, d)) + 1e-9
            ).any():
                continue
            if cross(a, b, c) * cross(a, b, d) <= 0 and cross(c, d, a) * cross(c, d, b) <= 0:
                raise ValueError("csv_polygon_self_intersection")


def parse_stem_csv(text: str, filename: str = "measurement.csv") -> StemCsvMeasurement:
    if len(text) > 1_000_000:
        raise ValueError("csv_too_large")
    rows = list(csv.reader(io.StringIO(text.removeprefix("\ufeff")), delimiter=";"))
    while rows and not rows[-1]:
        rows.pop()
    rows = [[v.strip() for v in row] for row in rows]
    if len(rows) < 8 or len(rows) > 76:
        raise ValueError("csv_requires_2_to_70_slices")
    if rows[0] != ["HFA_KRUEMMUNG_V1"]:
        raise ValueError("csv_format_unsupported")
    for i, count in enumerate([1, 2, 3, 4, 5, 2]):
        if len(rows[i]) != count or any(not v for v in rows[i]):
            raise ValueError(f"csv_header_invalid: line {i + 1}")

    def numbers(values: list[str], line: int) -> list[float]:
        try:
            result = [float(v) for v in values]
        except ValueError as exc:
            raise ValueError(f"csv_number_invalid: line {line}") from exc
        if not all(math.isfinite(v) for v in result):
            raise ValueError(f"csv_number_non_finite: line {line}")
        return result

    measured_at = datetime.strptime(" ".join(rows[2][1:]), "%d.%m.%Y %H:%M:%S").isoformat()
    length, nominal, speed = numbers([rows[3][0], rows[3][1], rows[3][3]], 4)
    slices: list[StemCsvSlice] = []
    for line, row in enumerate(rows[6:], 7):
        if len(row) != 73:
            raise ValueError(f"csv_slice_requires_73_values: line {line}")
        values = numbers(row, line)
        position = values[0] * 10.0
        if position < 0 or position > length + 0.1:
            raise ValueError(f"csv_slice_outside_length: line {line}")
        if slices and position <= slices[-1].position_mm:
            raise ValueError(f"csv_positions_not_strictly_increasing: line {line}")
        points = list(zip(values[1::2], values[2::2], strict=True))
        validate_polygon(points)
        slices.append(StemCsvSlice(csv_line=line, position_mm=position, points_mm=points))
    return StemCsvMeasurement(
        workpiece_id=rows[2][0],
        customer=rows[1][0],
        measurement_name=rows[1][1],
        measured_at=measured_at,
        length_mm=length,
        nominal_length_mm=nominal,
        leading_end=rows[3][2],
        speed_m_per_min=speed,
        diameters_and_shape_mm=numbers(rows[4], 5),
        volumes_m3=numbers(rows[5], 6),
        slices=slices,
        csv_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        csv_filename=filename,
    )


def project(points: Any, matrix: Any) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    h = np.column_stack((points, np.ones(len(points)))) @ np.asarray(matrix).reshape(3, 3).T
    if (np.abs(h[:, 2]) < 1e-9).any() or not np.isfinite(h).all():
        raise ValueError("registration_singular")
    return h[:, :2] / h[:, 2:]


def resample_contour(points: Any, count: int = 144) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    if np.any(lengths <= 1e-9):
        raise ValueError("contour_duplicate_point")
    cumulative = np.r_[0.0, np.cumsum(lengths)]
    samples = np.linspace(0, cumulative[-1], count, endpoint=False)
    indices = np.searchsorted(cumulative, samples, side="right") - 1
    return (
        points[indices]
        + edges[indices] * ((samples - cumulative[indices]) / lengths[indices])[:, None]
    )


def closest_on_polygon(points: Any, polygon: Any) -> tuple[np.ndarray, np.ndarray]:
    points, polygon = np.asarray(points), np.asarray(polygon)
    edges = np.roll(polygon, -1, axis=0) - polygon
    lengths2 = (edges * edges).sum(axis=1)
    offsets = points[:, None, :] - polygon[None, :, :]
    t = np.clip((offsets * edges).sum(axis=2) / np.maximum(lengths2, 1e-12), 0, 1)
    candidates = polygon[None, :, :] + t[:, :, None] * edges
    distances = ((points[:, None, :] - candidates) ** 2).sum(axis=2)
    indices = distances.argmin(axis=1)
    return candidates[np.arange(len(points)), indices], np.sqrt(distances.min(axis=1))


@dataclass
class ContourRegistration:
    valid: bool = False
    reasons: list[str] = field(default_factory=list)
    image_to_mm: Any = None
    error_mm: float | None = None
    stability_mm: float | None = None
    alternatives: list[Any] = field(default_factory=list)


def register_contours(
    image_contour: Any,
    metric_contour: Any,
    *,
    maximum_error_mm: float = 0.5,
    maximum_stability_mm: float = 0.25,
) -> ContourRegistration:
    import cv2

    image = resample_contour(image_contour)
    metric = np.asarray(metric_contour, dtype=float)
    target = resample_contour(metric)
    # Unmarked conics have projective automorphisms: outline agreement cannot
    # identify their interior metric. Reject before selecting an arbitrary fit.
    ellipse = cv2.fitEllipse(metric.astype(np.float32))
    center, diameters, angle = ellipse
    radians = math.radians(angle)
    rotation = np.array(
        [[math.cos(radians), math.sin(radians)], [-math.sin(radians), math.cos(radians)]]
    )
    ellipse_points = (metric - center) @ rotation.T / (np.asarray(diameters) / 2)
    if np.sqrt(np.mean((np.linalg.norm(ellipse_points, axis=1) - 1) ** 2)) < 0.015:
        return ContourRegistration(reasons=["registration_outline_ambiguous"])
    candidates: list[tuple[float, Any]] = []
    # Reflection is configured by the operator during splitting, but contour
    # traversal order is independent of physical mirroring.
    for contour in (target, target[::-1]):
        for shift in range(0, len(contour), 4):
            corresponding = np.roll(contour, shift, axis=0)
            affine = np.linalg.lstsq(np.c_[image, np.ones(len(image))], corresponding, rcond=None)[
                0
            ]
            matrix = np.vstack((affine.T, [0, 0, 1]))
            try:
                for _ in range(18):
                    mapped = project(image, matrix)
                    nearest, _ = closest_on_polygon(mapped, metric)
                    updated, _ = cv2.findHomography(image, nearest, method=0)
                    if updated is None:
                        break
                    delta = np.max(np.linalg.norm(project(image, updated) - mapped, axis=1))
                    matrix = updated
                    if delta < 0.001:
                        break
                mapped = project(image, matrix)
                _, forward = closest_on_polygon(mapped, metric)
                _, backward = closest_on_polygon(target, mapped)
                # A collapsed/folded warp is not an accepted match.
                homogeneous = np.c_[image, np.ones(len(image))] @ matrix.T
                if np.min(homogeneous[:, 2]) * np.max(homogeneous[:, 2]) <= 0:
                    continue
                error = float(np.sqrt(np.mean(np.r_[forward, backward] ** 2)))
                if np.linalg.cond(matrix) < 1e10 and np.isfinite(error):
                    candidates.append((error, matrix))
            except (ValueError, np.linalg.LinAlgError, cv2.error):
                continue
    if not candidates:
        return ContourRegistration(reasons=["registration_failed"])
    candidates.sort(key=lambda item: item[0])
    refined = []
    for _, matrix in candidates[:8]:
        try:
            for _ in range(180):
                mapped = project(image, matrix)
                nearest, _ = closest_on_polygon(mapped, metric)
                # Use both directions to constrain tangential drift on long edges.
                reverse, _ = closest_on_polygon(target, mapped)
                reverse_image = project(reverse, np.linalg.inv(matrix))
                updated, _ = cv2.findHomography(
                    np.vstack((image, reverse_image)), np.vstack((nearest, target)), method=0
                )
                if updated is None:
                    break
                delta = np.max(np.linalg.norm(project(image, updated) - mapped, axis=1))
                matrix = updated
                if delta < 0.0001:
                    break
            mapped = project(image, matrix)
            denominator = np.c_[image, np.ones(len(image))] @ matrix[2]
            if denominator.min() * denominator.max() <= 0 or np.linalg.cond(matrix) >= 1e10:
                continue
            _, forward = closest_on_polygon(mapped, metric)
            _, backward = closest_on_polygon(target, mapped)
            refined.append((float(np.sqrt(np.mean(np.r_[forward, backward] ** 2))), matrix))
        except (ValueError, np.linalg.LinAlgError, cv2.error):
            continue
    if not refined:
        return ContourRegistration(reasons=["registration_singular"])
    candidates = sorted(refined, key=lambda item: item[0])
    error, best = candidates[0]
    result = ContourRegistration(image_to_mm=best, error_mm=error)
    if error > maximum_error_mm:
        result.reasons.append("registration_contour_mismatch")
        return result
    mapped = project(image, best)
    _, forward = closest_on_polygon(mapped, metric)
    _, backward = closest_on_polygon(target, mapped)
    if np.max(np.r_[forward, backward]) > 2 * maximum_error_mm:
        result.reasons.append("registration_coverage_insufficient")
        return result
    center = polygon_centroid(metric)
    probes_mm = np.vstack((center, center + (target - center) * 0.5, target))
    probes_image = project(probes_mm, np.linalg.inv(best))
    alternatives = [
        matrix for score, matrix in candidates[1:] if score <= error + max(0.025, error * 0.1)
    ]
    # Deterministic half-pixel perturbations retain reproducibility. Refit the
    # contour (not fixed point pairs), so correspondence instability is included.
    rng = np.random.default_rng(0)
    for _ in range(12):
        noisy = image + rng.normal(0, 0.5, image.shape)
        current = best.copy()
        for _ in range(8):
            nearest, _ = closest_on_polygon(project(noisy, current), metric)
            current, _ = cv2.findHomography(noisy, nearest, method=0)
            if current is None:
                break
        if current is not None:
            alternatives.append(current)
    stability = max(
        (
            float(np.max(np.linalg.norm(project(probes_image, other) - probes_mm, axis=1)))
            for other in alternatives
        ),
        default=0.0,
    )
    result.stability_mm = stability
    result.alternatives = alternatives
    if stability > maximum_stability_mm:
        result.reasons.append("registration_unstable_or_ambiguous")
        return result
    result.valid = True
    return result


def radial_samples(
    outer: Any,
    wood: Any,
    bark: Any,
    unknown: Any,
    *,
    center_px: tuple[float, float],
    mm_per_pixel: float,
    count: int = 360,
) -> list[BarkRadialSample]:
    outer, wood, bark, unknown = (np.asarray(m, dtype=bool) for m in (outer, wood, bark, unknown))
    if any(m.shape != outer.shape for m in (wood, bark, unknown)):
        raise ValueError("measurement_mask_shape_mismatch")
    cx, cy = center_px
    height, width = outer.shape
    if not (0 <= round(cx) < width and 0 <= round(cy) < height and wood[round(cy), round(cx)]):
        return [
            BarkRadialSample(
                angle_deg=i * 360 / count, valid=False, reason="center_not_inside_wood"
            )
            for i in range(count)
        ]
    radii = np.arange(0, math.hypot(width, height) + 1, 0.25)
    output = []
    for index in range(count):
        angle = index * 2 * math.pi / count
        x = np.rint(cx + radii * math.cos(angle)).astype(int)
        y = np.rint(cy + radii * math.sin(angle)).astype(int)
        inside = (x >= 0) & (x < width) & (y >= 0) & (y < height)
        x, y, r = x[inside], y[inside], radii[inside]
        o, w, b, u = outer[y, x], wood[y, x], bark[y, x], unknown[y, x]
        sample = BarkRadialSample(angle_deg=index * 360 / count, valid=False)
        exits = np.flatnonzero(o[:-1] & ~o[1:])
        if not o[0] or len(exits) != 1 or o[-1]:
            sample.reason = "outer_boundary_ambiguous_or_clipped"
        else:
            end = int(exits[0])
            woods = np.flatnonzero(w[: end + 1])
            if not len(woods):
                sample.reason = "wood_missing"
            else:
                last_wood = int(woods[-1])
                # Interior cracks do not shorten the wood radius. Disconnected
                # bark inside it indicates multiple boundary crossings.
                if np.any(b[:last_wood] & ~u[:last_wood]):
                    sample.reason = "multiple_wood_bark_crossings"
                elif np.any(u[max(0, last_wood - 1) : end + 1]):
                    sample.reason = "boundary_unknown_or_occluded"
                elif not np.all(b[last_wood + 1 : end + 1]):
                    sample.reason = "bark_not_confirmed"
                else:
                    outer_r = (r[end] + r[end + 1]) / 2
                    wood_r = outer_r if last_wood == end else (r[last_wood] + r[last_wood + 1]) / 2
                    sample.valid = True
                    sample.wood_radius_mm = float(wood_r * mm_per_pixel)
                    sample.outer_radius_mm = float(outer_r * mm_per_pixel)
                    sample.thickness_mm = float((outer_r - wood_r) * mm_per_pixel)
        output.append(sample)
    return output


def summarize_samples(
    samples: list[BarkRadialSample],
    *,
    minimum_valid_fraction: float = 0.9,
    maximum_invalid_gap_deg: float = 10,
    include_extrema: bool = False,
) -> dict[str, Any]:
    valid = [s.thickness_mm for s in samples if s.valid and s.thickness_mm is not None]
    fraction = len(valid) / len(samples) if samples else 0
    longest = current = 0
    for item in samples * 2:
        current = 0 if item.valid else current + 1
        longest = max(current, longest)
    gap = min(longest, len(samples)) * 360 / len(samples) if samples else 360
    accepted = bool(valid) and fraction >= minimum_valid_fraction and gap <= maximum_invalid_gap_deg
    return {
        "valid": accepted,
        "valid_fraction": fraction,
        "maximum_invalid_gap_deg": gap,
        "mean_mm": float(np.mean(valid)) if accepted else None,
        "min_mm": min(valid) if accepted and include_extrema else None,
        "max_mm": max(valid) if accepted and include_extrema else None,
        "reason_codes": (
            []
            if accepted
            else (["insufficient_valid_rays"] if fraction < minimum_valid_fraction else [])
            + (["excessive_invalid_angular_gap"] if gap > maximum_invalid_gap_deg else [])
        ),
    }
