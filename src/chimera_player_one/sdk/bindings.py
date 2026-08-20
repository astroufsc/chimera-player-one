# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""One Python method per exported SDK function. No state, no policy.

This layer's whole job is marshalling: build the ctypes call, check the return
code, hand back a Python value. Anything that *remembers* something -- which
camera is open, what the ROI is, whether an exposure is running -- belongs in
:mod:`chimera_player_one.sdk.camera`, and anything that knows what chimera is
belongs in the instrument. Keeping this file dumb is what lets the tests swap the
shared library for a fake and still exercise every line of marshalling.

**Signatures are bound once, at construction.** The vendor's wrapper assigns
``restype`` and ``argtypes`` on the shared function object *inside each call*,
alternating an int and a double signature on ``POASetConfig`` depending on which
helper you used. Two threads in a chimera process -- and there are always at
least two, since ``control()`` runs on its own thread -- can interleave there and
marshal a float as an int. Bound once, that race cannot happen.

**Return codes are converted explicitly, not through ``restype``.** Setting
``restype`` to an ``IntEnum`` class is a neat trick the vendor uses, and ctypes
will call it as a converter -- but an SDK newer than this binding returning an
unmapped code then raises ``ValueError`` from inside ctypes, at a place that has
nothing to do with the problem. We take a plain ``c_int`` and let
:class:`~chimera_player_one.sdk.errors.POAError` keep the raw number.

**Out-parameters use ``ctypes.pointer``, not ``byref``.** ``byref`` is marginally
faster and completely opaque to a fake library, which would receive a ``CArgObject``
it cannot write through. ``pointer`` gives the fake a ``.contents`` to assign to,
which is what makes the seam in :mod:`chimera_player_one.sdk.simulator` possible
at all. These calls happen per frame at most, so the allocation is irrelevant.
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from .enums import (
    POABool,
    POACameraState,
    POAConfig,
    POAErrors,
    POAImgFormat,
    POAValueType,
    PWErrors,
    PWState,
)
from .errors import POAError, PWError
from .structs import (
    POACameraProperties,
    POAConfigAttributes,
    POAConfigValue,
    POASensorModeInfo,
    PWProperties,
)

__all__ = ["CameraSdk", "FilterWheelSdk"]

_CONFIGURED = "_chimera_player_one_bound"

_INT_P = ctypes.POINTER(ctypes.c_int)

