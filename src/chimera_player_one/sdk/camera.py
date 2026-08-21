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

**Locking grain: a method that issues more than one SDK call holds the library
lock for its whole body; a single-call method lets ``_call`` take it per call.**
:meth:`wait_for_image` is the deliberate exception -- it takes and releases per
poll, so a 600 s sub does not own the bus and a temperature read gets a window
every 50 ms. The multi-call methods, and why each is a transaction: `geometry`
(a torn read yields a `Geometry` that never existed), `configure`, `open`,
`reopen`, `close`, `begin_exposure` (setting the exposure time and arming must
not have a foreign write between them) and `start_cooling` (the target/enable
order above is load-bearing).

Note what is deliberately *not* locked at the chimera layer: see the comments on
the unlocked overrides in ``instruments/playeronecamera.py``. Once every SDK call
is serialised here, marking a single-call getter ``@lock`` would only queue it
behind a whole multi-frame batch.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass

import numpy as np

from .bindings import CameraSdk
from .enums import POACameraState, POAConfig, POAErrors, POAImgFormat
from .errors import POAError
from .structs import POACameraProperties

__all__ = [
    "Camera",
    "Exposure",
    "Geometry",
    "ExposureAbortedError",
    "ExposureTimeoutError",
]

#: How often to ask whether the frame has arrived. Short enough that an abort
#: feels immediate, long enough not to spin a core during a 600 s sub.
_POLL_INTERVAL = 0.05

#: How long POAGetImageData may take. By the time it is called POAImageReady has
#: already reported the frame is there, and the header says the call then
#: "will return immediately" -- so this covers the handover, not the exposure and
#: not really the bus. A constant is defensible for that, and 2 s is INDIGO's:
#: its Player One driver passes a bare 2000 for this same 18 MB Ares frame. The
#: field pattern across drivers is to separate the *wait* (long, polled, cheap)
#: from the *transfer* (short constant), which the wait_for_image/read_frame
#: split already does. Nobody sizes it by pixel count.
_TRANSFER_TIMEOUT_MS = 2_000

#: How long to wait between closing a wedged camera and re-opening it. The libusb
#: aborts in the 2026-08-20 log arrived about one every 2 s, which is the SDK's
#: own bulk-read timeout showing through, so this gives the stack one of those to
#: finish tearing the pipe down. ASSUMED, not measured -- if a reconnect is ever
#: seen to fail and then succeed on a second try, this is the number to raise.
_REOPEN_SETTLE = 2.0


class ExposureAbortedError(Exception):
    """Raised when an exposure was stopped by its abort flag rather than finishing.

    ``stop_error`` carries the failure from POAStopExposure, if it failed. A stop
    that fails during a deliberate abort is the cheapest evidence there is that
    the camera is in trouble, and it used to be discarded.
    """

    def __init__(self, message: str, stop_error: POAError | None = None) -> None:
        super().__init__(message)
        self.stop_error = stop_error


