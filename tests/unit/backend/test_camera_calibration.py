from __future__ import annotations

import base64

import cv2
import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeCameraCalibrationObservation,
    RuntimeLiveFrame,
)
from cv_runtime.camera_calibration import (
    calculate_camera_calibration,
    camera_calibration_profile_hash,
    create_identity_camera_calibration,
    validate_camera_intrinsic_calibration,
)


def _checkerboard() -> np.ndarray:
    square = 54
    columns = 10
    rows = 7
    board = np.zeros((rows * square, columns * square, 3), dtype=np.uint8)
    for row in range(rows):
        for column in range(columns):
            color = 255 if (row + column) % 2 == 0 else 0
            cv2.rectangle(
                board,
                (column * square, row * square),
                ((column + 1) * square, (row + 1) * square),
                (color, color, color),
                thickness=-1,
            )
    return board


def _checkerboard_observations() -> list[RuntimeCameraCalibrationObservation]:
    board = _checkerboard()
    height, width = 600, 800
    source = np.asarray(
        [
            [0, 0],
            [board.shape[1] - 1, 0],
            [board.shape[1] - 1, board.shape[0] - 1],
            [0, board.shape[0] - 1],
        ],
        dtype=np.float32,
    )
    quads = [
        [[115, 110], [675, 80], [690, 500], [100, 520]],
        [[150, 70], [690, 145], [635, 520], [105, 450]],
        [[80, 145], [640, 85], [720, 445], [155, 535]],
        [[175, 120], [645, 105], [700, 480], [120, 505]],
        [[105, 85], [705, 120], [650, 525], [145, 480]],
        [[135, 155], [650, 70], [705, 490], [85, 535]],
    ]
    observations: list[RuntimeCameraCalibrationObservation] = []
    for index, quad in enumerate(quads, start=1):
        transform = cv2.getPerspectiveTransform(source, np.asarray(quad, dtype=np.float32))
        canvas = np.full((height, width, 3), 127, dtype=np.uint8)
        warped = cv2.warpPerspective(
            board,
            transform,
            (width, height),
            dst=canvas,
            borderMode=cv2.BORDER_TRANSPARENT,
        )
        success, encoded = cv2.imencode(".png", warped)
        assert success
        observations.append(
            RuntimeCameraCalibrationObservation(
                observation_id=f"observation-{index}",
                frame=RuntimeLiveFrame(
                    image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
                    mime_type="image/png",
                    width=width,
                    height=height,
                    sequence=index,
                    metadata={},
                ),
            )
        )
    return observations


def _blank_observation(index: int) -> RuntimeCameraCalibrationObservation:
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    success, encoded = cv2.imencode(".png", image)
    assert success
    return RuntimeCameraCalibrationObservation(
        observation_id=f"blank-{index}",
        frame=RuntimeLiveFrame(
            image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
            mime_type="image/png",
            width=320,
            height=240,
            sequence=index,
            metadata={},
        ),
    )


def _charuco_observations() -> list[RuntimeCameraCalibrationObservation]:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    board = cv2.aruco.CharucoBoard((7, 5), 1.0, 0.7, dictionary)
    board_image = cv2.cvtColor(board.generateImage((700, 500)), cv2.COLOR_GRAY2BGR)
    height, width = 620, 860
    source = np.asarray(
        [[0, 0], [699, 0], [699, 499], [0, 499]],
        dtype=np.float32,
    )
    quads = [
        [[90, 80], [760, 105], [735, 555], [115, 530]],
        [[135, 65], [780, 155], [700, 560], [80, 470]],
        [[70, 155], [730, 75], [790, 490], [125, 575]],
        [[145, 125], [745, 90], [775, 540], [100, 555]],
        [[105, 75], [775, 130], [720, 575], [130, 500]],
    ]
    observations: list[RuntimeCameraCalibrationObservation] = []
    for index, quad in enumerate(quads, start=1):
        transform = cv2.getPerspectiveTransform(source, np.asarray(quad, dtype=np.float32))
        canvas = np.full((height, width, 3), 255, dtype=np.uint8)
        warped = cv2.warpPerspective(
            board_image,
            transform,
            (width, height),
            dst=canvas,
            borderMode=cv2.BORDER_TRANSPARENT,
        )
        success, encoded = cv2.imencode(".png", warped)
        assert success
        observations.append(
            RuntimeCameraCalibrationObservation(
                observation_id=f"charuco-{index}",
                frame=RuntimeLiveFrame(
                    image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
                    mime_type="image/png",
                    width=width,
                    height=height,
                    sequence=index,
                    metadata={},
                ),
            )
        )
    return observations


