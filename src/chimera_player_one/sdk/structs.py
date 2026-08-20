# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""ctypes structures for the Player One SDKs, field for field from the headers.

Field order and types are an ABI contract: get one wrong and every field after it
reads plausible garbage, with no error anywhere. ``test_structs.py`` pins the
sizes -- ``sizeof(POACameraProperties)`` is 992 on a 64-bit host, and that number
is a checksum over the whole declaration.

Two subtleties worth stating, because both are easy to get wrong:

**The config value really is a union, and it must be declared as one.**
``POAConfigValue`` is ``{long intValue; double floatValue; POABool boolValue;}``.
``long`` is 8 bytes on LP64 Unix and 4 on Windows LLP64 -- but ``ctypes.c_long``
already tracks the platform's C ``long``, and the ``double`` member forces the
union to 8 bytes on both. So declaring it as a real ``ctypes.Union`` is correct
everywhere. What is *not* correct is passing a bare ``c_long`` out-parameter
where the SDK writes a union, which under-allocates by 4 bytes on Windows.

**Sentinel-terminated arrays are truncated, never filtered.**
``bins`` ends at the first ``0`` and ``imgFormats`` at the first ``-1``, with the
remainder zero-padded -- and ``0`` is a valid ``POA_RAW8``. Filtering the
sentinel out of ``imgFormats`` reports five image formats a mono camera does not
have. The decoding properties below do it correctly, once, so no caller has to
remember.
"""

from __future__ import annotations

import ctypes

from .enums import POABayerPattern, POAImgFormat

__all__ = [
    "POACameraProperties",
    "POAConfigAttributes",
    "POAConfigValue",
    "POASensorModeInfo",
    "PWProperties",
]


class POAConfigValue(ctypes.Union):
    _fields_ = [
        ("intValue", ctypes.c_long),
        ("floatValue", ctypes.c_double),
        ("boolValue", ctypes.c_int),
    ]


class POACameraProperties(ctypes.Structure):
    _fields_ = [
        ("cameraModelName", ctypes.c_char * 256),
        ("userCustomID", ctypes.c_char * 16),
        ("cameraID", ctypes.c_int),
        ("maxWidth", ctypes.c_int),
        ("maxHeight", ctypes.c_int),
        ("bitDepth", ctypes.c_int),
        ("isColorCamera", ctypes.c_int),
        ("isHasST4Port", ctypes.c_int),
        ("isHasCooler", ctypes.c_int),
        ("isUSB3Speed", ctypes.c_int),
        ("bayerPattern", ctypes.c_int),
        ("pixelSize", ctypes.c_double),
        ("SN", ctypes.c_char * 64),
        ("sensorModelName", ctypes.c_char * 32),
        ("localPath", ctypes.c_char * 256),
        ("bins", ctypes.c_int * 8),
        ("imgFormats", ctypes.c_int * 8),
        ("isSupportHardBin", ctypes.c_int),
        ("pID", ctypes.c_int),
        # 248, not 256: SDK 3.3.0 took 8 bytes for isSupportHardBin and pID.
        ("reserved", ctypes.c_char * 248),
    ]

    @property
    def model(self) -> str:
        return self.cameraModelName.decode(errors="replace")

    @property
    def serial(self) -> str:
        return self.SN.decode(errors="replace")

    @property
    def sensor(self) -> str:
        return self.sensorModelName.decode(errors="replace")

    @property
    def custom_id(self) -> str:
        return self.userCustomID.decode(errors="replace")

    @property
    def is_colour(self) -> bool:
        return bool(self.isColorCamera)

    @property
    def has_cooler(self) -> bool:
        return bool(self.isHasCooler)

    @property
    def has_st4(self) -> bool:
        return bool(self.isHasST4Port)

    @property
    def supports_hardware_bin(self) -> bool:
        return bool(self.isSupportHardBin)

    @property
    def bayer(self) -> POABayerPattern:
        return POABayerPattern(self.bayerPattern)

    @property
    def binnings(self) -> list[int]:
        """Supported binnings, truncated at the ``0`` terminator."""
        return list(_truncate(self.bins, 0))

    @property
    def formats(self) -> list[POAImgFormat]:
        """Supported image formats, truncated at ``POA_END``."""
        return [POAImgFormat(v) for v in _truncate(self.imgFormats, -1)]


def _truncate(array, sentinel: int):
    """Yield values up to -- not including -- the first ``sentinel``.

    Deliberately not ``[v for v in array if v != sentinel]``: the padding after
    the sentinel is zero, and zero means ``POA_RAW8``, so filtering invents
    formats. Measured on an Ares-M PRO, whose raw ``imgFormats`` is
    ``[0, 1, -1, 0, 0, 0, 0, 0]``.
    """
    for value in array:
        if value == sentinel:
            return
        yield value


class POAConfigAttributes(ctypes.Structure):
    _fields_ = [
        ("isSupportAuto", ctypes.c_int),
        ("isWritable", ctypes.c_int),
        ("isReadable", ctypes.c_int),
        ("configID", ctypes.c_int),
        ("valueType", ctypes.c_int),
        ("maxValue", POAConfigValue),
        ("minValue", POAConfigValue),
        ("defaultValue", POAConfigValue),
        ("szConfName", ctypes.c_char * 64),
        ("szDescription", ctypes.c_char * 128),
        ("reserved", ctypes.c_char * 64),
    ]

    @property
    def name(self) -> str:
        return self.szConfName.decode(errors="replace")

    @property
    def description(self) -> str:
        return self.szDescription.decode(errors="replace")


class POASensorModeInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * 64),
        ("desc", ctypes.c_char * 128),
    ]


class PWProperties(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_char * 64),
        ("Handle", ctypes.c_int),
        ("PositionCount", ctypes.c_int),
        ("SN", ctypes.c_char * 32),
        ("Reserved", ctypes.c_char * 32),
    ]

    @property
    def name(self) -> str:
        return self.Name.decode(errors="replace")

    @property
    def serial(self) -> str:
        return self.SN.decode(errors="replace")
