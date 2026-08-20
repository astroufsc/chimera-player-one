# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Find and load the Player One shared libraries that ship inside this package.

The one rule here: **a user never sets a library path.** Not ``LD_LIBRARY_PATH``,
not ``DYLD_LIBRARY_PATH``, not a working directory. Those cannot be set by a
process for itself anyway -- the dynamic linker reads them at ``exec`` -- which is
why the vendor's own sample scripts set one and then ``os.execvp`` themselves.
Everything needed is resolved from absolute paths under ``_sdk/``.

Two things this deliberately does not do, both copied from the vendor wrapper and
both wrong:

- ``cdll.LoadLibrary("./libPlayerOneCamera.dylib")``, a **cwd-relative** path. It
  works from the SDK directory and nowhere else.
- a ``DummyDLL`` fallback that swallows the load error and returns ``-1`` from
  every call. ``-1`` is not a valid ``POAErrors``, so a missing library surfaces
  much later as a ``ValueError`` inside an unrelated call. A library that will not
  load is fatal, and it is reported here, once, with the reason.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import os
import platform
import sys
from pathlib import Path

__all__ = [
    "SdkNotAvailableError",
    "camera_library",
    "filterwheel_library",
    "describe",
    "sdk_root",
    "udev_rules_path",
]


class SdkNotAvailableError(RuntimeError):
    """A vendor library could not be loaded, with the reason and the fix."""


#: ``platform.machine()`` spellings we accept for each vendored Linux directory.
#: The vendor names its directories x64/arm64/arm32/x86; README_PLATFORMS.txt maps
#: them to x86_64-, aarch64-, arm- and i686-linux-gnu respectively.
_LINUX_MACHINES = {
    "x64": {"x86_64", "amd64"},
    "arm64": {"aarch64", "arm64", "armv8b", "armv8l"},
    "arm32": {"armv6l", "armv7l", "armv7", "arm"},
    "x86": {"i386", "i486", "i586", "i686", "x86"},
}
_WINDOWS_MACHINES = {
    "x64": {"amd64", "x86_64"},
    "x86": {"x86", "i386", "i686"},
}


def sdk_root() -> Path:
    """Absolute path to the vendored SDK tree. Never relative, never cwd-based."""
    return Path(__file__).resolve().parent.parent / "_sdk"


def _platform_dir() -> Path:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        # One universal binary covers Apple Silicon and Intel, so machine is
        # deliberately not consulted.
        return sdk_root() / "macos"
    table = {"Linux": _LINUX_MACHINES, "Windows": _WINDOWS_MACHINES}.get(system)
    if table is None:
        raise SdkNotAvailableError(
            f"Player One ships no libraries for {system}. Supported: macOS, "
            f"Linux (x86_64, aarch64, armv7, i686), Windows (x64, x86)."
        )
    for arch, machines in table.items():
        if machine in machines:
            return sdk_root() / system.lower() / arch
    known = sorted(m for ms in table.values() for m in ms)
    raise SdkNotAvailableError(
        f"no vendored Player One library for {system} {machine!r}. "
        f"Known {system} machines: {', '.join(known)}."
    )


def _preload_libusb() -> str | None:
    """Make ``libusb`` resolvable before the camera library asks for it.

    Only Linux needs this, and only because it is free: the dynamic linker
    satisfies a ``NEEDED`` soname from an already-loaded object, so preloading by
    absolute path removes any dependence on ``ldconfig``, ``$ORIGIN`` or
    ``patchelf``.

    macOS is handled at vendoring time instead -- ``@loader_path`` is baked into
    the camera dylib's rpaths and libusb sits beside it -- because dyld matches an
    already-loaded image by install name, so preloading would not help. Windows
    has no libusb at all.
    """
    if platform.system() != "Linux":
        return None
    vendored = _platform_dir() / "libusb-1.0.so.0"
    candidates = [str(vendored)] if vendored.exists() else []
    found = ctypes.util.find_library("usb-1.0")
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            ctypes.CDLL(candidate, mode=ctypes.RTLD_GLOBAL)
            return candidate
        except OSError:
            continue
    return None


def _library_filename(stem: str) -> str:
    system = platform.system()
    if system == "Darwin":
        return f"lib{stem}.dylib"
    if system == "Windows":
        return f"{stem}.dll"
    return f"lib{stem}.so"


def _explain(path: Path, exc: OSError) -> str:
    text = str(exc)
    lines = [f"could not load {path.name} from {path.parent}", f"  {text}"]
    if "libusb" in text:
        lines.append("")
        lines.append(
            "  The Player One camera library needs libusb-1.0. This package ships"
        )
        lines.append(
            "  one, so seeing this means the vendored copy is missing or was built"
        )
        lines.append("  for another architecture. Re-vendor with:")
        lines.append("    uv run scripts/vendor_sdk.py --build-libusb")
        if platform.system() == "Linux":
            lines.append("  or install the system package: apt install libusb-1.0-0")
    elif not path.exists():
        lines.append("")
        lines.append(
            "  That file is not in the package. The wheel should carry it; if you"
        )
        lines.append("  are running from a checkout, vendor the SDK first:")
        lines.append("    uv run scripts/vendor_sdk.py --build-libusb")
    return "\n".join(lines)


def _load(stem: str) -> ctypes.CDLL:
    directory = _platform_dir()
    path = directory / _library_filename(stem)
    _preload_libusb()
    if sys.platform == "win32" and directory.is_dir():
        # Windows resolves a DLL's own imports against the DLL search path, not
        # its directory, so the directory has to be added explicitly.
        # Spelled `sys.platform`, not `platform.system()`, because a type checker
        # narrows on the former and knows this call exists only here.
        os.add_dll_directory(str(directory))
    try:
        return ctypes.CDLL(str(path))
    except OSError as exc:
        raise SdkNotAvailableError(_explain(path, exc)) from exc


@functools.cache
def camera_library() -> ctypes.CDLL:
    """The Player One Camera SDK, loaded once per process."""
    return _load("PlayerOneCamera")


@functools.cache
def filterwheel_library() -> ctypes.CDLL:
    """The Player One FilterWheel SDK, loaded once per process."""
    return _load("PlayerOnePW")


def udev_rules_path() -> Path:
    """The udev rules a Linux host needs for non-root access to the devices."""
    return sdk_root() / "99-player_one_astronomy.rules"


def describe() -> dict[str, object]:
    """What loaded, from where, and what version -- the data behind ``doctor``.

    Never raises: each probe records its own failure, because the point of this
    is to run on a machine where something is wrong.
    """
    info: dict[str, object] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "sdk_root": str(sdk_root()),
    }
    try:
        info["platform_dir"] = str(_platform_dir())
    except SdkNotAvailableError as exc:
        info["platform_dir"] = f"unsupported: {exc}"
        return info

    info["libusb"] = _preload_libusb() or (
        "not needed" if platform.system() != "Linux" else "not found"
    )
    for label, loader, version_fn, count_fn in (
        ("camera", camera_library, "POAGetSDKVersion", "POAGetCameraCount"),
        ("filterwheel", filterwheel_library, "POAGetPWSDKVer", "POAGetPWCount"),
    ):
        try:
            lib = loader()
        except SdkNotAvailableError as exc:
            info[label] = {"error": str(exc)}
            continue
        version = getattr(lib, version_fn)
        version.restype = ctypes.c_char_p
        # The count call is also the bus scan, so it must run before anything
        # asks for device properties. See docs/notes/BUILD-LOG.md, entry 4.
        info[label] = {
            "version": version().decode(),
            "count": int(getattr(lib, count_fn)()),
        }
    return info
