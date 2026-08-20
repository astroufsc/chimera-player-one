# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""A Player One camera as a chimera instrument.

The module name is not a style choice. ``classloader.py`` imports
``clsname.lower()``, so ``PlayerOneCamera`` must live in ``playeronecamera.py``
and an underscore anywhere in the filename breaks plugin loading -- silently,
because nothing tries to load it until a ``chimera.config`` names it.

What this layer adds over :mod:`chimera_player_one.sdk.camera` is chimera: the
exposure/readout split, the four events, abort plumbing, and the FITS metadata.
The SDK contracts -- exposure through ``POA_EXP``, geometry read back after it is
set, cooling ordered target-then-enable, never blocking in ``POAGetImageData`` --
all live one layer down and are not repeated here.

**Three things are wrong silently if they are wrong**, so they are stated once:

1. ``_expose`` and ``_readout`` are **not declared** in ``CameraBase``; it simply
   calls them. Omitting one is an ``AttributeError`` at exposure time, not at
   import, so it survives every test that does not actually expose.
2. ``CameraBase`` fires **none** of the four events. A driver that does not fire
   them looks like it works, and every caller that waits on ``expose_complete``
   hangs.
3. ``_save_image`` needs ``frame_start_time``, ``frame_temperature`` and
   ``binning_factor`` in ``extras`` or the FITS quietly loses ``DATE-OBS`` and
   ``CCD-TEMP`` and gets a WCS scaled as though unbinned. ``DATE-OBS`` is not
   cosmetic here: kepler's periodic-error fold anchors on mid-exposure, computed
   as ``DATE-OBS + EXPTIME/2``, and a missing or wrong one slides the phase of
   the very signal that measurement exists to find.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import override

from chimera.controllers.imageserver.imagerequest import ImageRequest
from chimera.instruments.camera import CameraBase
from chimera.interfaces.camera import CameraFeature, CameraStatus, ReadoutMode
from chimera.util.image import Image

from chimera_player_one.sdk.bindings import CameraSdk
from chimera_player_one.sdk.camera import Camera, ExposureAbortedError, Geometry
from chimera_player_one.sdk.enums import POAImgFormat
from chimera_player_one.sdk.errors import POAError
from chimera_player_one.sdk.loader import SdkNotAvailableError
from chimera_player_one.sdk.structs import POACameraProperties

__all__ = ["PlayerOneCamera"]

_FORMATS = {
    "RAW16": POAImgFormat.POA_RAW16,
    "RAW8": POAImgFormat.POA_RAW8,
}

#: Report a temperature change above this many degrees. Small enough to track a
#: cooldown, large enough that sensor noise does not spam the bus.
_TEMPERATURE_EVENT_THRESHOLD = 0.5


