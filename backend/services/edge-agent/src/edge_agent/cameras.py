from __future__ import annotations

import importlib.util
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CameraFrame:
    index: int
    width: int
    height: int
    captured_at: str
    image: Any | None = None


@dataclass(frozen=True)
class CameraAdapterInventory:
    adapter: str
    available: bool
    provider: str
    version: str | None = None
    device_count: int | None = None
    detail: str | None = None


class CameraSource(Protocol):
    def read(self, stop_event: threading.Event) -> CameraFrame | None: ...

    def close(self) -> None: ...


def _bounded_float(
    config: dict[str, Any], name: str, minimum: float, maximum: float
) -> float | None:
    value = config.get(name)
    if value is None:
        return None
    number = float(value)
    if not minimum <= number <= maximum:
        raise RuntimeError(f"Camera property {name} must be between {minimum} and {maximum}.")
    return number


def _bounded_int(config: dict[str, Any], name: str, minimum: int, maximum: int) -> int | None:
    value = config.get(name)
    if value is None:
        return None
    number = int(value)
    if not minimum <= number <= maximum:
        raise RuntimeError(f"Camera property {name} must be between {minimum} and {maximum}.")
    return number


class OpenCvCameraSource:
    """Released OpenCV adapter for RTSP, files, USB and explicit GStreamer pipelines."""

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("The selected source requires the platform OpenCV package.") from exc
        source_type = str(config.get("type", "rtsp"))
        usb_source = source_type in {"usb", "usb_opencv", "opencv"}
        if usb_source:
            raw_uri = config.get("device_path")
            if raw_uri is None:
                raw_uri = config.get("device_index")
            if raw_uri is None:
                raw_uri = config.get("uri", 0)
        else:
            raw_uri = config.get("uri")
        if raw_uri is None:
            raise RuntimeError(f"The {source_type} camera source requires uri.")
        if usb_source and (isinstance(raw_uri, int) or str(raw_uri).strip().isdigit()):
            uri: str | int = int(raw_uri)
        else:
            uri = str(raw_uri)
        backend_name = (
            "gstreamer" if source_type == "gstreamer" else str(config.get("backend") or "auto")
        )
        backend_attribute = {
            "auto": "CAP_ANY",
            "gstreamer": "CAP_GSTREAMER",
            "v4l2": "CAP_V4L2",
            "dshow": "CAP_DSHOW",
            "msmf": "CAP_MSMF",
        }.get(backend_name.strip().lower())
        if backend_attribute is None or not hasattr(cv2, backend_attribute):
            raise RuntimeError(f"Unsupported OpenCV camera backend: {backend_name}.")
        backend = int(getattr(cv2, backend_attribute))
        self._capture = cv2.VideoCapture(uri, backend)
        if not self._capture.isOpened():
            self._capture.release()
            raise RuntimeError(f"Unable to open edge video source {uri!r}.")
        width = _bounded_int(config, "width", 16, 16384)
        height = _bounded_int(config, "height", 16, 16384)
        fps = _bounded_float(config, "fps", 0.1, 240.0)
        if width is not None:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height is not None:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps is not None:
            self._capture.set(cv2.CAP_PROP_FPS, fps)
        buffer_size = _bounded_int(config, "buffer_size", 1, 32)
        if buffer_size is not None and hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)
        fourcc = str(config.get("fourcc") or "").strip().upper()
        if fourcc:
            if len(fourcc) != 4 or not fourcc.isalnum():
                raise RuntimeError("Camera property fourcc must contain four letters or digits.")
            if hasattr(cv2, "CAP_PROP_FOURCC") and hasattr(cv2, "VideoWriter_fourcc"):
                self._capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        self._index = 0

    def read(self, stop_event: threading.Event) -> CameraFrame | None:
        if stop_event.is_set():
            return None
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise RuntimeError("Edge video source stopped delivering frames.")
        self._index += 1
        height, width = frame.shape[:2]
        return CameraFrame(self._index, int(width), int(height), _utc_now(), frame)

    def close(self) -> None:
        self._capture.release()


