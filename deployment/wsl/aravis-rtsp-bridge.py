#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} muss eine ganze Zahl sein, ist aber {raw!r}.") from exc
    return max(value, minimum)


def _gst_value(value: str) -> str:
    return shlex.quote(value)


def _source_pipeline() -> str:
    parts = ["aravissrc"]
    camera_name = _env("ARAVIS_CAMERA_NAME")
    if camera_name:
        parts.append(f"camera-name={_gst_value(camera_name)}")

    features = _env("ARAVIS_FEATURES")
    if features:
        parts.append(f"features={_gst_value(features)}")

    extra_properties = _env("ARAVIS_SRC_PROPERTIES")
    if extra_properties:
        parts.append(extra_properties)

    return " ".join(parts)


def _conversion_pipeline() -> str:
    custom = _env("ARAVIS_CONVERSION_PIPELINE")
    if custom:
        return custom

    mode = _env("ARAVIS_CONVERSION_MODE", "bayer").lower()
    if mode == "bayer":
        return "bayer2rgb ! videoconvert"
    if mode in {"mono", "raw", "rgb"}:
        return "videoconvert"
    if mode == "none":
        return "identity"
    raise SystemExit(
        "ARAVIS_CONVERSION_MODE muss bayer, mono, raw, rgb oder none sein "
        f"und ist aktuell {mode!r}."
    )


def _bridge_pipeline() -> str:
    queue_size = _env_int("ARAVIS_QUEUE_SIZE", 2, minimum=1)
    bitrate = _env_int("ARAVIS_H264_BITRATE_KBIT", 6000, minimum=100)
    key_int = _env_int("ARAVIS_H264_KEY_INT_MAX", 30, minimum=1)
    source_caps = _env("ARAVIS_SOURCE_CAPS")

    parts = [
        _source_pipeline(),
        f"queue max-size-buffers={queue_size} leaky=downstream",
    ]
    if source_caps:
        parts.append(source_caps)
    parts.extend(
        [
            _conversion_pipeline(),
            "videoconvert",
            "video/x-raw,format=I420",
            f"queue max-size-buffers={queue_size} leaky=downstream",
            (
                "x264enc tune=zerolatency speed-preset=ultrafast "
                f"bitrate={bitrate} key-int-max={key_int} bframes=0"
            ),
            "rtph264pay name=pay0 pt=96 config-interval=1",
        ]
    )
    return " ! ".join(parts)


def main() -> int:
    Gst.init(None)

    address = _env("ARAVIS_RTSP_ADDRESS", "0.0.0.0")
    port = _env("ARAVIS_RTSP_PORT", "8554")
    mount = _env("ARAVIS_RTSP_MOUNT", "/flir-blackfly")
    if not mount.startswith("/"):
        mount = f"/{mount}"

    launch = f"( {_bridge_pipeline()} )"
    server = GstRtspServer.RTSPServer()
    server.set_address(address)
    server.set_service(port)

    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(launch)
    factory.set_shared(True)
    factory.set_eos_shutdown(True)
    server.get_mount_points().add_factory(mount, factory)

    loop = GLib.MainLoop()

    def stop(*_args: object) -> None:
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    source_label = _env("ARAVIS_CAMERA_NAME", "<first-discovered-camera>")
    print(
        {
            "status": "starting",
            "camera": source_label,
            "rtsp_url": f"rtsp://{address}:{port}{mount}",
            "pipeline": launch,
        },
        flush=True,
    )
    if server.attach(None) == 0:
        print("FEHLER: RTSP-Server konnte nicht an den GLib-Kontext gebunden werden.", file=sys.stderr)
        return 1

    loop.run()
    print({"status": "stopped", "camera": source_label}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