class PlayerOneCamera(CameraBase):
    """Player One Astronomy camera, over the vendor SDK bundled with this package.

    Choose the camera by ``serial`` (stable), ``model``, or ``camera_index``
    (USB enumeration order, which changes when a cable is re-seated).
    """

    __config__ = {
        #: Serial number, the only identifier that survives a re-plug. Wins over
        #: `model` and `camera_index` when set. Read it from `doctor`.
        "serial": None,
        #: Model name, e.g. "Ares-M PRO". Used when `serial` is unset.
        "model": None,
        #: USB enumeration index. The last resort, and the least stable.
        "camera_index": 0,
        #: "RAW16" or "RAW8". RAW16 always, unless you are chasing frame rate:
        #: every Player One sensor here is >8 bits, so RAW8 discards real data.
        "image_format": "RAW16",
        #: Gain in the camera's own units, or None to leave it alone. The vendor
        #: publishes per-sensor presets; `doctor` prints them.
        "gain": None,
        #: Black level. None leaves the camera's default, which is per-sensor.
        "offset": None,
        #: USB traffic cap, 35-100. Lower it if frames drop on a shared bus or a
        #: long cable; it costs readout speed, not image quality.
        "usb_bandwidth": None,
        #: Fan duty 0-100, cooled cameras only. None leaves it as found.
        "fan_power": None,
        #: Run against the in-process fake library instead of hardware. For
        #: development and CI; it exercises the real ctypes layer either way.
        "simulated": False,
        #: How long past the requested exposure to wait before declaring a frame
        #: lost. A camera that never reports ready would otherwise park a chimera
        #: thread forever.
        "exposure_timeout_margin": 10.0,
    }

    def __init__(self) -> None:
        CameraBase.__init__(self)
        self._camera: Camera | None = None
        self._binnings: dict[str, int] = {}
        self._readout_modes: dict[int, ReadoutMode] = {}
        self._supports: dict[CameraFeature, bool] = {}
        self._last_temperature: float | None = None
        self._frame_started_at: dt.datetime | None = None
        self._frame_temperature: float = 0.0
        self._frame_geometry: Geometry | None = None
        self._exposing_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    @override
    def __start__(self) -> bool:
        self._camera = self._open()
        props = self._camera.properties

        self["camera_model"] = props.model
        self["ccd_model"] = props.sensor
        self["ccd_width"] = props.maxWidth
        self["ccd_height"] = props.maxHeight
        self["pixel_size_x"] = props.pixelSize
        self["pixel_size_y"] = props.pixelSize
        if self["ccd_saturation_level"] is None:
            self["ccd_saturation_level"] = self._full_scale(props)

        self._build_readout_modes(props)
        self._supports = {
            CameraFeature.TEMPERATURE_CONTROL: props.has_cooler,
            CameraFeature.PROGRAMMABLE_GAIN: True,
            CameraFeature.PROGRAMMABLE_BIAS_LEVEL: True,
            CameraFeature.PROGRAMMABLE_FAN: props.has_cooler,
            CameraFeature.PROGRAMMABLE_OVERSCAN: False,
            CameraFeature.PROGRAMMABLE_LEDS: False,
            CameraFeature.PROGRAMMABLE_ADC: False,
        }

        self._apply_static_settings()

        setpoint = self["temperature_setpoint"]
        if setpoint is not None:
            if props.has_cooler:
                self._camera.start_cooling(float(setpoint))
            else:
                self.log.warning(
                    "temperature_setpoint is set but %s has no cooler; ignoring",
                    props.model,
                )

        self.log.info(
            "%s (%s, %dx%d, %d-bit, %.2f um) ready; cooler=%s",
            props.model,
            props.serial,
            props.maxWidth,
            props.maxHeight,
            props.bitDepth,
            props.pixelSize,
            props.has_cooler,
        )
        self.set_hz(0.5)
        return True

    @override
    def __stop__(self) -> bool:
        # CameraBase.__stop__ aborts a running exposure; overriding it means we
        # have to do that ourselves before letting go of the hardware.
        try:
            self.abort_exposure(readout=False)
        except Exception:
            self.log.debug("abort during shutdown failed", exc_info=True)
        if self._camera is not None:
            self._camera.close()
            self._camera = None
        return True

    def _open(self) -> Camera:
        if self["simulated"]:
            from chimera_player_one.sdk.simulator import FakeCameraLibrary

            self.log.warning("running against the SIMULATED camera library")
            return Camera.open(CameraSdk(FakeCameraLibrary()))
        try:
            return Camera.open(
                serial=self["serial"],
                model=self["model"],
                index=int(self["camera_index"]),
            )
        except POAError as exc:
            if exc.is_access_denied:
                # By far the most common first-run failure on Linux, and the raw
                # code says nothing about the cause.
                raise POAError(
                    exc.function,
                    exc.code,
                    "permission denied opening the camera. On Linux the udev "
                    "rules are needed for non-root access; install them with "
                    "`chimera-player-one-doctor --install-udev` and re-plug the "
                    "camera.",
                ) from exc
            raise
        except SdkNotAvailableError:
            self.log.error("the bundled Player One SDK could not be loaded")
            raise

    def _full_scale(self, props: POACameraProperties) -> int:
        """The largest value this camera can actually produce, in the current format.

        MEASURED on an Ares-M PRO, 2026-08-20, and it is **neither** of the two
        obvious answers. RAW16 carries the sensor value *left-shifted* into the
        top bits, so on a 14-bit sensor:

        * not 16383 -- the data is shifted, so it reaches far past that;
        * not 65535 -- the low two bits are always zero, so that is unreachable.

        Full scale is ``16383 << 2 == 65532``. Confirmed twice: a flooded sensor
        plateaus at exactly 65532 with 97.6 % of pixels there, and on a dark frame
        100 % of values are multiples of 4.

        This matters more than it looks. A flat-field or linearity routine told
        the saturation level is 65535 never sees a saturated pixel, and happily
        fits a curve through the clipped end of its own data.
        """
        image_format = _FORMATS.get(str(self["image_format"]).upper())
        if image_format is POAImgFormat.POA_RAW8:
            return 255
        shift = 16 - props.bitDepth
        return ((1 << props.bitDepth) - 1) << shift

    def _apply_static_settings(self) -> None:
        camera = self._camera
        assert camera is not None
        if self["gain"] is not None:
            camera.gain = int(self["gain"])
        if self["offset"] is not None:
            camera.offset = int(self["offset"])
        if self["usb_bandwidth"] is not None:
            camera.usb_bandwidth = int(self["usb_bandwidth"])
        if self["fan_power"] is not None and camera.has_cooler:
            camera.set_fan_power(int(self["fan_power"]))

    def _build_readout_modes(self, props: POACameraProperties) -> None:
        """One readout mode per hardware binning the camera reports.

        The binning strings are chimera's ("2x2"); the ids are the SDK's integer
        binning, so the two dictionaries stay trivially consistent. `get_binnings`
        keys must index `get_readout_modes` or `_get_readout_mode_info` falls back
        to full frame without complaining.
        """
        self._binnings = {f"{b}x{b}": b for b in props.binnings}
        self._readout_modes = {}
        for binning in props.binnings:
            mode = ReadoutMode()
            mode.mode = binning
            mode.gain = 1.0
            # The SDK rounds ROI width down to a multiple of 4, so a mode that
            # advertises 1002 px at bin 3 would be one the camera cannot give.
            width = props.maxWidth // binning
            mode.width = width - (width % 4)
            mode.height = props.maxHeight // binning
            mode.pixel_width = props.pixelSize * binning
            mode.pixel_height = props.pixelSize * binning
            self._readout_modes[binning] = mode

    # -- background loop --------------------------------------------------

    @override
    def control(self) -> bool:
        """Report temperature changes. Nothing in CameraBase fires this event."""
        if self._camera is None:
            return True
        try:
            temperature = self._camera.temperature
        except POAError:
            self.log.debug("temperature read failed", exc_info=True)
            return True
        previous = self._last_temperature
        if (
            previous is None
            or abs(temperature - previous) >= _TEMPERATURE_EVENT_THRESHOLD
        ):
            self._last_temperature = temperature
            if previous is not None:
                self.temperature_change(temperature, temperature - previous)
        return True

    # -- exposure ---------------------------------------------------------

    def _expose(self, image_request: ImageRequest) -> CameraStatus:
        """Arm the camera and wait out the exposure. Called by CameraBase."""
        camera = self._require_camera()
        self.expose_begin(image_request)
        status = CameraStatus.OK
        try:
            geometry = self._configure_for(image_request)
            exptime = float(image_request["exptime"])
            self._frame_geometry = geometry
            self._frame_started_at = camera.begin_exposure(exptime)
            # Sampled at shutter open rather than readout: on a cooled camera
            # mid-exposure drift is small, but a readout-time sample is wrong by
            # the whole exposure and biased one way.
            self._frame_temperature = camera.temperature
            camera.wait_for_image(
                exptime, self.abort, margin=float(self["exposure_timeout_margin"])
            )
        except ExposureAbortedError:
            status = CameraStatus.ABORTED
            self.log.info("exposure aborted")
        except POAError:
            status = CameraStatus.ERROR
            self.log.exception("exposure failed")
        finally:
            self.expose_complete(image_request, status)
        return status

    def _readout(self, image_request: ImageRequest) -> Image | None:
        """Fetch the frame and hand it to chimera. Called by CameraBase."""
        camera = self._require_camera()
        self.readout_begin(image_request)
        if self.abort.is_set():
            self.readout_complete(None, CameraStatus.ABORTED)
            return None
        try:
            (
                mode,
                binning,
                _top,
                _left,
                _width,
                _height,
            ) = self._get_readout_mode_info(
                image_request["binning"], image_request["window"]
            )
            pixels = camera.read_frame(self._frame_geometry)
        except POAError:
            self.log.exception("readout failed")
            self.readout_complete(None, CameraStatus.ERROR)
            return None

        image = self._save_image(
            image_request,
            pixels,
            {
                # Without these three the FITS silently loses DATE-OBS and
                # CCD-TEMP, and the WCS is scaled as if unbinned.
                "frame_start_time": self._frame_started_at,
                "frame_temperature": self._frame_temperature,
                "binning_factor": self._binnings.get(binning, 1),
            },
        )
        if self.abort.is_set():
            self.readout_complete(None, CameraStatus.ABORTED)
            return None
        self.readout_complete(image.url(), CameraStatus.OK)
        return image

    def _configure_for(self, image_request: ImageRequest) -> Geometry:
        """Translate a chimera ImageRequest into camera geometry.

        `_get_readout_mode_info` returns binned coordinates, which is also what
        the SDK's ROI wants, so there is no unit conversion here -- and adding
        one "to be safe" would double-apply the binning.
        """
        camera = self._require_camera()
        mode, binning, top, left, width, height = self._get_readout_mode_info(
            image_request["binning"], image_request["window"]
        )
        image_format = _FORMATS.get(str(self["image_format"]).upper())
        if image_format is None:
            raise POAError(
                "POASetImageFormat",
                4,
                f"unknown image_format {self['image_format']!r}; use RAW16 or RAW8",
            )
        window = None
        if (left, top) != (0, 0) or (width, height) != (mode.width, mode.height):
            window = (left, top, width, height)
        return camera.configure(
            binning=self._binnings.get(binning, 1),
            window=window,
            image_format=image_format,
        )

    def _require_camera(self) -> Camera:
        if self._camera is None:
            raise POAError("POAOpenCamera", 5, "camera is not open; was __start__ run?")
        return self._camera

    # -- cooling ----------------------------------------------------------

    @override
    def start_cooling(self, temp_c: float) -> bool:
        self._require_camera().start_cooling(float(temp_c))
        return True

    @override
    def stop_cooling(self) -> bool:
        self._require_camera().stop_cooling()
        return True

    @override
    def is_cooling(self) -> bool:
        return self._require_camera().cooling

    @override
    def get_temperature(self) -> float:
        return self._require_camera().temperature

    @override
    def get_set_point(self) -> float:
        return self._require_camera().target_temperature

    @override
    def start_fan(self, rate: int | None = None) -> bool:
        self._require_camera().set_fan_power(100 if rate is None else int(rate))
        return True

    @override
    def stop_fan(self) -> bool:
        self._require_camera().set_fan_power(0)
        return True

    @override
    def is_fanning(self) -> bool:
        # The SDK ties the fan to the cooler and exposes no independent readback,
        # so this reports the cooler rather than inventing a value.
        camera = self._require_camera()
        return camera.cooling if camera.has_cooler else False

    # -- capabilities -----------------------------------------------------

    @override
    def get_binnings(self) -> dict[str, int]:
        return dict(self._binnings)

    @override
    def get_adcs(self) -> dict[str, int]:
        # One ADC, not selectable on these cameras.
        return {f"{self._require_camera().properties.bitDepth} bits": 0}

    @override
    def get_physical_size(self) -> tuple[int, int]:
        props = self._require_camera().properties
        return (props.maxWidth, props.maxHeight)

    @override
    def get_pixel_size(self) -> tuple[float, float]:
        props = self._require_camera().properties
        return (props.pixelSize, props.pixelSize)

    @override
    def get_overscan_size(self) -> tuple[int, int]:
        return (0, 0)

    @override
    def get_readout_modes(self) -> dict[int, ReadoutMode]:
        return dict(self._readout_modes)

    @override
    def supports(self, feature: CameraFeature | None = None) -> bool:
        return self._supports.get(feature, False)

    # -- metadata ---------------------------------------------------------

    @override
    def get_metadata(self, request: ImageRequest) -> list[tuple]:
        """CameraBase's cards, plus the ones that make a frame reproducible.

        Gain, offset and e-/ADU are what turn ADU back into electrons, and the
        serial is what says *which* camera when two of the same model share a
        host. None of it is derivable from the pixels afterwards.
        """
        metadata = super().get_metadata(request)
        camera = self._camera
        if camera is None:
            return metadata
        try:
            props = camera.properties
            metadata += [
                ("CAMSN", props.serial, "camera serial number"),
                ("SENSOR", props.sensor, "sensor model"),
                ("GAIN", camera.gain, "camera gain setting"),
                ("OFFSET", camera.offset, "camera black level setting"),
                ("EGAIN", camera.electrons_per_adu(), "electrons per ADU at this gain"),
                ("BITDEPTH", props.bitDepth, "sensor ADC bit depth"),
            ]
            if props.has_cooler:
                metadata += [
                    ("SET-TEMP", camera.target_temperature, "cooler set point [C]"),
                    ("COOLPOWR", camera.cooler_power, "cooler power [percent]"),
                ]
        except POAError:
            # A frame with fewer cards beats a frame that failed to save. The
            # camera has already produced the pixels by this point.
            self.log.warning(
                "could not read camera metadata for the header", exc_info=True
            )
        return metadata