class AravisGigeCameraSource(OpenCvCameraSource):
    """GigE Vision adapter using the released Aravis GStreamer source element."""

    def __init__(self, config: dict[str, Any]) -> None:
        camera = str(config.get("camera_name") or "").strip()
        if not camera or not all(char.isalnum() or char in "._:-" for char in camera):
            raise RuntimeError("aravis_gige requires a safe camera_name (serial, name or IP).")
        width = _bounded_int(config, "width", 16, 16384)
        height = _bounded_int(config, "height", 16, 16384)
        fps = _bounded_float(config, "fps", 0.1, 240.0)
        properties = [f"camera-name={camera}"]
        if width is not None:
            properties.append(f"width={width}")
        if height is not None:
            properties.append(f"height={height}")
        caps = "video/x-raw"
        if fps is not None:
            caps += f",framerate={max(int(round(fps)), 1)}/1"
        pipeline = (
            f"aravissrc {' '.join(properties)} ! {caps} ! videoconvert ! "
            "video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
        )
        super().__init__({"type": "gstreamer", "uri": pipeline})


class BaslerPylonCameraSource:
    """Basler adapter using the vendor pypylon SDK with bounded acquisition settings."""

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            from pypylon import pylon  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Basler pypylon is not installed.") from exc
        factory = pylon.TlFactory.GetInstance()
        devices = list(factory.EnumerateDevices())
        selectors = {
            "serial_number": ("GetSerialNumber",),
            "user_defined_name": ("GetUserDefinedName",),
            "ip_address": ("GetIpAddress",),
            "device_id": ("GetDeviceID", "GetFullName"),
            "id_": ("GetDeviceID", "GetFullName"),
            "model": ("GetModelName",),
            "camera_name": (
                "GetSerialNumber",
                "GetUserDefinedName",
                "GetFriendlyName",
                "GetFullName",
                "GetIpAddress",
            ),
        }
        for key, methods in selectors.items():
            selector = str(config.get(key) or "").strip()
            if selector:
                devices = [
                    item
                    for item in devices
                    if selector in {self._device_value(item, method) for method in methods}
                ]
        if len(devices) != 1:
            raise RuntimeError(
                f"Expected one matching Basler camera, found {len(devices)}; "
                "configure serial_number, user_defined_name or ip_address."
            )
        self._pylon = pylon
        self._camera = pylon.InstantCamera(factory.CreateDevice(devices[0]))
        try:
            self._camera.Open()
            self._configure(config)
            self._converter = pylon.ImageFormatConverter()
            self._converter.OutputPixelFormat = pylon.PixelType_BGR8packed
            self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
            strategy_name = str(config.get("grab_strategy") or "LatestImageOnly")
            if strategy_name not in {"LatestImageOnly", "LatestImages", "OneByOne"}:
                raise RuntimeError(f"Unsupported Basler grab_strategy: {strategy_name}.")
            self._camera.StartGrabbing(getattr(pylon, f"GrabStrategy_{strategy_name}"))
        except Exception:
            self.close()
            raise
        self._index = 0

    @staticmethod
    def _device_value(device: object, method: str) -> str:
        try:
            return str(getattr(device, method)())
        except Exception:  # SDK fields depend on the transport (USB/GigE).
            return ""

    def _node(self, name: str) -> Any | None:  # noqa: ANN401 - dynamic GenICam node
        try:
            return getattr(self._camera, name)
        except Exception:  # pypylon raises GenICam exceptions for absent optional nodes.
            return None

    def _set_value(self, name: str, value: object, *, optional: bool = False) -> None:
        if value is None:
            return
        node = self._node(name)
        if node is None:
            if optional:
                return
            raise RuntimeError(f"Basler camera property is unavailable: {name}.")
        node.SetValue(value)

    def _set_first(self, names: tuple[str, ...], value: object) -> None:
        if value is None:
            return
        for name in names:
            if self._node(name) is not None:
                self._set_value(name, value)
                return
        raise RuntimeError(f"Basler camera property is unavailable: {'/'.join(names)}.")

    def _configure(self, config: dict[str, Any]) -> None:
        transport = str(config.get("transport_mode") or "pylon_direct").lower()
        if transport not in {"auto", "direct", "pylon", "pylon_direct"}:
            raise RuntimeError("Basler Edge acquisition requires the direct pylon transport.")
        self._set_value("AcquisitionMode", config.get("acquisition_mode") or "Continuous")
        self._set_value("PixelFormat", config.get("pixel_format"))
        # Reset ROI offsets before resizing, then apply the requested offsets.
        if config.get("width") is not None or config.get("height") is not None:
            self._set_value("OffsetX", 0, optional=True)
            self._set_value("OffsetY", 0, optional=True)
        for name, key in (
            ("Width", "width"),
            ("Height", "height"),
            ("OffsetX", "offset_x"),
            ("OffsetY", "offset_y"),
            ("GevSCPSPacketSize", "packet_size"),
            ("GevSCPD", "inter_packet_delay"),
        ):
            self._set_value(name, int(config[key]) if config.get(key) is not None else None)
        fps = _bounded_float(config, "fps", 0.1, 240.0)
        if fps is not None:
            self._set_value(
                "AcquisitionFrameRateEnable",
                bool(config.get("acquisition_frame_rate_enable", True)),
                optional=True,
            )
            self._set_first(("AcquisitionFrameRate", "AcquisitionFrameRateAbs"), fps)
        # Legacy Edge manifests use exposure_us/gain and free_run/software/line1.
        exposure = config.get("exposure_time_us", config.get("exposure_us"))
        gain = config.get("gain_db", config.get("gain"))
        if exposure is not None:
            self._set_value("ExposureAuto", config.get("exposure_auto") or "Off", optional=True)
            self._set_first(("ExposureTime", "ExposureTimeAbs"), float(exposure))
        if gain is not None:
            self._set_value("GainAuto", config.get("gain_auto") or "Off", optional=True)
            self._set_value("Gain", float(gain))
        self._set_value(
            "MaxNumBuffer",
            int(config.get("buffer_count") or config.get("max_num_buffer") or 16),
            optional=True,
        )
        self._set_value("OutputQueueSize", config.get("output_queue_size"))
        if config.get("frame_retention_ms") is not None:
            node = self._camera.GetStreamGrabberNodeMap().GetNode("FrameRetention")
            node.SetValue(int(config["frame_retention_ms"]))
        mode = str(config.get("trigger_mode") or "Off").lower()
        if mode not in {"off", "false", "0", "free_run", "on", "true", "1", "software", "line1"}:
            raise RuntimeError(f"Unsupported Basler trigger_mode: {mode}.")
        active = mode not in {"off", "false", "0", "free_run"}
        trigger_source = str(
            config.get("trigger_source") or ("Line1" if mode == "line1" else "Software")
        )
        self._set_value(
            "TriggerSelector", config.get("trigger_selector") or "FrameStart", optional=not active
        )
        self._set_value("TriggerMode", "Off", optional=not active)
        if active:
            self._set_value("TriggerSource", trigger_source)
            self._set_value("TriggerActivation", config.get("trigger_activation"))
            self._set_value("TriggerMode", "On")
        features = config.get("features") or {}
        for name, value in features.items():
            self._set_value(str(name), value)
        # Feature overrides may change trigger settings; read their effective values.
        effective_mode = str(features.get("TriggerMode", "On" if active else "Off"))
        effective_source = str(features.get("TriggerSource", trigger_source))
        self._trigger = effective_mode.lower() == "on" and effective_source.lower() == "software"
        self._hardware_trigger = effective_mode.lower() == "on" and not self._trigger
        timeout = config.get("timeout_ms")
        if timeout is None:
            timeout = float(config.get("fetch_timeout_seconds") or 2.0) * 1000
        self._timeout_ms = _bounded_int({"timeout_ms": timeout}, "timeout_ms", 50, 30_000) or 2000

    def read(self, stop_event: threading.Event) -> CameraFrame | None:
        if stop_event.is_set():
            return None
        if self._trigger:
            if self._camera.CanWaitForFrameTriggerReady():
                if not self._camera.WaitForFrameTriggerReady(
                    self._timeout_ms, self._pylon.TimeoutHandling_ThrowException
                ):
                    raise RuntimeError("Basler software trigger did not become ready.")
            self._camera.ExecuteSoftwareTrigger()
        result = self._camera.RetrieveResult(self._timeout_ms, self._pylon.TimeoutHandling_Return)
        if result is None:
            if self._hardware_trigger or stop_event.is_set():
                return None  # Hardware triggers may be idle between workpieces.
            raise RuntimeError(
                "Basler camera stopped delivering frames before acquisition timeout."
            )
        try:
            if not result.GrabSucceeded():
                raise RuntimeError(f"Basler acquisition failed: {result.GetErrorDescription()}.")
            converted = self._converter.Convert(result)
            image = converted.GetArray().copy()  # Own the data before SDK buffers are released.
        finally:
            result.Release()
        self._index += 1
        height, width = image.shape[:2]
        return CameraFrame(self._index, int(width), int(height), _utc_now(), image)

    def close(self) -> None:
        try:
            if self._camera.IsGrabbing():
                self._camera.StopGrabbing()
        finally:
            if self._camera.IsOpen():
                self._camera.Close()


