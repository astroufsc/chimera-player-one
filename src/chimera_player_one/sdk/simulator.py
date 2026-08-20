# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Fake Player One libraries, standing in for the shared object itself.

**The seam is the ``ctypes.CDLL`` boundary, not a Python camera protocol.** These
classes are handed to :class:`~chimera_player_one.sdk.bindings.CameraSdk` exactly
where the real library would go, and they answer with the same out-parameters,
the same structs and the same ``POAErrors`` codes. So the struct packing, the
pointer marshalling, the union member selection, the error mapping and the 16-bit
buffer handling in ``bindings.py`` are all executed by the test suite. The only
thing absent is the vendor blob.

Faking one layer up -- a ``Camera`` object with an ``expose()`` method -- would be
less code and would test almost nothing, because it would skip precisely the
marshalling most likely to be wrong.

It also does what hardware will not do on request:

* fail a named function once, with a chosen code (``fail_once``);
* disappear mid-run, the way a USB cable does (``disconnect_after_frames``);
* run a cooler that never reaches setpoint (``cooler_reaches_setpoint=False``);
* return fewer bytes than the buffer expects (``short_read``).

Frame content is deliberately simple -- bias, read noise, a few Gaussian stars --
because its job is to exercise the pipeline, not to be astrophysics. It is a
seam: `mirage` can be plugged in behind :meth:`FakeCameraLibrary.render` later to
produce solvable star fields without any driver change.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .enums import (
    POACameraState,
    POAConfig,
    POAErrors,
    POAImgFormat,
    POAValueType,
    PWErrors,
    PWState,
)

__all__ = [
    "FakeCameraLibrary",
    "FakeFilterWheelLibrary",
    "FakeCameraSpec",
    "ARES_M_PRO",
    "SEDNA_M",
]


def _deref(arg: Any) -> Any:
    """Get the object behind a ctypes out-parameter, however it was passed."""
    if hasattr(arg, "contents"):
        return arg.contents
    if hasattr(arg, "_obj"):  # ctypes.byref
        return arg._obj
    return arg


def _plain(arg: Any) -> Any:
    return arg.value if hasattr(arg, "value") else arg


class _FakeFunc:
    """A callable that also accepts ``restype``/``argtypes``, as a CDLL export does."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


@dataclass
class FakeCameraSpec:
    """What a fake camera claims to be. Defaults are the Ares-M PRO's real values."""

    model: str = "Ares-M PRO"
    sensor: str = "IMX533"
    serial: str = "FAKE0000000000000000"
    width: int = 3008
    height: int = 3008
    bit_depth: int = 14
    pixel_size: float = 3.76
    has_cooler: bool = True
    has_st4: bool = False
    is_colour: bool = False
    is_usb3: bool = True
    supports_hardware_bin: bool = True
    pid: int = 0x5335
    bins: tuple[int, ...] = (1, 2, 3, 4)
    formats: tuple[POAImgFormat, ...] = (POAImgFormat.POA_RAW8, POAImgFormat.POA_RAW16)
    sensor_modes: tuple[tuple[str, str], ...] = (
        ("Normal", "normal mode"),
        ("HDR", "high dynamic range"),
    )


#: The two cameras actually on the bench, so tests use real geometry.
ARES_M_PRO = FakeCameraSpec()
SEDNA_M = FakeCameraSpec(
    model="Sedna-M",
    sensor="IMX178",
    serial="FAKE1111111111111111",
    width=3096,
    height=2078,
    pixel_size=2.40,
    has_cooler=False,
    has_st4=True,
    supports_hardware_bin=False,
    pid=0x1783,
    sensor_modes=(),
)

