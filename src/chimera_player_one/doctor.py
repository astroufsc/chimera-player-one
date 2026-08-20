# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""``chimera-player-one-doctor`` -- what loaded, what is attached, what is missing.

This runs on the machine where something is wrong, so nothing in it may raise:
every probe reports its own failure and the next one still runs. A tool that dies
on the first problem tells you about one problem.

It also installs the Linux udev rules, because a wheel cannot. Without them the
camera enumerates and every open fails with ``POA_ERROR_ACCESS_DENIED``, which is
the single most common first-run failure and says nothing about its cause.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from chimera_player_one.sdk import loader

UDEV_TARGET = Path("/etc/udev/rules.d/99-player_one_astronomy.rules")

_OK = "ok  "
_BAD = "FAIL"
_NA = "--  "


def _print_environment() -> None:
    print("environment")
    print(
        f"  {_OK} {platform.system()} {platform.machine()}, python {sys.version.split()[0]}"
    )
    print(f"       package  {Path(loader.__file__).resolve().parent.parent}")


def _print_vendored() -> None:
    print("\nvendored SDK")
    root = loader.sdk_root()
    manifest = root / "provenance.toml"
    if not manifest.exists():
        print(
            f"  {_BAD} no provenance.toml -- run: uv run scripts/vendor_sdk.py --build-libusb"
        )
        return
    data = tomllib.loads(manifest.read_text())
    for key, sdk in data.get("sdk", {}).items():
        print(f"  {_OK} {sdk.get('name', key)} {sdk.get('version', '?')}")
    missing = [
        e["path"] for e in data.get("file", []) if not (root / e["path"]).exists()
    ]
    if missing:
        print(f"  {_BAD} {len(missing)} shipped file(s) missing, e.g. {missing[0]}")
    else:
        print(f"  {_OK} {len(data.get('file', []))} files present")
    try:
        print(f"  {_OK} platform directory {loader._platform_dir()}")
    except loader.SdkNotAvailableError as exc:
        print(f"  {_BAD} {exc}")


def _print_libusb() -> None:
    print("\nlibusb")
    if platform.system() == "Windows":
        print(f"  {_NA} not used on Windows (SetupAPI/WinUSB)")
        return
    if platform.system() == "Darwin":
        vendored = loader.sdk_root() / "macos" / "libusb-1.0.0.dylib"
        if vendored.exists():
            print(f"  {_OK} bundled  {vendored.name}")
            print("       resolved through an @loader_path rpath baked into the")
            print("       camera library at vendoring time -- no DYLD_LIBRARY_PATH")
        else:
            print(f"  {_BAD} not bundled; the camera library will not load")
        return
    found = loader._preload_libusb()
    if found:
        print(f"  {_OK} {found}")
    else:
        print(f"  {_BAD} not found -- install it:  sudo apt install libusb-1.0-0")


def _print_udev() -> None:
    print("\nudev rules (Linux only)")
    if platform.system() != "Linux":
        print(f"  {_NA} not applicable on {platform.system()}")
        return
    if UDEV_TARGET.exists():
        print(f"  {_OK} installed at {UDEV_TARGET}")
        return
    print(f"  {_BAD} not installed at {UDEV_TARGET}")
    print("       Without them the camera enumerates but every open fails with")
    print("       POA_ERROR_ACCESS_DENIED. Install once, as root:")
    print("         sudo chimera-player-one-doctor --install-udev")


def _print_devices() -> None:
    print("\ncameras")
    try:
        from chimera_player_one.sdk.bindings import CameraSdk

        sdk = CameraSdk()
        print(
            f"  {_OK} camera SDK {sdk.get_sdk_version()} (API {sdk.get_api_version()})"
        )
        cameras = sdk.enumerate()
        if not cameras:
            print(f"  {_NA} none attached")
        for props in cameras:
            print(
                f"  {_OK} {props.model}  sn={props.serial}  {props.sensor} "
                f"{props.maxWidth}x{props.maxHeight} {props.bitDepth}-bit "
                f"{props.pixelSize:.2f}um"
            )
            print(
                f"       cooler={props.has_cooler} usb3={bool(props.isUSB3Speed)} "
                f"bins={props.binnings} formats={[f.name for f in props.formats]}"
            )
            # Presets need the camera opened, and doctor must stay read-only:
            # opening a camera another process is using would disturb it.
    except Exception as exc:  # noqa: BLE001 - the whole point is to report, not raise
        print(f"  {_BAD} {exc}")

    print("\nfilter wheels")
    try:
        from chimera_player_one.sdk.bindings import FilterWheelSdk

        pw = FilterWheelSdk()
        print(f"  {_OK} filter wheel SDK {pw.get_sdk_version()}")
        wheels = pw.enumerate()
        if not wheels:
            print(f"  {_NA} none attached")
        for props in wheels:
            print(
                f"  {_OK} {props.name}  sn={props.serial}  "
                f"{props.PositionCount} positions  handle={props.Handle}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  {_BAD} {exc}")


def _install_udev() -> int:
    if platform.system() != "Linux":
        print(f"udev rules are a Linux thing; nothing to do on {platform.system()}")
        return 0
    source = loader.udev_rules_path()
    if not source.exists():
        print(f"FAIL the rules file is missing from the package: {source}")
        return 1
    try:
        UDEV_TARGET.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, UDEV_TARGET)
    except PermissionError:
        print("FAIL need root. Try:  sudo chimera-player-one-doctor --install-udev")
        return 1
    print(f"ok   installed {UDEV_TARGET}")
    for command in (["udevadm", "control", "--reload-rules"], ["udevadm", "trigger"]):
        try:
            subprocess.run(command, check=True, capture_output=True)
            print(f"ok   {' '.join(command)}")
        except (OSError, subprocess.CalledProcessError) as exc:
            print(
                f"--   {' '.join(command)} failed ({exc}); re-plug the camera instead"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="chimera-player-one-doctor",
        description="Check a chimera-player-one installation and the attached hardware.",
    )
    parser.add_argument(
        "--install-udev",
        action="store_true",
        help="install the Linux udev rules a wheel cannot install (needs root)",
    )
    args = parser.parse_args()
    if args.install_udev:
        return _install_udev()

    _print_environment()
    _print_vendored()
    _print_libusb()
    _print_udev()
    _print_devices()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