#: (name, restype, argtypes) for every function we call in the camera SDK.
_CAMERA_SIGNATURES: list[tuple[str, Any, list[Any]]] = [
    ("POAGetCameraCount", ctypes.c_int, []),
    (
        "POAGetCameraProperties",
        ctypes.c_int,
        [ctypes.c_int, ctypes.POINTER(POACameraProperties)],
    ),
    (
        "POAGetCameraPropertiesByID",
        ctypes.c_int,
        [ctypes.c_int, ctypes.POINTER(POACameraProperties)],
    ),
    ("POAOpenCamera", ctypes.c_int, [ctypes.c_int]),
    ("POAInitCamera", ctypes.c_int, [ctypes.c_int]),
    ("POACloseCamera", ctypes.c_int, [ctypes.c_int]),
    ("POAGetConfigsCount", ctypes.c_int, [ctypes.c_int, _INT_P]),
    (
        "POAGetConfigAttributes",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, ctypes.POINTER(POAConfigAttributes)],
    ),
    (
        "POAGetConfigAttributesByConfigID",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, ctypes.POINTER(POAConfigAttributes)],
    ),
    (
        "POASetConfig",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, POAConfigValue, ctypes.c_int],
    ),
    (
        "POAGetConfig",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, ctypes.POINTER(POAConfigValue), _INT_P],
    ),
    ("POAGetConfigValueType", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAGetImageStartPos", ctypes.c_int, [ctypes.c_int, _INT_P, _INT_P]),
    ("POASetImageStartPos", ctypes.c_int, [ctypes.c_int, ctypes.c_int, ctypes.c_int]),
    ("POAGetImageSize", ctypes.c_int, [ctypes.c_int, _INT_P, _INT_P]),
    ("POASetImageSize", ctypes.c_int, [ctypes.c_int, ctypes.c_int, ctypes.c_int]),
    ("POAGetImageBin", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POASetImageBin", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    ("POAGetImageFormat", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POASetImageFormat", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    ("POAStartExposure", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    ("POAStopExposure", ctypes.c_int, [ctypes.c_int]),
    ("POAGetCameraState", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAImageReady", ctypes.c_int, [ctypes.c_int, _INT_P]),
    (
        "POAGetImageData",
        ctypes.c_int,
        [ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long, ctypes.c_int],
    ),
    ("POAGetDroppedImagesCount", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAGetSensorModeCount", ctypes.c_int, [ctypes.c_int, _INT_P]),
    (
        "POAGetSensorModeInfo",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, ctypes.POINTER(POASensorModeInfo)],
    ),
    ("POASetSensorMode", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    ("POAGetSensorMode", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAGetGainsAndOffsets", ctypes.c_int, [ctypes.c_int] + [_INT_P] * 8),
    ("POAGetErrorString", ctypes.c_char_p, [ctypes.c_int]),
    ("POAGetAPIVersion", ctypes.c_int, []),
    ("POAGetSDKVersion", ctypes.c_char_p, []),
]

_PW_SIGNATURES: list[tuple[str, Any, list[Any]]] = [
    ("POAGetPWCount", ctypes.c_int, []),
    ("POAGetPWProperties", ctypes.c_int, [ctypes.c_int, ctypes.POINTER(PWProperties)]),
    (
        "POAGetPWPropertiesByHandle",
        ctypes.c_int,
        [ctypes.c_int, ctypes.POINTER(PWProperties)],
    ),
    ("POAOpenPW", ctypes.c_int, [ctypes.c_int]),
    ("POAClosePW", ctypes.c_int, [ctypes.c_int]),
    ("POAGetCurrentPosition", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAGotoPosition", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    ("POAGetPWState", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POAGetOneWay", ctypes.c_int, [ctypes.c_int, _INT_P]),
    ("POASetOneWay", ctypes.c_int, [ctypes.c_int, ctypes.c_int]),
    (
        "POAGetPWFilterAlias",
        ctypes.c_int,
        [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int],
    ),
    ("POAGetPWFocusOffset", ctypes.c_int, [ctypes.c_int, ctypes.c_int, _INT_P]),
    ("POAResetPW", ctypes.c_int, [ctypes.c_int]),
    ("POAGetPWErrorString", ctypes.c_char_p, [ctypes.c_int]),
    ("POAGetPWSDKVer", ctypes.c_char_p, []),
]


def _bind(lib: Any, signatures: list[tuple[str, Any, list[Any]]]) -> None:
    """Apply restype/argtypes once per library handle, never per call."""
    if getattr(lib, _CONFIGURED, False):
        return
    for name, restype, argtypes in signatures:
        try:
            fn = getattr(lib, name)
        except AttributeError:
            # A older SDK may not export everything; fail when it is called,
            # naming the function, rather than at import for everyone.
            continue
        fn.restype = restype
        fn.argtypes = argtypes
    setattr(lib, _CONFIGURED, True)


class CameraSdk:
    """The Player One Camera SDK as Python methods. Stateless."""

    def __init__(self, lib: Any = None):
        if lib is None:
            from .loader import camera_library

            lib = camera_library()
        _bind(lib, _CAMERA_SIGNATURES)
        self._lib = lib
        self._value_types: dict[int, POAValueType] = {}

    # -- plumbing ---------------------------------------------------------

    def _call(self, name: str, *args: Any) -> None:
        code = int(getattr(self._lib, name)(*args))
        if code != POAErrors.POA_OK:
            raise POAError(name, code, self.error_string(code))

    def error_string(self, code: int) -> str:
        try:
            raw = self._lib.POAGetErrorString(int(code))
        except Exception:  # noqa: BLE001 - diagnostics must never mask the error
            return ""
        return raw.decode(errors="replace") if raw else ""

    # -- discovery --------------------------------------------------------

    def get_camera_count(self) -> int:
        """Count the cameras -- **and scan the bus**, which nothing else does.

        This is not an accessor. ``POAGetCameraProperties`` before any call to
        this returns ``POA_ERROR_INVALID_INDEX`` and leaves the struct zeroed,
        which looks like a broken camera rather than a call-order mistake.
        :meth:`get_camera_properties` therefore calls it for you.
        """
        return int(self._lib.POAGetCameraCount())

    def get_camera_properties(self, index: int) -> POACameraProperties:
        props = POACameraProperties()
        self._call("POAGetCameraProperties", int(index), ctypes.pointer(props))
        return props

    def get_camera_properties_by_id(self, camera_id: int) -> POACameraProperties:
        props = POACameraProperties()
        self._call("POAGetCameraPropertiesByID", int(camera_id), ctypes.pointer(props))
        return props

    def enumerate(self) -> list[POACameraProperties]:
        """Every attached camera, scanned then read, in index order."""
        return [self.get_camera_properties(i) for i in range(self.get_camera_count())]

    # -- lifecycle --------------------------------------------------------

    def open_camera(self, camera_id: int) -> None:
        self._call("POAOpenCamera", int(camera_id))

    def init_camera(self, camera_id: int) -> None:
        self._call("POAInitCamera", int(camera_id))

    def close_camera(self, camera_id: int) -> None:
        self._call("POACloseCamera", int(camera_id))

    # -- configuration ----------------------------------------------------

    def get_config_value_type(self, config: POAConfig) -> POAValueType:
        cached = self._value_types.get(int(config))
        if cached is not None:
            return cached
        out = ctypes.c_int()
        self._call("POAGetConfigValueType", int(config), ctypes.pointer(out))
        value_type = POAValueType(out.value)
        self._value_types[int(config)] = value_type
        return value_type

    def set_config(
        self, camera_id: int, config: POAConfig, value: Any, is_auto: bool = False
    ) -> None:
        """Write a setting, packing the union according to the config's type.

        Getting the union member wrong is silent: an int written into the float
        member is read back as a denormal, and nothing errors.
        """
        value_type = self.get_config_value_type(config)
        packed = POAConfigValue()
        if value_type is POAValueType.VAL_FLOAT:
            packed.floatValue = float(value)
        elif value_type is POAValueType.VAL_BOOL:
            packed.boolValue = int(bool(value))
        else:
            packed.intValue = int(value)
        self._call(
            "POASetConfig", int(camera_id), int(config), packed, int(bool(is_auto))
        )

    def get_config(self, camera_id: int, config: POAConfig) -> tuple[Any, bool]:
        value_type = self.get_config_value_type(config)
        packed = POAConfigValue()
        is_auto = ctypes.c_int()
        self._call(
            "POAGetConfig",
            int(camera_id),
            int(config),
            ctypes.pointer(packed),
            ctypes.pointer(is_auto),
        )
        if value_type is POAValueType.VAL_FLOAT:
            value: Any = packed.floatValue
        elif value_type is POAValueType.VAL_BOOL:
            value = bool(packed.boolValue)
        else:
            value = int(packed.intValue)
        return value, bool(is_auto.value)

    def get_configs_count(self, camera_id: int) -> int:
        out = ctypes.c_int()
        self._call("POAGetConfigsCount", int(camera_id), ctypes.pointer(out))
        return out.value

    def get_config_attributes(self, camera_id: int, index: int) -> POAConfigAttributes:
        attrs = POAConfigAttributes()
        self._call(
            "POAGetConfigAttributes", int(camera_id), int(index), ctypes.pointer(attrs)
        )
        return attrs

    def get_config_attributes_by_id(
        self, camera_id: int, config: POAConfig
    ) -> POAConfigAttributes:
        attrs = POAConfigAttributes()
        self._call(
            "POAGetConfigAttributesByConfigID",
            int(camera_id),
            int(config),
            ctypes.pointer(attrs),
        )
        return attrs

    # -- geometry ---------------------------------------------------------

    def get_image_start_pos(self, camera_id: int) -> tuple[int, int]:
        x, y = ctypes.c_int(), ctypes.c_int()
        self._call(
            "POAGetImageStartPos", int(camera_id), ctypes.pointer(x), ctypes.pointer(y)
        )
        return x.value, y.value

    def set_image_start_pos(self, camera_id: int, start_x: int, start_y: int) -> None:
        self._call("POASetImageStartPos", int(camera_id), int(start_x), int(start_y))

    def get_image_size(self, camera_id: int) -> tuple[int, int]:
        w, h = ctypes.c_int(), ctypes.c_int()
        self._call(
            "POAGetImageSize", int(camera_id), ctypes.pointer(w), ctypes.pointer(h)
        )
        return w.value, h.value

    def set_image_size(self, camera_id: int, width: int, height: int) -> None:
        """Set the ROI size. **Read it back.**

        Measured on an Ares-M PRO: width rounds *down* to a multiple of 4 and
        height *down* to a multiple of 2. The header documents the width rule and
        says nothing about the height one.
        """
        self._call("POASetImageSize", int(camera_id), int(width), int(height))

    def get_image_bin(self, camera_id: int) -> int:
        out = ctypes.c_int()
        self._call("POAGetImageBin", int(camera_id), ctypes.pointer(out))
        return out.value

    def set_image_bin(self, camera_id: int, binning: int) -> None:
        """Set binning. **Read size and start position back** -- both change."""
        self._call("POASetImageBin", int(camera_id), int(binning))

    def get_image_format(self, camera_id: int) -> POAImgFormat:
        out = ctypes.c_int()
        self._call("POAGetImageFormat", int(camera_id), ctypes.pointer(out))
        return POAImgFormat(out.value)

    def set_image_format(self, camera_id: int, image_format: POAImgFormat) -> None:
        self._call("POASetImageFormat", int(camera_id), int(image_format))

    # -- exposure ---------------------------------------------------------

    def start_exposure(self, camera_id: int, single_frame: bool = True) -> None:
        """``single_frame`` True is Snap mode, False is continuous Video mode."""
        self._call("POAStartExposure", int(camera_id), int(bool(single_frame)))

    def stop_exposure(self, camera_id: int) -> None:
        self._call("POAStopExposure", int(camera_id))

    def get_camera_state(self, camera_id: int) -> POACameraState:
        out = ctypes.c_int()
        self._call("POAGetCameraState", int(camera_id), ctypes.pointer(out))
        return POACameraState(out.value)

    def image_ready(self, camera_id: int) -> bool:
        out = ctypes.c_int()
        self._call("POAImageReady", int(camera_id), ctypes.pointer(out))
        return out.value == POABool.POA_TRUE

    def get_image_data(
        self, camera_id: int, buffer: np.ndarray, timeout_ms: int
    ) -> None:
        """Fill a preallocated ``uint8`` buffer in place. No copy, no allocation.

        The size argument is ``buffer.nbytes``, not ``buffer.size``. Those agree
        only for a ``uint8`` array -- hand this a ``uint16`` array and the vendor's
        ``.size`` spelling under-reports by half, which the SDK answers with
        ``POA_ERROR_SIZE_LESS`` if you are lucky and a short read if you are not.
        """
        if buffer.dtype != np.uint8:
            raise TypeError(f"image buffer must be uint8, got {buffer.dtype}")
        if not buffer.flags["C_CONTIGUOUS"]:
            raise ValueError("image buffer must be C-contiguous")
        ptr = buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))
        self._call(
            "POAGetImageData",
            int(camera_id),
            ptr,
            ctypes.c_long(buffer.nbytes),
            int(timeout_ms),
        )

    def get_dropped_images_count(self, camera_id: int) -> int:
        out = ctypes.c_int()
        self._call("POAGetDroppedImagesCount", int(camera_id), ctypes.pointer(out))
        return out.value

    # -- sensor modes -----------------------------------------------------

    def get_sensor_mode_count(self, camera_id: int) -> int:
        """0 means this camera has no selectable sensor modes -- not an error."""
        out = ctypes.c_int()
        self._call("POAGetSensorModeCount", int(camera_id), ctypes.pointer(out))
        return out.value

    def get_sensor_mode_info(self, camera_id: int, index: int) -> POASensorModeInfo:
        info = POASensorModeInfo()
        self._call(
            "POAGetSensorModeInfo", int(camera_id), int(index), ctypes.pointer(info)
        )
        return info

    def get_sensor_mode(self, camera_id: int) -> int:
        out = ctypes.c_int()
        self._call("POAGetSensorMode", int(camera_id), ctypes.pointer(out))
        return out.value

    def set_sensor_mode(self, camera_id: int, index: int) -> None:
        self._call("POASetSensorMode", int(camera_id), int(index))

    # -- presets and versions ---------------------------------------------

    def get_gains_and_offsets(self, camera_id: int) -> dict[str, int]:
        """The vendor's recommended gain/offset presets for this sensor.

        Better than a hardcoded table: these come from the camera and follow it
        across models.
        """
        names = [
            "gain_highest_dr",
            "hc_gain",
            "unity_gain",
            "gain_lowest_rn",
            "offset_highest_dr",
            "offset_hc_gain",
            "offset_unity_gain",
            "offset_lowest_rn",
        ]
        outs = [ctypes.c_int() for _ in names]
        self._call(
            "POAGetGainsAndOffsets", int(camera_id), *(ctypes.pointer(o) for o in outs)
        )
        return {name: out.value for name, out in zip(names, outs, strict=True)}

    def get_sdk_version(self) -> str:
        raw = self._lib.POAGetSDKVersion()
        return raw.decode(errors="replace") if raw else ""

    def get_api_version(self) -> int:
        return int(self._lib.POAGetAPIVersion())


class FilterWheelSdk:
    """The Player One FilterWheel SDK as Python methods. Stateless."""

    def __init__(self, lib: Any = None):
        if lib is None:
            from .loader import filterwheel_library

            lib = filterwheel_library()
        _bind(lib, _PW_SIGNATURES)
        self._lib = lib

    def _call(self, name: str, *args: Any) -> None:
        code = int(getattr(self._lib, name)(*args))
        if code != PWErrors.PW_OK:
            raise PWError(name, code, self.error_string(code))

    def error_string(self, code: int) -> str:
        try:
            raw = self._lib.POAGetPWErrorString(int(code))
        except Exception:  # noqa: BLE001
            return ""
        return raw.decode(errors="replace") if raw else ""

    def get_count(self) -> int:
        """Count the wheels -- and, as with the camera SDK, scan for them.

        The header warns of a side effect worth knowing: on the *first* detection
        after connecting, the wheel self-checks and moves to position 1.
        """
        return int(self._lib.POAGetPWCount())

    def get_properties(self, index: int) -> PWProperties:
        props = PWProperties()
        self._call("POAGetPWProperties", int(index), ctypes.pointer(props))
        return props

    def enumerate(self) -> list[PWProperties]:
        return [self.get_properties(i) for i in range(self.get_count())]

    def open(self, handle: int) -> None:
        self._call("POAOpenPW", int(handle))

    def close(self, handle: int) -> None:
        self._call("POAClosePW", int(handle))

    def get_position(self, handle: int) -> int:
        """0-based position, or ``-1`` while the wheel is moving.

        MEASURED: the SDK reports a move in progress by **returning
        ``PW_ERROR_IS_MOVING``**, not by answering ``-1``. The wire protocol uses
        ``0xFF`` as a between-detents sentinel and the header describes ``-1``, so
        both the obvious readings are wrong -- a caller that only checks the value
        gets an exception instead, at the one moment it is most likely to poll.

        Normalised here to the sentinel the rest of the code expects, so exactly
        one place knows.
        """
        out = ctypes.c_int()
        try:
            self._call("POAGetCurrentPosition", int(handle), ctypes.pointer(out))
        except PWError as exc:
            if exc.error is PWErrors.PW_ERROR_IS_MOVING:
                return -1
            raise
        return out.value

    def goto_position(self, handle: int, position: int) -> None:
        self._call("POAGotoPosition", int(handle), int(position))

    def get_state(self, handle: int) -> PWState:
        out = ctypes.c_int()
        self._call("POAGetPWState", int(handle), ctypes.pointer(out))
        return PWState(out.value)

    def get_one_way(self, handle: int) -> bool:
        """Whether the wheel always approaches a position from one direction."""
        out = ctypes.c_int()
        self._call("POAGetOneWay", int(handle), ctypes.pointer(out))
        return bool(out.value)

    def set_one_way(self, handle: int, one_way: bool) -> None:
        """Trade move time for repeatability on a wheel with backlash in its detents."""
        self._call("POASetOneWay", int(handle), int(bool(one_way)))

    def get_filter_alias(self, handle: int, position: int) -> str:
        """The name the wheel itself stores for a position (max 24 chars)."""
        buf = ctypes.create_string_buffer(64)
        self._call("POAGetPWFilterAlias", int(handle), int(position), buf, 64)
        return buf.value.decode(errors="replace")

    def get_focus_offset(self, handle: int, position: int) -> int:
        out = ctypes.c_int()
        self._call(
            "POAGetPWFocusOffset", int(handle), int(position), ctypes.pointer(out)
        )
        return out.value

    def reset(self, handle: int) -> None:
        self._call("POAResetPW", int(handle))

    def get_sdk_version(self) -> str:
        raw = self._lib.POAGetPWSDKVer()
        return raw.decode(errors="replace") if raw else ""
