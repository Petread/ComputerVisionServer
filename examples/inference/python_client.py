from __future__ import annotations

import os
from pathlib import Path

import requests

BASE_URL = os.getenv("CV_PLATFORM_BASE_URL", "https://127.0.0.1:8443/inference-rt/api/v1")
TOKEN = os.getenv("CV_PLATFORM_INFERENCE_TOKEN")


def classify_image(image_path: Path) -> dict:
    headers = {
        "X-Request-ID": "python-example-req-001",
        "X-Correlation-ID": "python-example-op-001",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    with image_path.open("rb") as handle:
        response = requests.post(
            f"{BASE_URL}/infer/classify",
            headers=headers,
            data={
                "model_id": "quality-classifier",
                "top_k": "2",
            },
            files={
                "image": (image_path.name, handle, "image/png"),
            },
            timeout=5,
        )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    payload = classify_image(Path("photo.png"))
    print(payload["model_id"], payload["model_version"], payload["predictions"])