_DEFAULT_CONFIG: dict[POAConfig, Any] = {
    POAConfig.POA_EXPOSURE: 10_000,
    POAConfig.POA_EXP: 0.01,
    POAConfig.POA_GAIN: 0,
    POAConfig.POA_OFFSET: 35,
    POAConfig.POA_TEMPERATURE: 25.0,
    POAConfig.POA_TARGET_TEMP: 0,
    POAConfig.POA_COOLER: False,
    POAConfig.POA_COOLER_POWER: 0,
    POAConfig.POA_FAN_POWER: 50,
    POAConfig.POA_HEATER_POWER: 10,
    POAConfig.POA_USB_BANDWIDTH_LIMIT: 90,
    POAConfig.POA_FRAME_LIMIT: 0,
    POAConfig.POA_HARDWARE_BIN: False,
    POAConfig.POA_PIXEL_BIN_SUM: False,
    POAConfig.POA_EGAIN: 1.0,
    POAConfig.POA_HQI: False,
}

_VALUE_TYPES: dict[POAConfig, POAValueType] = {
    POAConfig.POA_TEMPERATURE: POAValueType.VAL_FLOAT,
    POAConfig.POA_EGAIN: POAValueType.VAL_FLOAT,
    POAConfig.POA_EXP: POAValueType.VAL_FLOAT,
    POAConfig.POA_COOLER: POAValueType.VAL_BOOL,
    POAConfig.POA_HARDWARE_BIN: POAValueType.VAL_BOOL,
    POAConfig.POA_PIXEL_BIN_SUM: POAValueType.VAL_BOOL,
    POAConfig.POA_MONO_BIN: POAValueType.VAL_BOOL,
    POAConfig.POA_HQI: POAValueType.VAL_BOOL,
    POAConfig.POA_FLIP_NONE: POAValueType.VAL_BOOL,
    POAConfig.POA_FLIP_HORI: POAValueType.VAL_BOOL,
    POAConfig.POA_FLIP_VERT: POAValueType.VAL_BOOL,
    POAConfig.POA_FLIP_BOTH: POAValueType.VAL_BOOL,
    POAConfig.POA_GUIDE_NORTH: POAValueType.VAL_BOOL,
    POAConfig.POA_GUIDE_SOUTH: POAValueType.VAL_BOOL,
    POAConfig.POA_GUIDE_EAST: POAValueType.VAL_BOOL,
    POAConfig.POA_GUIDE_WEST: POAValueType.VAL_BOOL,
}


@dataclass
class _CameraState:
    spec: FakeCameraSpec
    opened: bool = False
    initialised: bool = False
    exposing: bool = False
    single_frame: bool = True
    started_at: float = 0.0
    start_x: int = 0
    start_y: int = 0
    width: int = 0
    height: int = 0
    binning: int = 1
    image_format: POAImgFormat = POAImgFormat.POA_RAW8
    sensor_mode: int = 0
    frames: int = 0
    dropped: int = 0
    config: dict[POAConfig, Any] = field(default_factory=lambda: dict(_DEFAULT_CONFIG))