class FlirSpinnakerCameraSource:
    """FLIR/Teledyne adapter using PySpin with explicit device selection."""

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            import PySpin  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("FLIR Spinnaker/PySpin is not installed.") from exc
        self._spin = PySpin
        self._system = PySpin.System.GetInstance()
        cameras = self._system.GetCameras()
        selector = str(config.get("serial_number", ""))
        selected = None
        for camera in cameras:
            serial_node = PySpin.CStringPtr(
                camera.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber")
            )
            serial = serial_node.GetValue() if PySpin.IsReadable(serial_node) else ""
            if not selector or serial == selector:
                selected = camera
                break
        cameras.Clear()
        if selected is None:
            self._system.ReleaseInstance()
            raise RuntimeError(f"No matching FLIR camera found: {selector or '<first>'}.")
        self._camera = selected
        self._camera.Init()
        self._set_float("ExposureTime", _bounded_float(config, "exposure_us", 1.0, 10_000_000.0))
        self._set_float("Gain", _bounded_float(config, "gain", 0.0, 48.0))
        pixel_format = str(config.get("pixel_format") or "").strip()
        if pixel_format:
            if pixel_format not in {"Mono8", "BayerRG8", "BayerBG8", "RGB8", "BGR8"}:
                raise RuntimeError("Unsupported FLIR pixel_format.")
            self._set_enum("PixelFormat", pixel_format)
        trigger = str(config.get("trigger_mode", "free_run"))
        if trigger not in {"free_run", "software", "line0"}:
            raise RuntimeError("FLIR trigger_mode must be free_run, software or line0.")
        self._set_enum("TriggerMode", "Off")
        if trigger != "free_run":
            self._set_enum("TriggerSelector", "FrameStart")
            self._set_enum("TriggerSource", "Software" if trigger == "software" else "Line0")
            self._set_enum("TriggerMode", "On")
        self._trigger = trigger
        self._timeout_ms = _bounded_int(config, "timeout_ms", 50, 30_000) or 2_000
        self._camera.BeginAcquisition()
        self._index = 0

    def _set_float(self, name: str, value: float | None) -> None:
        if value is None:
            return
        node = self._spin.CFloatPtr(self._camera.GetNodeMap().GetNode(name))
        if not self._spin.IsWritable(node):
            raise RuntimeError(f"FLIR camera property is not writable: {name}.")
        node.SetValue(min(max(value, node.GetMin()), node.GetMax()))

    def _set_enum(self, name: str, value: str) -> None:
        node = self._spin.CEnumerationPtr(self._camera.GetNodeMap().GetNode(name))
        if not self._spin.IsWritable(node):
            raise RuntimeError(f"FLIR camera property is not writable: {name}.")
        entry = node.GetEntryByName(value)
        if not self._spin.IsReadable(entry):
            raise RuntimeError(f"FLIR camera property {name} does not support {value}.")
        node.SetIntValue(entry.GetValue())

    def read(self, stop_event: threading.Event) -> CameraFrame | None:
        if stop_event.is_set():
            return None
        if self._trigger == "software":
            trigger = self._spin.CCommandPtr(self._camera.GetNodeMap().GetNode("TriggerSoftware"))
            if not self._spin.IsWritable(trigger):
                raise RuntimeError("FLIR software trigger is not writable.")
            trigger.Execute()
        raw = self._camera.GetNextImage(self._timeout_ms)
        try:
            if raw.IsIncomplete():
                raise RuntimeError(f"FLIR acquisition incomplete: {raw.GetImageStatus()}.")
            converted = raw.Convert(self._spin.PixelFormat_BGR8, self._spin.HQ_LINEAR)
            image = converted.GetNDArray()
        finally:
            raw.Release()
        self._index += 1
        height, width = image.shape[:2]
        return CameraFrame(self._index, int(width), int(height), _utc_now(), image)

    def close(self) -> None:
        try:
            self._camera.EndAcquisition()
            self._camera.DeInit()
        finally:
            self._system.ReleaseInstance()


