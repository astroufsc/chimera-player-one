# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Enumerations transcribed from ``PlayerOneCamera.h`` and ``PlayerOnePW.h``.

The vendor's spelling is kept exactly, so any symbol here can be grepped for in
``_sdk/include/``. The headers are shipped beside the libraries for that reason.

Values are written out rather than left implicit. The C enums are contiguous from
zero, so ``auto()`` would agree today -- but these numbers cross an ABI, and a
member inserted in a future SDK would silently renumber everything after it. The
vendor is careful about this (``POA_EXP`` was appended at 31 in 3.8.0 explicitly
to preserve binary compatibility) and we should be too.
"""

from __future__ import annotations

import enum

__all__ = [
    "POABayerPattern",
    "POABool",
    "POACameraState",
    "POAConfig",
    "POAErrors",
    "POAImgFormat",
    "POAValueType",
    "PWErrors",
    "PWState",
]


class POABool(enum.IntEnum):
    POA_FALSE = 0
    POA_TRUE = 1


class POABayerPattern(enum.IntEnum):
    POA_BAYER_RG = 0
    POA_BAYER_BG = 1
    POA_BAYER_GR = 2
    POA_BAYER_GB = 3
    POA_BAYER_MONO = -1


class POAImgFormat(enum.IntEnum):
    POA_RAW8 = 0
    POA_RAW16 = 1
    POA_RGB24 = 2
    POA_MONO8 = 3
    #: Sentinel terminating ``POACameraProperties.imgFormats``. Decode that array
    #: by **truncating** here, never by filtering this value out -- the padding
    #: past the sentinel is zero, and zero is ``POA_RAW8``. See BUILD-LOG entry 4.
    POA_END = -1

    @property
    def bytes_per_pixel(self) -> int:
        return {
            POAImgFormat.POA_RAW8: 1,
            POAImgFormat.POA_RAW16: 2,
            POAImgFormat.POA_RGB24: 3,
            POAImgFormat.POA_MONO8: 1,
        }[self]


class POAErrors(enum.IntEnum):
    POA_OK = 0
    POA_ERROR_INVALID_INDEX = 1
    POA_ERROR_INVALID_ID = 2
    POA_ERROR_INVALID_CONFIG = 3
    POA_ERROR_INVALID_ARGU = 4
    POA_ERROR_NOT_OPENED = 5
    POA_ERROR_DEVICE_NOT_FOUND = 6
    POA_ERROR_OUT_OF_LIMIT = 7
    POA_ERROR_EXPOSURE_FAILED = 8
    POA_ERROR_TIMEOUT = 9
    POA_ERROR_SIZE_LESS = 10
    POA_ERROR_EXPOSING = 11
    POA_ERROR_POINTER = 12
    POA_ERROR_CONF_CANNOT_WRITE = 13
    POA_ERROR_CONF_CANNOT_READ = 14
    POA_ERROR_ACCESS_DENIED = 15
    POA_ERROR_OPERATION_FAILED = 16
    POA_ERROR_MEMORY_FAILED = 17


class POACameraState(enum.IntEnum):
    STATE_CLOSED = 0
    STATE_OPENED = 1
    STATE_EXPOSING = 2


class POAValueType(enum.IntEnum):
    VAL_INT = 0
    VAL_FLOAT = 1
    VAL_BOOL = 2


class POAConfig(enum.IntEnum):
    """Camera settings, all reached through ``POASetConfig``/``POAGetConfig``.

    There are no dedicated cooling or bandwidth functions -- those are just
    members of this enum, which is why the list is long and mixed.
    """

    POA_EXPOSURE = 0  #: microseconds, int. Prefer POA_EXP; see below.
    POA_GAIN = 1
    POA_HARDWARE_BIN = 2
    POA_TEMPERATURE = 3  #: read-only, float, degrees C
    POA_WB_R = 4
    POA_WB_G = 5
    POA_WB_B = 6
    POA_OFFSET = 7
    POA_AUTOEXPO_MAX_GAIN = 8
    POA_AUTOEXPO_MAX_EXPOSURE = 9  #: milliseconds, unlike POA_EXPOSURE
    POA_AUTOEXPO_BRIGHTNESS = 10
    POA_GUIDE_NORTH = 11
    POA_GUIDE_SOUTH = 12
    POA_GUIDE_EAST = 13
    POA_GUIDE_WEST = 14
    POA_EGAIN = 15  #: read-only, float, e-/ADU at the current gain
    POA_COOLER_POWER = 16  #: read-only, int, 0-100 %
    POA_TARGET_TEMP = 17  #: int, degrees C. Inert until POA_COOLER is enabled.
    POA_COOLER = 18  #: bool; drives the cooler and the fan together
    POA_HEATER = 19  #: read-only, deprecated since SDK 3.1.0; follows the cooler
    POA_HEATER_POWER = 20
    POA_FAN_POWER = 21
    POA_FLIP_NONE = 22  #: the four flips are exclusive toggles, not a bitfield,
    POA_FLIP_HORI = 23  #: and each ignores the value passed to POASetConfig
    POA_FLIP_VERT = 24
    POA_FLIP_BOTH = 25
    POA_FRAME_LIMIT = 26  #: 0 = unlimited
    POA_HQI = 27
    POA_USB_BANDWIDTH_LIMIT = 28
    POA_PIXEL_BIN_SUM = 29  #: True sums binned pixels, False (default) averages
    POA_MONO_BIN = 30
    #: Seconds, **float**, range [0.00001, 7200.0]. Added in SDK 3.8.0 to raise
    #: the exposure ceiling without breaking ABI, and the vendor's own changelog
    #: says to use it in place of POA_EXPOSURE. We do.
    POA_EXP = 31


class PWErrors(enum.IntEnum):
    PW_OK = 0
    PW_ERROR_INVALID_INDEX = 1
    PW_ERROR_INVALID_HANDLE = 2
    PW_ERROR_INVALID_ARGU = 3
    PW_ERROR_NOT_OPENED = 4
    PW_ERROR_NOT_FOUND = 5
    PW_ERROR_IS_MOVING = 6
    PW_ERROR_POINTER = 7
    #: The header attributes this to "sending commands too often or removed".
    PW_ERROR_OPERATION_FAILED = 8
    #: The header's advice for this one is to call POAResetPW.
    PW_ERROR_FIRMWARE_ERROR = 9


class PWState(enum.IntEnum):
    PW_STATE_CLOSED = 0
    PW_STATE_OPENED = 1
    PW_STATE_MOVING = 2