class FakeCameraLibrary:
    """Quacks like the loaded ``libPlayerOneCamera``.

    ``scanned`` reproduces the real SDK's least obvious behaviour: properties are
    unavailable until ``POAGetCameraCount`` has run, because that call is the bus
    scan. A driver that gets the order wrong fails here exactly as it would on
    hardware, which is the point of faking at this level.
    """

    def __init__(
        self, specs: list[FakeCameraSpec] | None = None, *, clock=time.monotonic
    ):
        self._clock = clock
        self._specs = list(specs) if specs is not None else [ARES_M_PRO]
        self._cameras = {
            index: _CameraState(
                spec=spec,
                width=spec.width,
                height=spec.height,
            )
            for index, spec in enumerate(self._specs)
        }
        self.scanned = False
        # -- fault injection --
        self.fail_once: dict[str, POAErrors] = {}
        self.fail_always: dict[str, POAErrors] = {}
        self.disconnect_after_frames: int | None = None
        self.cooler_reaches_setpoint = True
        self.short_read = False
        self.calls: list[str] = []

    # -- CDLL surface -----------------------------------------------------

    def __getattr__(self, name: str) -> _FakeFunc:
        if not name.startswith("POA"):
            raise AttributeError(name)
        impl = self.__class__.__dict__.get(f"_{name}")
        if impl is None:
            raise AttributeError(f"{name} is not exported by the fake camera library")
        func = _FakeFunc(lambda *args, _impl=impl: _impl(self, *args))
        setattr(self, name, func)
        return func

    def _guard(self, name: str) -> POAErrors | None:
        self.calls.append(name)
        if name in self.fail_always:
            return self.fail_always[name]
        if name in self.fail_once:
            return self.fail_once.pop(name)
        return None

    def _camera(self, camera_id: int) -> _CameraState | None:
        return self._cameras.get(int(camera_id))

    # -- frame synthesis (the seam mirage would replace) ------------------

    def render(self, state: _CameraState) -> np.ndarray:
        """A frame with a bias, read noise and a few stars. Deterministic."""
        rng = np.random.default_rng(seed=1234 + state.frames)
        h, w = state.height, state.width
        exptime = float(state.config[POAConfig.POA_EXP])
        peak = 255 if state.image_format is POAImgFormat.POA_RAW8 else 65535
        bias = state.config[POAConfig.POA_OFFSET]
        frame = rng.normal(bias, 3.0, size=(h, w))
        for _ in range(12):
            cy, cx = rng.integers(8, h - 8), rng.integers(8, w - 8)
            flux = rng.uniform(0.05, 0.6) * peak * min(exptime / 0.01, 50.0)
            yy, xx = np.mgrid[cy - 6 : cy + 7, cx - 6 : cx + 7]
            frame[cy - 6 : cy + 7, cx - 6 : cx + 7] += flux * np.exp(
                -(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 1.6**2))
            )
        return np.clip(frame, 0, peak)

    # -- discovery --------------------------------------------------------

    def _POAGetCameraCount(self):
        self.calls.append("POAGetCameraCount")
        self.scanned = True
        return len(self._cameras)

    def _POAGetCameraProperties(self, index, out):
        if (err := self._guard("POAGetCameraProperties")) is not None:
            return err
        # The real SDK will not answer before its bus scan. Reproduce that, or a
        # driver that calls in the wrong order passes here and fails on hardware.
        if not self.scanned or int(_plain(index)) not in self._cameras:
            return POAErrors.POA_ERROR_INVALID_INDEX
        self._fill_properties(
            self._cameras[int(_plain(index))], int(_plain(index)), out
        )
        return POAErrors.POA_OK

    def _POAGetCameraPropertiesByID(self, camera_id, out):
        if (err := self._guard("POAGetCameraPropertiesByID")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        self._fill_properties(state, int(_plain(camera_id)), out)
        return POAErrors.POA_OK

    def _fill_properties(self, state: _CameraState, camera_id: int, out) -> None:
        spec, props = state.spec, _deref(out)
        props.cameraModelName = spec.model.encode()
        props.userCustomID = b""
        props.cameraID = camera_id
        props.maxWidth, props.maxHeight = spec.width, spec.height
        props.bitDepth = spec.bit_depth
        props.isColorCamera = int(spec.is_colour)
        props.isHasST4Port = int(spec.has_st4)
        props.isHasCooler = int(spec.has_cooler)
        props.isUSB3Speed = int(spec.is_usb3)
        props.bayerPattern = -1 if not spec.is_colour else 0
        props.pixelSize = spec.pixel_size
        props.SN = spec.serial.encode()
        props.sensorModelName = spec.sensor.encode()
        props.localPath = b"fake"
        props.isSupportHardBin = int(spec.supports_hardware_bin)
        props.pID = spec.pid
        # Sentinel-terminated, zero-padded -- exactly as the SDK leaves them, so a
        # decoder that filters instead of truncating fails in tests, not at night.
        for i in range(8):
            props.bins[i] = spec.bins[i] if i < len(spec.bins) else 0
        for i in range(8):
            props.imgFormats[i] = (
                int(spec.formats[i])
                if i < len(spec.formats)
                else (int(POAImgFormat.POA_END) if i == len(spec.formats) else 0)
            )

    # -- lifecycle --------------------------------------------------------

    def _POAOpenCamera(self, camera_id):
        if (err := self._guard("POAOpenCamera")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        state.opened = True
        return POAErrors.POA_OK

    def _POAInitCamera(self, camera_id):
        if (err := self._guard("POAInitCamera")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None or not state.opened:
            return POAErrors.POA_ERROR_NOT_OPENED
        state.initialised = True
        return POAErrors.POA_OK

    def _POACloseCamera(self, camera_id):
        if (err := self._guard("POACloseCamera")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        state.opened = state.initialised = state.exposing = False
        return POAErrors.POA_OK

    # -- configuration ----------------------------------------------------

    def _POAGetConfigValueType(self, config, out):
        if (err := self._guard("POAGetConfigValueType")) is not None:
            return err
        try:
            cfg = POAConfig(int(_plain(config)))
        except ValueError:
            return POAErrors.POA_ERROR_INVALID_CONFIG
        _deref(out).value = int(_VALUE_TYPES.get(cfg, POAValueType.VAL_INT))
        return POAErrors.POA_OK

    def _POASetConfig(self, camera_id, config, value, is_auto):
        if (err := self._guard("POASetConfig")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None or not state.opened:
            return POAErrors.POA_ERROR_NOT_OPENED
        cfg = POAConfig(int(_plain(config)))
        value_type = _VALUE_TYPES.get(cfg, POAValueType.VAL_INT)
        if value_type is POAValueType.VAL_FLOAT:
            state.config[cfg] = float(value.floatValue)
        elif value_type is POAValueType.VAL_BOOL:
            state.config[cfg] = bool(value.boolValue)
        else:
            state.config[cfg] = int(value.intValue)
        if cfg is POAConfig.POA_EXP:
            state.config[POAConfig.POA_EXPOSURE] = int(state.config[cfg] * 1e6)
        return POAErrors.POA_OK

    def _POAGetConfig(self, camera_id, config, value_out, auto_out):
        if (err := self._guard("POAGetConfig")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None or not state.opened:
            return POAErrors.POA_ERROR_NOT_OPENED
        cfg = POAConfig(int(_plain(config)))
        if cfg is POAConfig.POA_TEMPERATURE:
            state.config[cfg] = self._sensor_temperature(state)
        if cfg is POAConfig.POA_COOLER_POWER:
            state.config[cfg] = 60 if state.config.get(POAConfig.POA_COOLER) else 0
        current = state.config.get(cfg, 0)
        value_type = _VALUE_TYPES.get(cfg, POAValueType.VAL_INT)
        packed = _deref(value_out)
        if value_type is POAValueType.VAL_FLOAT:
            packed.floatValue = float(current)
        elif value_type is POAValueType.VAL_BOOL:
            packed.boolValue = int(bool(current))
        else:
            packed.intValue = int(current)
        _deref(auto_out).value = 0
        return POAErrors.POA_OK

    def _sensor_temperature(self, state: _CameraState) -> float:
        if not state.spec.has_cooler or not state.config.get(POAConfig.POA_COOLER):
            return 25.0
        target = float(state.config[POAConfig.POA_TARGET_TEMP])
        # The cooler that never gets there: a real and common failure (undersized
        # supply, warm night), and one no hardware will perform on demand.
        return target if self.cooler_reaches_setpoint else target + 8.0

    def _POAGetConfigsCount(self, camera_id, out):
        if (err := self._guard("POAGetConfigsCount")) is not None:
            return err
        _deref(out).value = len(_DEFAULT_CONFIG)
        return POAErrors.POA_OK

    def _POAGetConfigAttributesByConfigID(self, camera_id, config, out):
        if (err := self._guard("POAGetConfigAttributesByConfigID")) is not None:
            return err
        cfg = POAConfig(int(_plain(config)))
        attrs = _deref(out)
        value_type = _VALUE_TYPES.get(cfg, POAValueType.VAL_INT)
        attrs.configID = int(cfg)
        attrs.valueType = int(value_type)
        attrs.isWritable = int(
            cfg
            not in {
                POAConfig.POA_TEMPERATURE,
                POAConfig.POA_EGAIN,
                POAConfig.POA_COOLER_POWER,
            }
        )
        attrs.isReadable = 1
        attrs.isSupportAuto = 0
        attrs.szConfName = cfg.name.encode()
        attrs.szDescription = b"fake"
        ranges = {
            POAConfig.POA_EXP: (1e-05, 7200.0),
            POAConfig.POA_GAIN: (0, 600),
            POAConfig.POA_OFFSET: (0, 1500),
            POAConfig.POA_TARGET_TEMP: (-50, 30),
            POAConfig.POA_TEMPERATURE: (-50.0, 100.0),
            POAConfig.POA_USB_BANDWIDTH_LIMIT: (35, 100),
        }
        lo, hi = ranges.get(cfg, (0, 100))
        if value_type is POAValueType.VAL_FLOAT:
            attrs.minValue.floatValue, attrs.maxValue.floatValue = float(lo), float(hi)
            attrs.defaultValue.floatValue = float(lo)
        else:
            attrs.minValue.intValue, attrs.maxValue.intValue = int(lo), int(hi)
            attrs.defaultValue.intValue = int(lo)
        return POAErrors.POA_OK

    def _POAGetConfigAttributes(self, camera_id, index, out):
        configs = list(_DEFAULT_CONFIG)
        i = int(_plain(index))
        if i >= len(configs):
            return POAErrors.POA_ERROR_INVALID_INDEX
        return self._POAGetConfigAttributesByConfigID(camera_id, int(configs[i]), out)

    # -- geometry ---------------------------------------------------------

    def _POAGetImageStartPos(self, camera_id, x_out, y_out):
        if (err := self._guard("POAGetImageStartPos")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(x_out).value, _deref(y_out).value = state.start_x, state.start_y
        return POAErrors.POA_OK

    def _POASetImageStartPos(self, camera_id, start_x, start_y):
        if (err := self._guard("POASetImageStartPos")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        state.start_x, state.start_y = int(_plain(start_x)), int(_plain(start_y))
        return POAErrors.POA_OK

    def _POAGetImageSize(self, camera_id, w_out, h_out):
        if (err := self._guard("POAGetImageSize")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(w_out).value, _deref(h_out).value = state.width, state.height
        return POAErrors.POA_OK

    def _POASetImageSize(self, camera_id, width, height):
        if (err := self._guard("POASetImageSize")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        if state.exposing:
            return POAErrors.POA_ERROR_EXPOSING
        w, h = int(_plain(width)), int(_plain(height))
        if w <= 0 or h <= 0 or w > state.spec.width // state.binning:
            return POAErrors.POA_ERROR_INVALID_ARGU
        # The SDK silently rounds width down to a multiple of 4 and height down
        # to a multiple of 2, and expects the caller to read both back. Measured;
        # the header mentions only the width rule. Reproduced here so a driver
        # that trusts what it asked for fails in the suite rather than at night.
        state.width, state.height = w - (w % 4), h - (h % 2)
        return POAErrors.POA_OK

    def _POAGetImageBin(self, camera_id, out):
        if (err := self._guard("POAGetImageBin")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(out).value = state.binning
        return POAErrors.POA_OK

    def _POASetImageBin(self, camera_id, binning):
        if (err := self._guard("POASetImageBin")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        if state.exposing:
            return POAErrors.POA_ERROR_EXPOSING
        b = int(_plain(binning))
        if b not in state.spec.bins:
            return POAErrors.POA_ERROR_INVALID_ARGU
        # MEASURED on an Ares-M PRO: binning *rescales the current ROI*, it does
        # not reset to full frame, and the start position scales with it. The
        # rounding loss therefore compounds across successive bin changes:
        # 3008 -> bin3 = 1000x1002 -> bin4 = 748x750, not 752x752.
        # An earlier version of this fake reset to full frame, which is what the
        # header reads like -- and which would have hidden a real driver bug.
        scale = state.binning / b
        width = int(state.width * scale)
        height = int(state.height * scale)
        state.width = width - (width % 4)
        state.height = height - (height % 2)
        state.start_x = int(state.start_x * scale)
        state.start_y = int(state.start_y * scale)
        state.binning = b
        return POAErrors.POA_OK

    def _POAGetImageFormat(self, camera_id, out):
        if (err := self._guard("POAGetImageFormat")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(out).value = int(state.image_format)
        return POAErrors.POA_OK

    def _POASetImageFormat(self, camera_id, image_format):
        if (err := self._guard("POASetImageFormat")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        if state.exposing:
            return POAErrors.POA_ERROR_EXPOSING
        fmt = POAImgFormat(int(_plain(image_format)))
        if fmt not in state.spec.formats:
            return POAErrors.POA_ERROR_INVALID_ARGU
        state.image_format = fmt
        return POAErrors.POA_OK

    # -- exposure ---------------------------------------------------------

    def _POAStartExposure(self, camera_id, single_frame):
        if (err := self._guard("POAStartExposure")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None or not state.initialised:
            return POAErrors.POA_ERROR_NOT_OPENED
        if (
            self.disconnect_after_frames is not None
            and state.frames >= self.disconnect_after_frames
        ):
            return POAErrors.POA_ERROR_DEVICE_NOT_FOUND
        state.exposing = True
        state.single_frame = bool(_plain(single_frame))
        state.started_at = self._clock()
        return POAErrors.POA_OK

    def _POAStopExposure(self, camera_id):
        if (err := self._guard("POAStopExposure")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        state.exposing = False
        state.dropped = 0
        return POAErrors.POA_OK

    def _POAGetCameraState(self, camera_id, out):
        if (err := self._guard("POAGetCameraState")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        if state.exposing:
            value = POACameraState.STATE_EXPOSING
        elif state.opened:
            value = POACameraState.STATE_OPENED
        else:
            value = POACameraState.STATE_CLOSED
        _deref(out).value = int(value)
        return POAErrors.POA_OK

    def _POAImageReady(self, camera_id, out):
        if (err := self._guard("POAImageReady")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        elapsed = self._clock() - state.started_at
        ready = state.exposing and elapsed >= float(state.config[POAConfig.POA_EXP])
        _deref(out).value = int(bool(ready))
        return POAErrors.POA_OK

    def _POAGetImageData(self, camera_id, buf, size, timeout_ms):
        if (err := self._guard("POAGetImageData")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None or not state.exposing:
            return POAErrors.POA_ERROR_NOT_OPENED
        frame = self.render(state)
        dtype = np.uint8 if state.image_format is POAImgFormat.POA_RAW8 else "<u2"
        payload = np.ascontiguousarray(frame.astype(dtype)).tobytes()
        requested = int(_plain(size))
        if requested < len(payload):
            return POAErrors.POA_ERROR_SIZE_LESS
        n = len(payload) // 2 if self.short_read else len(payload)
        ctypes.memmove(buf, payload, n)
        state.frames += 1
        if state.single_frame:
            state.exposing = False
        else:
            state.started_at = self._clock()
        return POAErrors.POA_OK

    def _POAGetDroppedImagesCount(self, camera_id, out):
        if (err := self._guard("POAGetDroppedImagesCount")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(out).value = state.dropped
        return POAErrors.POA_OK

    # -- sensor modes, presets, versions ----------------------------------

    def _POAGetSensorModeCount(self, camera_id, out):
        if (err := self._guard("POAGetSensorModeCount")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(out).value = len(state.spec.sensor_modes)
        return POAErrors.POA_OK

    def _POAGetSensorModeInfo(self, camera_id, index, out):
        if (err := self._guard("POAGetSensorModeInfo")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        modes = state.spec.sensor_modes
        i = int(_plain(index))
        if i >= len(modes):
            return POAErrors.POA_ERROR_INVALID_INDEX
        info = _deref(out)
        info.name, info.desc = modes[i][0].encode(), modes[i][1].encode()
        return POAErrors.POA_OK

    def _POAGetSensorMode(self, camera_id, out):
        if (err := self._guard("POAGetSensorMode")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        _deref(out).value = state.sensor_mode
        return POAErrors.POA_OK

    def _POASetSensorMode(self, camera_id, index):
        if (err := self._guard("POASetSensorMode")) is not None:
            return err
        state = self._camera(camera_id)
        if state is None:
            return POAErrors.POA_ERROR_INVALID_ID
        i = int(_plain(index))
        if i >= len(state.spec.sensor_modes):
            return POAErrors.POA_ERROR_INVALID_INDEX
        state.sensor_mode = i
        return POAErrors.POA_OK

    def _POAGetGainsAndOffsets(self, camera_id, *outs):
        if (err := self._guard("POAGetGainsAndOffsets")) is not None:
            return err
        for out, value in zip(outs, (0, 125, 130, 600, 35, 50, 50, 1000), strict=True):
            _deref(out).value = value
        return POAErrors.POA_OK

    def _POAGetErrorString(self, code):
        try:
            return POAErrors(int(_plain(code))).name.encode()
        except ValueError:
            return b"unknown"

    def _POAGetSDKVersion(self):
        return b"3.10.1"

    def _POAGetAPIVersion(self):
        return 20260430


@dataclass
class _WheelState:
    positions: int = 7
    name: str = "POA Phoenix Wheel"
    serial: str = "FAKEWHEEL00000000"
    opened: bool = False
    position: int = 0
    target: int = 0
    moving_until: float = 0.0
    one_way: bool = False
    aliases: tuple[str, ...] = ("U", "B", "V", "R", "I", "Ha", "OIII")
    offsets: tuple[int, ...] = (0, 0, 0, 10, 25, -40, -40)


class FakeFilterWheelLibrary:
    """Quacks like the loaded ``libPlayerOnePW``, including the move delay."""

    def __init__(
        self,
        wheels: list[_WheelState] | None = None,
        *,
        clock=time.monotonic,
        move_time=0.05,
    ):
        self._clock = clock
        self._move_time = move_time
        self._wheels = {0: _WheelState()} if wheels is None else dict(enumerate(wheels))
        self.fail_once: dict[str, PWErrors] = {}
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> _FakeFunc:
        if not name.startswith("POA"):
            raise AttributeError(name)
        impl = self.__class__.__dict__.get(f"_{name}")
        if impl is None:
            raise AttributeError(
                f"{name} is not exported by the fake filter wheel library"
            )
        func = _FakeFunc(lambda *args, _impl=impl: _impl(self, *args))
        setattr(self, name, func)
        return func

    def _guard(self, name: str) -> PWErrors | None:
        self.calls.append(name)
        return self.fail_once.pop(name, None)

    def _wheel(self, handle: int) -> _WheelState | None:
        return self._wheels.get(int(_plain(handle)))

    def _settle(self, wheel: _WheelState) -> None:
        if wheel.moving_until and self._clock() >= wheel.moving_until:
            wheel.position, wheel.moving_until = wheel.target, 0.0

    def _POAGetPWCount(self):
        self.calls.append("POAGetPWCount")
        return len(self._wheels)

    def _POAGetPWProperties(self, index, out):
        if (err := self._guard("POAGetPWProperties")) is not None:
            return err
        wheel = self._wheels.get(int(_plain(index)))
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_INDEX
        props = _deref(out)
        props.Name = wheel.name.encode()
        props.Handle = int(_plain(index))
        props.PositionCount = wheel.positions
        props.SN = wheel.serial.encode()
        return PWErrors.PW_OK

    def _POAGetPWPropertiesByHandle(self, handle, out):
        return self._POAGetPWProperties(handle, out)

    def _POAOpenPW(self, handle):
        if (err := self._guard("POAOpenPW")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        wheel.opened = True
        return PWErrors.PW_OK

    def _POAClosePW(self, handle):
        if (err := self._guard("POAClosePW")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        wheel.opened = False
        return PWErrors.PW_OK

    def _POAGetCurrentPosition(self, handle, out):
        if (err := self._guard("POAGetCurrentPosition")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None or not wheel.opened:
            return PWErrors.PW_ERROR_NOT_OPENED
        self._settle(wheel)
        # MEASURED on a POA Phoenix Wheel: a move in progress is reported as an
        # ERROR RETURN, not as a -1 position. This fake returned -1 until the
        # bring-up ladder said otherwise -- the same way it was wrong about
        # binning. See BUILD-LOG entries 9 and 13.
        if wheel.moving_until:
            return PWErrors.PW_ERROR_IS_MOVING
        _deref(out).value = wheel.position
        return PWErrors.PW_OK

    def _POAGotoPosition(self, handle, position):
        if (err := self._guard("POAGotoPosition")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None or not wheel.opened:
            return PWErrors.PW_ERROR_NOT_OPENED
        self._settle(wheel)
        if wheel.moving_until:
            return PWErrors.PW_ERROR_IS_MOVING
        target = int(_plain(position))
        if not 0 <= target < wheel.positions:
            return PWErrors.PW_ERROR_INVALID_ARGU
        wheel.target = target
        wheel.moving_until = self._clock() + self._move_time
        return PWErrors.PW_OK

    def _POAGetPWState(self, handle, out):
        if (err := self._guard("POAGetPWState")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        self._settle(wheel)
        if wheel.moving_until:
            value = PWState.PW_STATE_MOVING
        elif wheel.opened:
            value = PWState.PW_STATE_OPENED
        else:
            value = PWState.PW_STATE_CLOSED
        _deref(out).value = int(value)
        return PWErrors.PW_OK

    def _POAGetOneWay(self, handle, out):
        if (err := self._guard("POAGetOneWay")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        _deref(out).value = int(wheel.one_way)
        return PWErrors.PW_OK

    def _POASetOneWay(self, handle, is_one_way):
        if (err := self._guard("POASetOneWay")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        wheel.one_way = bool(_plain(is_one_way))
        return PWErrors.PW_OK

    def _POAGetPWFilterAlias(self, handle, position, buf, buf_len):
        if (err := self._guard("POAGetPWFilterAlias")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None or not wheel.opened:
            return PWErrors.PW_ERROR_NOT_OPENED
        p = int(_plain(position))
        if not 0 <= p < wheel.positions:
            return PWErrors.PW_ERROR_INVALID_ARGU
        buf.value = wheel.aliases[p].encode()
        return PWErrors.PW_OK

    def _POAGetPWFocusOffset(self, handle, position, out):
        if (err := self._guard("POAGetPWFocusOffset")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None or not wheel.opened:
            return PWErrors.PW_ERROR_NOT_OPENED
        p = int(_plain(position))
        if not 0 <= p < wheel.positions:
            return PWErrors.PW_ERROR_INVALID_ARGU
        _deref(out).value = wheel.offsets[p]
        return PWErrors.PW_OK

    def _POAResetPW(self, handle):
        if (err := self._guard("POAResetPW")) is not None:
            return err
        wheel = self._wheel(handle)
        if wheel is None:
            return PWErrors.PW_ERROR_INVALID_HANDLE
        wheel.position, wheel.target, wheel.moving_until = 0, 0, 0.0
        return PWErrors.PW_OK

    def _POAGetPWErrorString(self, code):
        try:
            return PWErrors(int(_plain(code))).name.encode()
        except ValueError:
            return b"unknown"

    def _POAGetPWSDKVer(self):
        return b"1.2.3.0"