def create_camera_source(source: dict[str, Any]) -> CameraSource:
    source_type = str(source.get("type", "passive"))
    if source_type in {"rtsp", "file", "usb", "usb_opencv", "opencv", "gstreamer"}:
        return OpenCvCameraSource(source)
    if source_type == "aravis_gige":
        return AravisGigeCameraSource(source)
    if source_type == "basler_pylon":
        return BaslerPylonCameraSource(source)
    if source_type == "flir_spinnaker":
        return FlirSpinnakerCameraSource(source)
    raise RuntimeError(f"Unsupported edge camera adapter: {source_type}.")


def inventory_camera_adapters() -> list[CameraAdapterInventory]:
    cv2_spec = importlib.util.find_spec("cv2")
    pylon_spec = importlib.util.find_spec("pypylon")
    spin_spec = importlib.util.find_spec("PySpin")
    gst = shutil.which("gst-inspect-1.0")
    opencv_version: str | None = None
    opencv_gstreamer = False
    if cv2_spec is not None:
        try:
            import cv2  # type: ignore[import-not-found]

            opencv_version = str(getattr(cv2, "__version__", "")) or None
            build_info = str(cv2.getBuildInformation())
            opencv_gstreamer = any(
                "GStreamer" in line and "YES" in line for line in build_info.splitlines()
            )
        except Exception:  # noqa: BLE001 - inventory must not take down the agent
            pass
    aravis = False
    if gst is not None and opencv_gstreamer:
        try:
            aravis = (
                subprocess.run(
                    [gst, "aravissrc"],
                    check=False,
                    capture_output=True,
                    timeout=3,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    pylon_available = False
    pylon_detail: str | None = "Basler pypylon is not installed."
    pylon_version: str | None = None
    pylon_devices: int | None = None
    if pylon_spec is not None:
        try:
            import pypylon  # type: ignore[import-not-found]
            from pypylon import pylon  # type: ignore[import-not-found]

            pylon_version = str(getattr(pypylon, "__version__", "")) or None
            pylon_devices = len(pylon.TlFactory.GetInstance().EnumerateDevices())
            pylon_available = True
            pylon_detail = None
        except Exception as exc:  # noqa: BLE001 - inventory must survive broken native SDKs
            pylon_detail = f"Basler SDK probe failed: {exc}"

    spin_version: str | None = None
    spin_devices: int | None = None
    if spin_spec is not None:
        system = None
        try:
            import PySpin  # type: ignore[import-not-found]

            spin_version = str(getattr(PySpin, "__version__", "")) or None
            system = PySpin.System.GetInstance()
            cameras = system.GetCameras()
            spin_devices = int(cameras.GetSize())
            cameras.Clear()
        except Exception:  # noqa: BLE001 - vendor SDKs may fail without hardware
            pass
        finally:
            if system is not None:
                system.ReleaseInstance()

    opencv = cv2_spec is not None
    return [
        CameraAdapterInventory(
            "rtsp", opencv, "opencv", opencv_version, detail="RTSP through OpenCV/FFmpeg"
        ),
        CameraAdapterInventory(
            "usb_opencv",
            opencv,
            "opencv",
            opencv_version,
            detail="USB/UVC through OpenCV",
        ),
        CameraAdapterInventory("gstreamer", opencv_gstreamer, "opencv+gstreamer", opencv_version),
        CameraAdapterInventory("aravis_gige", aravis, "aravis+gstreamer"),
        CameraAdapterInventory(
            "basler_pylon",
            pylon_available,
            "pypylon",
            pylon_version,
            pylon_devices,
            pylon_detail,
        ),
        CameraAdapterInventory(
            "flir_spinnaker",
            spin_spec is not None,
            "PySpin",
            spin_version,
            spin_devices,
        ),
    ]
