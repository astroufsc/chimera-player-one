# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""A Player One camera as an object: opens, remembers its geometry, takes frames.

This is the layer that knows the SDK's *contracts* -- the ones the header states
in prose and the ones only measurement finds. It knows nothing about chimera, so
it can be driven from a script, a test or the bring-up ladder.

Four contracts are enforced here rather than left to callers:

**Exposure time goes through ``POA_EXP``, in seconds.** ``POA_EXPOSURE`` is
integer microseconds and caps out below what a deep-sky sub needs; the vendor
added ``POA_EXP`` in SDK 3.8.0 for exactly this and its own changelog says to
prefer it. Range ``[1e-5, 7200]`` s.

**Geometry is always read back after it is set.** ``POASetImageBin`` rewrites the
ROI *and* the start position, and ``POASetImageSize`` rounds width down to a
multiple of four. A driver that believes what it asked for allocates a buffer of
the wrong shape, and numpy then raises somewhere unrelated.

**Cooling is ordered: target first, then enable.** ``POA_TARGET_TEMP`` on its own
does nothing -- the value is cached and only reaches the hardware when
``POA_COOLER`` is switched on.

**Nothing ever blocks in ``POAGetImageData``.** It is the only call with a
timeout, so blocking in it makes the abort latency of a 600 s sub equal to that
timeout. We poll ``POAImageReady`` on a short cycle, check the abort flag every
time round, and only fetch once the frame is already waiting.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass

import numpy as np

from .bindings import CameraSdk
from .enums import POAConfig, POAImgFormat
from .errors import POAError
from .structs import POACameraProperties

__all__ = ["Camera", "Exposure", "Geometry", "ExposureAbortedError"]

#: How often to ask whether the frame has arrived. Short enough that an abort
#: feels immediate, long enough not to spin a core during a 600 s sub.
_POLL_INTERVAL = 0.05

#: The header's advice for POAGetImageData is "exposure + 500 ms". By the time we
#: call it the frame is already ready, so this only covers the transfer.
_TRANSFER_TIMEOUT_MS = 5_000


class ExposureAbortedError(Exception):
    """Raised when an exposure was stopped by its abort flag rather than finishing."""


@dataclass(frozen=True)
class Geometry:
    """The camera's actual ROI, as read back from it -- never as requested."""

    binning: int
    start_x: int
    start_y: int
    width: int
    height: int
    image_format: POAImgFormat

    @property
    def buffer_bytes(self) -> int:
        return self.width * self.height * self.image_format.bytes_per_pixel


@dataclass(frozen=True)
class Exposure:
    """A frame and the metadata that has to travel with it.

    ``started_at`` is the moment the shutter opened, in UTC. It becomes
    ``DATE-OBS``, and downstream that is not cosmetic: kepler's periodic-error
    fold anchors on mid-exposure, computed as this plus half of ``exptime``.
    """

    data: np.ndarray
    started_at: dt.datetime
    exptime: float
    temperature: float
    geometry: Geometry