def test_checkerboard_calibration_calculates_profile_errors_hash_and_preview() -> None:
    result = calculate_camera_calibration(
        source_channel_id="chn_calibration",
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="checkerboard",
            columns=9,
            rows=6,
            square_length=1,
        ),
        observations=_checkerboard_observations(),
        minimum_observation_count=5,
        maximum_rms_reprojection_error_px=5,
        generated_at="2026-08-03T10:00:00Z",
    )

    assert result.status == "valid"
    assert result.profile is not None
    assert result.profile.source_channel_id == "chn_calibration"
    assert result.profile.image_width == 800
    assert result.profile.image_height == 600
    assert result.profile.accepted_observation_count == 6
    assert len(result.profile.camera_matrix) == 9
    assert len(result.profile.distortion_coefficients) >= 4
    assert result.profile.perspective_rectified is False
    assert result.preview_before is not None
    assert result.preview_after is not None
    assert result.preview_after.metadata["perspective_rectified"] is False
    assert all(item.accepted for item in result.observations)
    assert all(item.reprojection_error_px is not None for item in result.observations)
    assert validate_camera_intrinsic_calibration(result.profile) == result.profile


def test_calibration_rejects_undetected_boards_and_too_few_observations() -> None:
    result = calculate_camera_calibration(
        source_channel_id="chn_calibration",
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="checkerboard",
            columns=9,
            rows=6,
        ),
        observations=[_blank_observation(index) for index in range(1, 4)],
        minimum_observation_count=3,
        maximum_rms_reprojection_error_px=2,
    )

    assert result.status == "invalid"
    assert result.profile is None
    assert result.failure_code == "calibration_observations_insufficient"
    assert all(item.reason_code == "calibration_board_not_detected" for item in result.observations)


def test_charuco_calibration_uses_the_same_typed_profile_contract() -> None:
    result = calculate_camera_calibration(
        source_channel_id="chn_charuco",
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="charuco",
            columns=7,
            rows=5,
            square_length=1,
            marker_length=0.7,
            dictionary_name="DICT_4X4_50",
        ),
        observations=_charuco_observations(),
        minimum_observation_count=4,
        maximum_rms_reprojection_error_px=5,
        generated_at="2026-08-03T10:00:00Z",
    )

    assert result.status == "valid"
    assert result.profile is not None
    assert result.profile.board_definition.board_type == "charuco"
    assert result.profile.accepted_observation_count == 5


def test_profile_hash_rejects_modified_intrinsics() -> None:
    result = calculate_camera_calibration(
        source_channel_id="chn_calibration",
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="checkerboard",
            columns=9,
            rows=6,
        ),
        observations=_checkerboard_observations(),
        minimum_observation_count=5,
        maximum_rms_reprojection_error_px=5,
        generated_at="2026-08-03T10:00:00Z",
    )
    assert result.profile is not None
    tampered = result.profile.model_dump(mode="json")
    tampered["camera_matrix"][0] += 1

    with pytest.raises(ValueError, match="hash"):
        validate_camera_intrinsic_calibration(tampered)

    unsupported = result.profile.model_copy(
        update={"distortion_coefficients": [0.0] * 6}
    )
    unsupported = unsupported.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(unsupported)}
    )
    with pytest.raises(ValueError, match="distortion vector"):
        validate_camera_intrinsic_calibration(unsupported)


def test_identity_passthrough_is_explicit_versioned_and_valid() -> None:
    result = create_identity_camera_calibration(
        source_channel_id="chn_calibration",
        image_width=1920,
        image_height=1080,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        generated_at="2026-08-06T08:00:00Z",
    )

    assert result.status == "valid"
    assert result.rms_reprojection_error_px is None
    assert result.observations == []
    assert result.profile is not None
    assert result.profile.calibration_method == "IDENTITY_PASSTHROUGH"
    assert result.profile.accepted_observation_count == 0
    assert result.profile.distortion_coefficients == [0.0] * 5
    assert result.profile.valid_roi.width == 1920
    assert result.profile.valid_roi.height == 1080
    assert validate_camera_intrinsic_calibration(result.profile) == result.profile


def test_measured_profile_hash_remains_compatible_without_method_discriminator() -> None:
    result = calculate_camera_calibration(
        source_channel_id="chn_calibration",
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        observations=_checkerboard_observations(),
        minimum_observation_count=5,
        maximum_rms_reprojection_error_px=5,
        generated_at="2026-08-03T10:00:00Z",
    )
    assert result.profile is not None
    legacy = result.profile.model_dump(mode="json")
    legacy.pop("calibration_method")

    assert camera_calibration_profile_hash(legacy) == result.profile.calibration_hash
    assert validate_camera_intrinsic_calibration(legacy).calibration_method == (
        "CHECKERBOARD_OR_CHARUCO"
    )