class ExposureTimeoutError(POAError):
    """Our own deadline expired, not the SDK's.

    A distinct class because the two were otherwise indistinguishable: the
    synthesised error carried the same function name and the same code as a real
    POA_ERROR_TIMEOUT, so nothing downstream could tell "the camera never
    reported a frame" from "an SDK call timed out". It subclasses POAError and
    keeps code 9, so every existing ``except POAError`` still catches it and
    ``is_timeout`` still holds.

    The evidence rides as attributes rather than only in the message, so tests
    assert on facts and not on prose. ``camera_state`` is the one that matters:
    STATE_EXPOSING means the camera thinks it is still integrating and the frame
    never came off the sensor; STATE_OPENED means the exposure was not running at
    all, which is a different fault entirely; None means it would not even answer.
    """

    def __init__(
        self,
        detail: str,
        *,
        camera_state: POACameraState | None = None,
        dropped: int | None = None,
        stop_error: POAError | None = None,
        polls: int = 0,
        foreign_calls: str = "",
    ) -> None:
        super().__init__("POAImageReady", int(POAErrors.POA_ERROR_TIMEOUT), detail)
        self.camera_state = camera_state
        self.dropped = dropped
        self.stop_error = stop_error
        self.polls = polls
        self.foreign_calls = foreign_calls


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
        with sdk.transaction():
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

    def close(self) -> POAError | None:
        """Stop anything running and release the camera. Safe to call twice.

        The stop is unconditional because a camera left exposing is not released
        cleanly and the most likely reason we are here is that something already
        went wrong. Its failure is **returned rather than discarded**: a stop that
        fails with OPERATION_FAILED is the earliest cheap evidence that the camera
        has stopped listening, and dropping it is how the 2026-08-20 session got
        all the way to a 15 s timeout with nothing logged.
        """
        if self._closed:
            return None
        self._closed = True
        stop_error: POAError | None = None
        with self._sdk.transaction():
            try:
                self._sdk.stop_exposure(self._camera_id)
            except POAError as exc:
                stop_error = exc
            self._sdk.close_camera(self._camera_id)
        return stop_error

    def reopen(self) -> None:
        """Close this handle and open the same camera again, then re-init it.

        POAInitCamera resets the camera, so everything a caller set -- geometry,
        format, gain, offset, cooling -- is gone when this returns. Callers must
        re-apply; nothing here does it for them.

        Selection is by the serial this object already has, never by the model,
        index or serial the caller originally passed to `open`. Camera IDs come
        from a bus scan and are not stable across one: with two cameras on a
        host, an index re-selects whatever enumerates first *now*, and a camera
        that dropped off the bus leaves the other sitting at index 0. Frames
        arriving from the wrong camera under the right name is the one failure
        this must not have, and it would be silent.

        Why this exists at all: on 2026-08-20 a camera stopped delivering frames
        while still answering every config read on EP0. It was present, open and
        responsive -- only the bulk image endpoint was dead -- and nothing in the
        driver could do anything about it but wait for a person to unplug it.
        POAInitCamera re-runs the FPGA and sensor bring-up, which is the only
        lever the SDK offers.
        """
        serial = self._properties.SN.decode() if self._properties.SN else None
        model = self._properties.cameraModelName.decode()
        with self._sdk.transaction():
            # Both best-effort: a camera that will not stop or will not close is
            # exactly the one we are trying to re-open, and refusing to continue
            # would make the failure permanent for no gain.
            try:
                self._sdk.stop_exposure(self._camera_id)
            except POAError:
                pass
            try:
                self._sdk.close_camera(self._camera_id)
            except POAError:
                pass
        time.sleep(_REOPEN_SETTLE)
        with self._sdk.transaction():
            cameras = self._sdk.enumerate()
            if not cameras:
                raise POAError(
                    "POAGetCameraCount",
                    int(POAErrors.POA_ERROR_DEVICE_NOT_FOUND),
                    f"no Player One cameras found while re-opening {serial or model}",
                )
            # serial first, model only as the fall-through open() already allows
            # for a camera that reports none -- and with two identical models and
            # no serials this is ambiguous here exactly as it is there.
            chosen = self._select(
                cameras, serial=serial, model=None if serial else model, index=0
            )
            self._sdk.open_camera(chosen.cameraID)
            try:
                self._sdk.init_camera(chosen.cameraID)
            except POAError:
                self._sdk.close_camera(chosen.cameraID)
                raise
            self._properties = chosen
            self._camera_id = chosen.cameraID
            self._closed = False
            # The old buffer was sized for a geometry the camera no longer has.
            self._buffer = None
            self._buffer_bytes = 0

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

    def read_if_idle(self, name: str, timeout: float = 0.0) -> float | None:
        """Read a single-call property, but only if the library is not busy.

        Returns None rather than waiting when something else holds the lock --
        which in practice means a frame is being transferred, since that is the
        only call that holds it for more than microseconds. Callers that must
        never block (a status panel, a control loop) use this and fall back to
        the last value they saw; callers that want the truth use the property.

        This is what lets `get_temperature` stay unlocked at the chimera layer.
        Marking it @lock would queue it behind an entire multi-frame batch, and
        a temperature widget that freezes for minutes is a worse failure than
        one that shows a value a few seconds old.
        """
        if not self._sdk.lock.acquire(timeout=timeout):
            return None
        try:
            return float(getattr(self, name))
        finally:
            self._sdk.lock.release()

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
        # One transaction: the two writes are a pair, and a foreign write landing
        # between them is how you get a cooler running to a setpoint nobody asked
        # for. This is why the chimera override does not need @lock -- the
        # atomicity lives here, where it costs microseconds instead of queueing
        # behind a whole multi-frame batch.
        with self._sdk.transaction():
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
        """What the camera says its geometry is, right now.

        Four reads under one transaction: a write landing between them returns a
        Geometry describing no state the camera was ever in, and the buffer gets
        sized from it.
        """
        with self._sdk.transaction():
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
        with self._sdk.transaction():
            return self._configure(binning, window, image_format)

    def _configure(
        self,
        binning: int,
        window: tuple[int, int, int, int] | None,
        image_format: POAImgFormat,
    ) -> Geometry:
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
        with self._sdk.transaction():
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
        started = time.monotonic()
        polls = 0
        while True:
            if abort is not None and abort.is_set():
                raise ExposureAbortedError(
                    "exposure aborted before readout", self.abort_exposure()
                )
            polls += 1
            if self._sdk.image_ready(self._camera_id):
                return
            if time.monotonic() > deadline:
                # Ask the camera what it thinks BEFORE stopping it. The header
                # says POAGetDroppedImagesCount is "reset to 0 after stop
                # capture", and the stop also flips the state out of
                # STATE_EXPOSING -- so diagnosing afterwards destroys both
                # numbers, which is exactly what we wished we had on 2026-08-20.
                # Before the stop, which closes the accounting window.
                foreign = self._sdk.exposure_window_summary(self._camera_id)
                state, dropped, rendered = self.diagnose()
                stop_error = self.abort_exposure()
                elapsed = time.monotonic() - started
                detail = (
                    f"no frame after {exptime + margin:.1f} s for a "
                    f"{exptime:.3f} s exposure; {rendered}, {polls} polls in "
                    f"{elapsed:.1f} s, {foreign}"
                )
                if stop_error is not None:
                    detail = f"{detail}; the stop failed too: {stop_error}"
                raise ExposureTimeoutError(
                    detail,
                    camera_state=state,
                    dropped=dropped,
                    stop_error=stop_error,
                    polls=polls,
                    foreign_calls=foreign,
                )
            time.sleep(_POLL_INTERVAL)

    def diagnose(self) -> tuple[POACameraState | None, int | None, str]:
        """What the camera says about itself. Never raises.

        Both calls ride EP0, the control endpoint, while frames ride the bulk
        endpoint -- so if these answer, the camera is present and it is the image
        pipe that has stopped, and if they do not, it is gone. Nothing else in
        the log separates those two, and they are the difference between a
        reconnect that will work and one that cannot.

        A diagnostic that masks the original error is worse than none, so every
        probe fails to a None and says so in the rendered string.
        """
        state: POACameraState | None = None
        dropped: int | None = None
        try:
            state = self._sdk.get_camera_state(self._camera_id)
        except POAError:
            pass
        try:
            dropped = self._sdk.get_dropped_images_count(self._camera_id)
        except POAError:
            pass
        return (
            state,
            dropped,
            f"camera state={state.name if state else 'unreadable'}, "
            f"dropped={dropped if dropped is not None else 'unreadable'}",
        )

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

    def abort_exposure(self) -> POAError | None:
        """Stop an exposure. Never raises -- the caller is already unwinding.

        Returns the failure instead of dropping it. POAStopExposure is one
        control transfer on EP0 and does not touch the image endpoint, so it is
        the cheapest question there is to ask a camera you suspect has gone;
        OPERATION_FAILED here is the earliest evidence available and it used to
        go straight in the bin.
        """
        try:
            self._sdk.stop_exposure(self._camera_id)
        except POAError as exc:
            return exc
        return None

    def expose(self, exptime: float, abort: threading.Event | None = None) -> Exposure:
        """Take one frame, start to finish. For scripts and tests.

        The chimera driver does not use this: it needs the phases separately,
        because chimera splits exposure and readout into two calls.
        """
        geometry = self.geometry()
        # Sampled before arming, not between the wait and the readout: a
        # millisecond before the shutter is astronomically identical to a
        # millisecond after, and it keeps the read out of the window where the
        # SDK has bulk transfers pending.
        temperature = self.temperature
        started_at = self.begin_exposure(exptime)
        self.wait_for_image(exptime, abort)
        data = self.read_frame(geometry)
        return Exposure(
            data=data,
            started_at=started_at,
            exptime=exptime,
            temperature=temperature,
            geometry=geometry,
        )