class Camera:
    """One opened Player One camera."""

    def __init__(self, sdk: CameraSdk, properties: POACameraProperties) -> None:
        self._sdk = sdk
        self._properties = properties
        self._camera_id = properties.cameraID
        self._buffer: np.ndarray | None = None
        self._buffer_bytes = 0
        self._closed = False

    # -- construction -----------------------------------------------------

    @classmethod
    def open(
        cls,
        sdk: CameraSdk | None = None,
        *,
        serial: str | None = None,
        model: str | None = None,
        index: int = 0,
    ) -> Camera:
        """Open a camera, chosen by serial, then model, then index.

        Serial first because it is the only stable identifier: two Ares-M PROs on
        one host enumerate in USB order, which changes when a cable is re-seated.
        """
        sdk = sdk or CameraSdk()
        cameras = sdk.enumerate()
        if not cameras:
            raise POAError("POAGetCameraCount", 6, "no Player One cameras found")
        chosen = cls._select(cameras, serial=serial, model=model, index=index)
        sdk.open_camera(chosen.cameraID)
        try:
            sdk.init_camera(chosen.cameraID)
        except POAError:
            sdk.close_camera(chosen.cameraID)
            raise
        return cls(sdk, chosen)

    @staticmethod
    def _select(
        cameras: list[POACameraProperties],
        *,
        serial: str | None,
        model: str | None,
        index: int,
    ) -> POACameraProperties:
        if serial:
            for camera in cameras:
                if camera.serial == serial:
                    return camera
            available = ", ".join(f"{c.model} ({c.serial})" for c in cameras)
            raise POAError(
                "POAOpenCamera",
                6,
                f"no camera with serial {serial!r}; have: {available}",
            )
        if model:
            for camera in cameras:
                if camera.model == model:
                    return camera
            available = ", ".join(c.model for c in cameras)
            raise POAError(
                "POAOpenCamera", 6, f"no camera model {model!r}; have: {available}"
            )
        if index >= len(cameras):
            raise POAError(
                "POAOpenCamera", 1, f"camera index {index} of {len(cameras)} attached"
            )
        return cameras[index]

    # -- identity ---------------------------------------------------------

    @property
    def properties(self) -> POACameraProperties:
        return self._properties

    @property
    def camera_id(self) -> int:
        return self._camera_id

    @property
    def has_cooler(self) -> bool:
        return self._properties.has_cooler

    def close(self) -> None:
        """Stop anything running and release the camera. Safe to call twice.

        The stop is unconditional and its result ignored, because a camera left
        exposing is not released cleanly and the most likely reason we are here
        is that something already went wrong.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._sdk.stop_exposure(self._camera_id)
        except POAError:
            pass
        self._sdk.close_camera(self._camera_id)

    def __enter__(self) -> Camera:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- settings ---------------------------------------------------------

    @property
    def exposure_seconds(self) -> float:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_EXP)
        return float(value)

    @exposure_seconds.setter
    def exposure_seconds(self, seconds: float) -> None:
        self._sdk.set_config(self._camera_id, POAConfig.POA_EXP, float(seconds))

    @property
    def gain(self) -> int:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_GAIN)
        return int(value)

    @gain.setter
    def gain(self, value: int) -> None:
        self._sdk.set_config(self._camera_id, POAConfig.POA_GAIN, int(value))

    @property
    def offset(self) -> int:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_OFFSET)
        return int(value)

    @offset.setter
    def offset(self, value: int) -> None:
        self._sdk.set_config(self._camera_id, POAConfig.POA_OFFSET, int(value))

    @property
    def temperature(self) -> float:
        """Sensor temperature in degrees C. Readable on every camera, cooled or not."""
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_TEMPERATURE)
        return float(value)

    @property
    def cooler_power(self) -> int:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_COOLER_POWER)
        return int(value)

    @property
    def target_temperature(self) -> float:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_TARGET_TEMP)
        return float(value)

    @property
    def cooling(self) -> bool:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_COOLER)
        return bool(value)

    def start_cooling(self, target_c: float) -> None:
        """Set the setpoint and switch the cooler on, **in that order**.

        Reversing them looks like it works and cools to whatever setpoint was
        left in the camera from last time: the target is cached until the cooler
        is enabled, and only then written to the hardware.
        """
        if not self.has_cooler:
            raise POAError("POASetConfig", 3, f"{self._properties.model} has no cooler")
        self._sdk.set_config(
            self._camera_id, POAConfig.POA_TARGET_TEMP, int(round(target_c))
        )
        self._sdk.set_config(self._camera_id, POAConfig.POA_COOLER, True)

    def stop_cooling(self) -> None:
        if not self.has_cooler:
            return
        self._sdk.set_config(self._camera_id, POAConfig.POA_COOLER, False)

    def set_fan_power(self, percent: int) -> None:
        self._sdk.set_config(self._camera_id, POAConfig.POA_FAN_POWER, int(percent))

    @property
    def usb_bandwidth(self) -> int:
        value, _ = self._sdk.get_config(
            self._camera_id, POAConfig.POA_USB_BANDWIDTH_LIMIT
        )
        return int(value)

    @usb_bandwidth.setter
    def usb_bandwidth(self, percent: int) -> None:
        self._sdk.set_config(
            self._camera_id, POAConfig.POA_USB_BANDWIDTH_LIMIT, int(percent)
        )

    def gain_presets(self) -> dict[str, int]:
        """The vendor's per-sensor gain/offset recommendations, from the camera."""
        return self._sdk.get_gains_and_offsets(self._camera_id)

    def electrons_per_adu(self) -> float:
        value, _ = self._sdk.get_config(self._camera_id, POAConfig.POA_EGAIN)
        return float(value)

    # -- geometry ---------------------------------------------------------

    def geometry(self) -> Geometry:
        """What the camera says its geometry is, right now."""
        binning = self._sdk.get_image_bin(self._camera_id)
        start_x, start_y = self._sdk.get_image_start_pos(self._camera_id)
        width, height = self._sdk.get_image_size(self._camera_id)
        image_format = self._sdk.get_image_format(self._camera_id)
        return Geometry(binning, start_x, start_y, width, height, image_format)

    def configure(
        self,
        *,
        binning: int = 1,
        window: tuple[int, int, int, int] | None = None,
        image_format: POAImgFormat = POAImgFormat.POA_RAW16,
    ) -> Geometry:
        """Set format, binning and window, then return what the camera actually did.

        Order matters and is not arbitrary: format and binning first, because
        binning resets the window; the window last, because it is the only part
        the caller asked for precisely.
        """
        self._sdk.set_image_format(self._camera_id, image_format)
        self._sdk.set_image_bin(self._camera_id, binning)
        if window is None:
            # MEASURED: POASetImageBin *rescales the current ROI*, it does not
            # reset to full frame. So consecutive configure() calls would inherit
            # the previous window -- and its rounding loss, which compounds:
            # 3008 -> bin3 gives 1000x1002 (width rounded down to a multiple of
            # 4), and bin4 from there gives 748x750 rather than 752x752. Asking
            # for full frame explicitly is the only way to actually get it.
            props = self._properties
            width = props.maxWidth // binning
            height = props.maxHeight // binning
            self._sdk.set_image_size(self._camera_id, width, height)
            self._sdk.set_image_start_pos(self._camera_id, 0, 0)
        else:
            start_x, start_y, width, height = window
            # Size before position: the SDK clamps the position against the
            # current size, so setting position first can silently clip it.
            self._sdk.set_image_size(self._camera_id, width, height)
            self._sdk.set_image_start_pos(self._camera_id, start_x, start_y)
        geometry = self.geometry()
        self._ensure_buffer(geometry)
        return geometry

    def _ensure_buffer(self, geometry: Geometry) -> np.ndarray:
        """One buffer, reused across frames, resized only when the shape changes.

        The vendor's viewer allocates an 18 MB array per frame and then copies it
        again in the conversion. At 3008x3008 RAW16 that is 36 MB of churn per
        exposure for nothing.
        """
        needed = geometry.buffer_bytes
        if self._buffer is None or self._buffer_bytes != needed:
            self._buffer = np.zeros(needed, dtype=np.uint8)
            self._buffer_bytes = needed
        return self._buffer

    # -- exposure ---------------------------------------------------------

    def begin_exposure(self, exptime: float) -> dt.datetime:
        """Arm a single frame and return the UTC instant the shutter opened.

        Snap mode re-arms by calling this again; the SDK needs no stop between
        consecutive single frames.
        """
        self.exposure_seconds = exptime
        started_at = dt.datetime.now(dt.UTC)
        self._sdk.start_exposure(self._camera_id, single_frame=True)
        return started_at

    def wait_for_image(
        self,
        exptime: float,
        abort: threading.Event | None = None,
        *,
        margin: float = 10.0,
    ) -> None:
        """Block until the frame is ready, the abort flag is set, or it overruns.

        Never blocks inside the SDK. ``POAGetImageData`` is the only call that
        takes a timeout, so waiting there would make abort latency equal to it --
        on a 600 s sub, that is the whole exposure.

        The overrun guard exists because a camera that never reports ready is a
        real failure (a dropped USB frame, a wedged sensor) and the alternative
        to a deadline is a chimera thread parked forever.
        """
        deadline = time.monotonic() + exptime + margin
        while True:
            if abort is not None and abort.is_set():
                self.abort_exposure()
                raise ExposureAbortedError("exposure aborted before readout")
            if self._sdk.image_ready(self._camera_id):
                return
            if time.monotonic() > deadline:
                self.abort_exposure()
                raise POAError(
                    "POAImageReady",
                    9,
                    f"no frame after {exptime + margin:.1f} s for a {exptime:.3f} s exposure",
                )
            time.sleep(_POLL_INTERVAL)

    def read_frame(self, geometry: Geometry | None = None) -> np.ndarray:
        """Fetch the waiting frame as a 2-D array, shaped ``(height, width)``.

        Zero-copy out of the SDK and one view -- no ``tobytes()`` round trip. The
        dtype is explicitly little-endian: the SDK documents RAW16 that way, and
        a native-order view would flip the image on a big-endian host.
        """
        geometry = geometry or self.geometry()
        buffer = self._ensure_buffer(geometry)
        self._sdk.get_image_data(self._camera_id, buffer, _TRANSFER_TIMEOUT_MS)
        if geometry.image_format is POAImgFormat.POA_RAW16:
            pixels = buffer.view("<u2")
        else:
            pixels = buffer
        return pixels.reshape(geometry.height, geometry.width)

    def abort_exposure(self) -> None:
        """Stop an exposure. Never raises -- the caller is already unwinding."""
        try:
            self._sdk.stop_exposure(self._camera_id)
        except POAError:
            pass

    def expose(self, exptime: float, abort: threading.Event | None = None) -> Exposure:
        """Take one frame, start to finish. For scripts and tests.

        The chimera driver does not use this: it needs the phases separately,
        because chimera splits exposure and readout into two calls.
        """
        geometry = self.geometry()
        started_at = self.begin_exposure(exptime)
        self.wait_for_image(exptime, abort)
        temperature = self.temperature
        data = self.read_frame(geometry)
        return Exposure(
            data=data,
            started_at=started_at,
            exptime=exptime,
            temperature=temperature,
            geometry=geometry,
        )
