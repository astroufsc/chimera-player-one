# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Paulo Henrique Silva <ph.silva@gmail.com>
"""Bring a Player One camera up one stage at a time, so a failure names itself.

    uv run scripts/poa_probe.py --stage discover
    uv run scripts/poa_probe.py --stage config
    uv run scripts/poa_probe.py --stage geometry
    uv run scripts/poa_probe.py --stage expose --exptime 0.5
    uv run scripts/poa_probe.py --stage saturation
    uv run scripts/poa_probe.py --stage cooling --target -10
    uv run scripts/poa_probe.py --stage wheel
    uv run scripts/poa_probe.py --stage all

Run them in order. The first one that fails is the one to debug -- that is the
whole value of a ladder over a single script that does everything.

Note this script carries **no PEP 723 header**, unlike the standalone report
scripts elsewhere. That is deliberate: an inline dependency block makes `uv run`
build an isolated environment *without the project*, and the entire point of this
ladder is to exercise the packaged libraries. It runs in the project environment,
where `chimera_player_one` is importable.

This replaces the `tests_sdk/` ladder in the research repo, which needs a
``DYLD_LIBRARY_PATH``, re-``execvp``s itself to apply it, ``chdir``s into the SDK
directory because the vendor wrapper dlopens a relative path, and points at a
stale 3.6.3 by a path that no longer exists. None of that is needed here: the
libraries ship inside the package.

| stage      | writes to the camera? |
|------------|-----------------------|
| discover   | no                    |
| config     | no                    |
| geometry   | **yes** (ROI/binning) |
| expose     | **yes** (exposes)     |
| saturation | **yes** (exposes)     |
| cooling    | **yes** (runs the TEC)|
| wheel      | **yes** (moves it)    |

Proving the checks can fail
---------------------------
Unplug the camera and run ``--stage discover``: it must say so and exit non-zero,
not traceback. Ask for ``--stage cooling`` on the Sedna-M, which has no cooler:
it must refuse by name rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from chimera_player_one.sdk.bindings import CameraSdk, FilterWheelSdk
from chimera_player_one.sdk.camera import Camera
from chimera_player_one.sdk.enums import POAConfig, POAImgFormat, PWState
from chimera_player_one.sdk.errors import PlayerOneError


def rule(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def stage_discover(args):
    """Enumerate. Nothing else matters until a camera appears here."""
    rule("discover")
    sdk = CameraSdk()
    print(f"SDK {sdk.get_sdk_version()}  API {sdk.get_api_version()}")
    cameras = sdk.enumerate()
    if not cameras:
        print("no cameras found -- check the cable, and on Linux the udev rules")
        return 1
    for props in cameras:
        print(f"\n  {props.model}   sn={props.serial}")
        print(
            f"    sensor    {props.sensor}  {props.maxWidth}x{props.maxHeight}  "
            f"{props.bitDepth}-bit  {props.pixelSize:.2f} um"
        )
        print(
            f"    features  cooler={props.has_cooler} st4={props.has_st4} "
            f"usb3={bool(props.isUSB3Speed)} hardbin={props.supports_hardware_bin}"
        )
        print(f"    bins      {props.binnings}")
        print(f"    formats   {[f.name for f in props.formats]}")
        print(f"    pID       0x{props.pID:04X}")
    if not any(c.isUSB3Speed for c in cameras):
        print(
            "\nNOTE none of these negotiated USB3. Readout will be slow; try "
            "another port or cable before blaming the driver."
        )
    return 0


def stage_config(args):
    """Read every setting the camera exposes, and its ranges. Writes nothing."""
    rule("config")
    with _open(args) as camera:
        sdk = camera._sdk
        cid = camera.camera_id
        print(f"{camera.properties.model}: {sdk.get_configs_count(cid)} settings")
        print("\n  gain presets (from the camera, not a table):")
        for key, value in camera.gain_presets().items():
            print(f"    {key:<22} {value}")
        print(f"\n  {'setting':<24}{'type':<10}{'range':<28}now")
        for config in POAConfig:
            try:
                attrs = sdk.get_config_attributes_by_id(cid, config)
                value, is_auto = sdk.get_config(cid, config)
            except PlayerOneError:
                continue
            vt = sdk.get_config_value_type(config)
            if vt.name == "VAL_FLOAT":
                lo, hi = attrs.minValue.floatValue, attrs.maxValue.floatValue
            else:
                lo, hi = attrs.minValue.intValue, attrs.maxValue.intValue
            flags = "".join(
                c
                for c, f in zip(
                    "wra", (attrs.isWritable, attrs.isReadable, attrs.isSupportAuto)
                )
                if f
            )
            print(
                f"  {config.name:<24}{vt.name:<10}[{lo}, {hi}]".ljust(62)
                + f"{value!r} ({flags})"
            )
    return 0


def stage_geometry(args):
    """Every binning, then a window. Read back, never assumed."""
    rule("geometry")
    with _open(args) as camera:
        for binning in camera.properties.binnings:
            geometry = camera.configure(
                binning=binning, image_format=POAImgFormat.POA_RAW16
            )
            expected_w = camera.properties.maxWidth // binning
            note = (
                "" if geometry.width == expected_w else f"  (rounded from {expected_w})"
            )
            print(
                f"  bin {binning}: {geometry.width}x{geometry.height} "
                f"at ({geometry.start_x},{geometry.start_y}){note}"
            )
        geometry = camera.configure(binning=1, window=(500, 500, 1024, 1024))
        print(
            f"  window (500,500,1024,1024) -> ({geometry.start_x},{geometry.start_y}) "
            f"{geometry.width}x{geometry.height}"
        )
        assert geometry.width == 1024 and geometry.height == 1024, "window not honoured"
    return 0


def stage_expose(args):
    """Take frames at each binning and report shape, stats and wall time."""
    rule(f"expose ({args.exptime} s)")
    with _open(args) as camera:
        for binning in (4, 2, 1):
            camera.configure(binning=binning, image_format=POAImgFormat.POA_RAW16)
            t0 = time.monotonic()
            exposure = camera.expose(args.exptime)
            elapsed = time.monotonic() - t0
            data = exposure.data
            print(
                f"  bin {binning}: {data.shape} {data.dtype}  "
                f"min={data.min()} max={data.max()} mean={data.mean():.1f}  "
                f"{elapsed:.2f} s wall  DATE-OBS={exposure.started_at.isoformat()}"
            )
            overhead = elapsed - args.exptime
            print(f"           readout+overhead {overhead:.2f} s")
    return 0


def stage_saturation(args):
    """Answer the open question: is RAW16 left-shifted, or 0..2^bitdepth-1?

    Point the camera at something bright -- a lamp, a white wall, the sky -- and
    run this. It reports the maximum ADU reached at a long exposure.

    ANSWERED on an Ares-M PRO, 2026-08-20: RAW16 **is** left-shifted, and full
    scale is `16383 << 2 == 65532` -- not 16383, and not 65535 either, because the
    low two bits are always zero. Kept as a stage because it has to be re-run for
    every new sensor, and because it also checks the number without needing light:
    if the data is shifted, every value is a multiple of `1 << (16 - bitDepth)`.

    That number sets `ccd_saturation_level`, and a wrong one silently corrupts
    every flat and every linearity correction downstream -- a routine told 65535
    never sees a saturated pixel and fits a curve through its own clipped data.
    """
    rule("saturation")
    with _open(args) as camera:
        depth = camera.properties.bitDepth
        camera.configure(binning=4, image_format=POAImgFormat.POA_RAW16)
        print(
            f"  {camera.properties.model} is {depth}-bit; "
            f"unshifted full scale = {(1 << depth) - 1}, shifted = 65535"
        )
        print("  point the camera at something BRIGHT for this to mean anything\n")
        for exptime in (0.01, 0.1, 1.0, 4.0):
            exposure = camera.expose(exptime)
            peak = int(exposure.data.max())
            saturated = np.count_nonzero(exposure.data >= peak) / exposure.data.size
            print(
                f"  {exptime:>5.2f} s  max={peak:>6}  "
                f"({100 * saturated:.2f} % of pixels at max)"
            )
        shift = 16 - depth
        residues = np.bincount(
            np.asarray(exposure.data).ravel() % (1 << shift), minlength=1 << shift
        )
        shifted = residues[0] / residues.sum() > 0.99
        full_scale = ((1 << depth) - 1) << shift if shifted else (1 << depth) - 1
        print(
            f"\n  low {shift} bits always zero: {shifted}  "
            f"({100 * residues[0] / residues.sum():.2f} % of values)"
        )
        print(f"  => ccd_saturation_level = {full_scale}")
        print("     (compare against the plateau above; they must agree)")
    return 0


def stage_cooling(args):
    """Engage the cooler and watch it approach setpoint."""
    rule(f"cooling (target {args.target} C)")
    with _open(args) as camera:
        if not camera.has_cooler:
            print(f"  {camera.properties.model} has no cooler -- nothing to test")
            return 1
        print(f"  ambient {camera.temperature:.1f} C")
        # Target first, then enable: the target is cached until the cooler is
        # switched on, so the other order silently uses the previous setpoint.
        camera.start_cooling(args.target)
        try:
            for _ in range(args.samples):
                time.sleep(2.0)
                print(
                    f"  {camera.temperature:>6.1f} C   "
                    f"cooler {camera.cooler_power:>3d} %   "
                    f"setpoint {camera.target_temperature:.0f} C"
                )
        finally:
            camera.stop_cooling()
            print("  cooler off")
    return 0


def stage_wheel(args):
    """Step the filter wheel through every position and read each one back."""
    rule("wheel")
    sdk = FilterWheelSdk()
    wheels = sdk.enumerate()
    if not wheels:
        print("no filter wheels found")
        return 1
    props = wheels[0]
    print(f"  {props.name}  sn={props.serial}  {props.PositionCount} positions")
    sdk.open(props.Handle)
    try:
        print(
            "  firmware names   :",
            [sdk.get_filter_alias(props.Handle, p) for p in range(props.PositionCount)],
        )
        print(
            "  firmware offsets :",
            [sdk.get_focus_offset(props.Handle, p) for p in range(props.PositionCount)],
        )
        while sdk.get_state(props.Handle) is PWState.PW_STATE_MOVING:
            time.sleep(0.05)
        start = sdk.get_position(props.Handle)
        print(f"  starting at position {start}")
        for position in range(props.PositionCount):
            t0 = time.monotonic()
            sdk.goto_position(props.Handle, position)
            # Poll the STATE, not the position: while moving, the SDK answers
            # POAGetCurrentPosition with an error return rather than a value.
            while sdk.get_state(props.Handle) is PWState.PW_STATE_MOVING:
                time.sleep(0.05)
            print(
                f"    -> {position} ({sdk.get_filter_alias(props.Handle, position)}) "
                f"in {time.monotonic() - t0:.2f} s"
            )
        sdk.goto_position(props.Handle, max(start, 0))
    finally:
        sdk.close(props.Handle)
    return 0


def _open(args):
    return Camera.open(serial=args.serial, model=args.model, index=args.index)


STAGES = {
    "discover": stage_discover,
    "config": stage_config,
    "geometry": stage_geometry,
    "expose": stage_expose,
    "saturation": stage_saturation,
    "cooling": stage_cooling,
    "wheel": stage_wheel,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stage",
        default="discover",
        choices=[*STAGES, "all"],
        help="which stage to run",
    )
    parser.add_argument("--serial", default=None, help="pick the camera by serial")
    parser.add_argument("--model", default=None, help="pick the camera by model name")
    parser.add_argument("--index", type=int, default=0, help="pick the camera by index")
    parser.add_argument("--exptime", type=float, default=0.1, help="exposure seconds")
    parser.add_argument(
        "--target", type=float, default=-10.0, help="cooling setpoint C"
    )
    parser.add_argument("--samples", type=int, default=10, help="cooling samples")
    args = parser.parse_args()

    stages = list(STAGES) if args.stage == "all" else [args.stage]
    worst = 0
    for name in stages:
        try:
            worst = max(worst, STAGES[name](args))
        except PlayerOneError as exc:
            print(f"\n{name}: FAILED -- {exc}", file=sys.stderr)
            return 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
