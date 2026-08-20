# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Paulo Henrique Silva <ph.silva@gmail.com>
"""Put the Player One shared libraries into the package, and prove which ones.

    uv run scripts/vendor_sdk.py                  # vendor from --sdk-dir
    uv run scripts/vendor_sdk.py --verify         # check hashes, write nothing
    uv run scripts/vendor_sdk.py --build-libusb   # macOS: build the universal libusb

The wheel ships every platform's libraries and picks one at import, so a user
needs no vendor download, no compiler, and no ``*_LIBRARY_PATH``. This script is
what puts them there, and ``provenance.toml`` is what lets CI prove none of them
changed behind our back.

Run it on **macOS**: the camera dylib needs ``install_name_tool`` (see below), and
that is the one step that cannot be done from another host. Everything else is
plain extraction. The results are committed, so this runs when an SDK is bumped,
not on every build.

Why each blob is rewritten on the way in
----------------------------------------

**Symlinks are resolved to real files.** Each vendor ``lib/`` ships one real
library plus three symlinks (``.so``, ``.so.3``, ``.so.3.10``). Zip -- and
therefore wheels -- handle symlinks inconsistently across tools and platforms, so
we ship a single real file per target under a fixed name and let the loader name
it exactly.

**macOS gets extra rpaths.** The camera dylib asks for
``@rpath/libusb-1.0.0.dylib`` and carries only ``@executable_path/../Frameworks``.
dyld resolves ``@rpath`` against the loading image *and the main executable*, so
whether it works depends on which Python is running -- it succeeds under a
homebrew python3 and fails under a uv-managed one, with the same libusb
installed. We add ``@loader_path`` (our vendored libusb wins) plus the three
usual system prefixes (a system copy still works if ours is deleted). The
dependency name itself is left alone, so the library stays swappable.

**Linux gets nothing.** No ``patchelf``, no ``$ORIGIN``. The loader preloads
libusb by absolute path with ``RTLD_GLOBAL`` before the camera library, and the
dynamic linker satisfies a ``NEEDED`` soname from an already-loaded object.

**Windows gets nothing.** The DLL imports only ``KERNEL32``/``USER32``/
``SETUPAPI`` and is built ``/MT``; there is no libusb and no MSVC runtime to
carry.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SDK_OUT = REPO / "src" / "chimera_player_one" / "_sdk"
WORK = REPO / "vendor-work"
DEFAULT_SDK_DIR = REPO.parent / "chimera-player-one-research" / "sdk"

CAMERA_VERSION = "3.10.1"
FILTERWHEEL_VERSION = "1.2.3"
LIBUSB_VERSION = "1.0.30"
LIBUSB_URL = (
    f"https://github.com/libusb/libusb/releases/download/"
    f"v{LIBUSB_VERSION}/libusb-{LIBUSB_VERSION}.tar.bz2"
)

#: Vendor arch directory -> our arch directory. The vendor's names are not the
#: ones ``platform.machine()`` reports, and README_PLATFORMS.txt gives the GCC
#: triples they mean: x86 'i686-linux-gnu', x64 'x86_64-linux-gnu', arm32
#: 'arm-linux-gnueabihf', arm64 'aarch64-linux-gnu'.
LINUX_ARCHES = {"x64": "x64", "arm64": "arm64", "arm32": "arm32", "x86": "x86"}
WINDOWS_ARCHES = {"x64": "x64", "x86": "x86"}


@dataclass
class Emitted:
    """One file placed under ``_sdk/``, and where it came from."""

    path: str
    source_archive: str
    source_member: str
    sha256: str = ""
    patched: list[str] = field(default_factory=list)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_member(archive: Path, member: str, dest: Path) -> None:
    """Copy one member out of a tar/zip to ``dest``, following symlinks.

    ``tarfile.extractfile`` returns ``None`` for a symlink member rather than
    resolving it, which is why every caller names the *real* versioned file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf, dest.open("wb") as out:
            with zf.open(member) as src:
                shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as tf:
            src = tf.extractfile(member)
            if src is None:
                raise SystemExit(
                    f"{archive.name}: {member} is a symlink or directory, not a "
                    "real file -- name the versioned file instead"
                )
            with dest.open("wb") as out:
                shutil.copyfileobj(src, out)
    dest.chmod(0o755)


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def patch_macos_camera(lib: Path) -> list[str]:
    """Add the rpaths that make libusb resolve without an interpreter's help."""
    applied = []
    for rpath in (
        "@loader_path",
        "/opt/homebrew/lib",
        "/usr/local/lib",
        "/opt/local/lib",
    ):
        run("install_name_tool", "-add_rpath", rpath, str(lib))
        applied.append(f"install_name_tool -add_rpath {rpath}")
    # install_name_tool invalidates the signature; an unsigned dylib will not
    # load on Apple Silicon. Ad-hoc re-sign.
    run("codesign", "--force", "--sign", "-", str(lib))
    applied.append("codesign --force --sign -")
    return applied


def camera_targets(sdk_dir: Path) -> list[tuple[Path, str, str]]:
    v = CAMERA_VERSION
    mac = sdk_dir / f"PlayerOne_Camera_SDK_MacOS_V{v}.tar.gz"
    lin = sdk_dir / f"PlayerOne_Camera_SDK_Linux_V{v}.tar.gz"
    win = sdk_dir / f"PlayerOne_Camera_SDK_Windows_V{v}.zip"
    out: list[tuple[Path, str, str]] = [
        (
            mac,
            f"PlayerOne_Camera_SDK_MacOS_V{v}/lib/libPlayerOneCamera.{v}.dylib",
            "macos/libPlayerOneCamera.dylib",
        ),
    ]
    for vendor_arch, ours in LINUX_ARCHES.items():
        out.append(
            (
                lin,
                f"PlayerOne_Camera_SDK_Linux_V{v}/lib/{vendor_arch}/libPlayerOneCamera.so.{v}",
                f"linux/{ours}/libPlayerOneCamera.so",
            )
        )
    for vendor_arch, ours in WINDOWS_ARCHES.items():
        out.append(
            (
                win,
                f"lib/{vendor_arch}/PlayerOneCamera.dll",
                f"windows/{ours}/PlayerOneCamera.dll",
            )
        )
    return out


def filterwheel_targets(sdk_dir: Path) -> list[tuple[Path, str, str]]:
    v = FILTERWHEEL_VERSION
    mac = sdk_dir / f"PlayerOne_FilterWheel_SDK_MacOS_V{v}.tar.gz"
    lin = sdk_dir / f"PlayerOne_FilterWheel_SDK_Linux_V{v}.tar.gz"
    win = sdk_dir / f"PlayerOne_FilterWheel_SDK_Windows_V{v}.zip"
    out: list[tuple[Path, str, str]] = [
        (
            mac,
            f"PlayerOne_FilterWheel_SDK_MacOS_V{v}/lib/libPlayerOnePW.{v}.dylib",
            "macos/libPlayerOnePW.dylib",
        ),
    ]
    for vendor_arch, ours in LINUX_ARCHES.items():
        out.append(
            (
                lin,
                f"PlayerOne_FilterWheel_SDK_Linux_V{v}/lib/{vendor_arch}/libPlayerOnePW.so.{v}",
                f"linux/{ours}/libPlayerOnePW.so",
            )
        )
    for vendor_arch, ours in WINDOWS_ARCHES.items():
        out.append(
            (
                win,
                f"lib/{vendor_arch}/PlayerOnePW.dll",
                f"windows/{ours}/PlayerOnePW.dll",
            )
        )
    return out


def support_targets(sdk_dir: Path) -> list[tuple[Path, str, str]]:
    """Licence, udev rules and the headers the binding was transcribed from."""
    cv, fv = CAMERA_VERSION, FILTERWHEEL_VERSION
    lin = sdk_dir / f"PlayerOne_Camera_SDK_Linux_V{cv}.tar.gz"
    mac = sdk_dir / f"PlayerOne_Camera_SDK_MacOS_V{cv}.tar.gz"
    fmac = sdk_dir / f"PlayerOne_FilterWheel_SDK_MacOS_V{fv}.tar.gz"
    return [
        (mac, f"PlayerOne_Camera_SDK_MacOS_V{cv}/license.txt", "LICENSE-PlayerOne.txt"),
        (
            lin,
            f"PlayerOne_Camera_SDK_Linux_V{cv}/udev/99-player_one_astronomy.rules",
            "99-player_one_astronomy.rules",
        ),
        (
            mac,
            f"PlayerOne_Camera_SDK_MacOS_V{cv}/include/PlayerOneCamera.h",
            "include/PlayerOneCamera.h",
        ),
        (
            fmac,
            f"PlayerOne_FilterWheel_SDK_MacOS_V{fv}/include/PlayerOnePW.h",
            "include/PlayerOnePW.h",
        ),
    ]


def build_libusb() -> Path:
    """Build a universal (x86_64 + arm64) libusb for macOS, from the release tarball.

    Homebrew ships arm64 only, so an Intel Mac would get nothing from it. The
    autotools build takes both ``-arch`` flags directly, which is the whole trick.
    """
    if sys.platform != "darwin":
        raise SystemExit("--build-libusb is macOS only")
    WORK.mkdir(parents=True, exist_ok=True)
    tarball = WORK / f"libusb-{LIBUSB_VERSION}.tar.bz2"
    if not tarball.exists():
        print(f"  downloading {LIBUSB_URL}")
        run("curl", "-sSL", "-o", str(tarball), LIBUSB_URL)
    src = WORK / f"libusb-{LIBUSB_VERSION}"
    if src.exists():
        shutil.rmtree(src)
    with tarfile.open(tarball) as tf:
        tf.extractall(WORK, filter="data")
    arch = "-arch arm64 -arch x86_64 -mmacosx-version-min=11.0"
    print("  configure (universal)")
    subprocess.run(
        [
            "./configure",
            "--disable-dependency-tracking",
            "--disable-static",
            "--enable-shared",
            "--disable-udev",
            f"CFLAGS={arch}",
            f"LDFLAGS={arch}",
            "--host=aarch64-apple-darwin",
        ],
        cwd=src,
        check=True,
        capture_output=True,
    )
    print("  make")
    subprocess.run(["make", "-j8"], cwd=src, check=True, capture_output=True)
    built = src / "libusb" / ".libs" / "libusb-1.0.0.dylib"
    if not built.exists():
        raise SystemExit("libusb build produced no dylib")
    return built


def vendor(sdk_dir: Path, with_libusb: bool) -> None:
    if not sdk_dir.is_dir():
        raise SystemExit(f"no SDK directory at {sdk_dir}")
    if SDK_OUT.exists():
        shutil.rmtree(SDK_OUT)
    SDK_OUT.mkdir(parents=True)

    emitted: list[Emitted] = []
    groups = [
        ("camera", camera_targets(sdk_dir)),
        ("filterwheel", filterwheel_targets(sdk_dir)),
        ("support", support_targets(sdk_dir)),
    ]
    for label, targets in groups:
        print(f"{label}:")
        for archive, member, rel in targets:
            if not archive.exists():
                raise SystemExit(f"missing archive {archive}")
            dest = SDK_OUT / rel
            extract_member(archive, member, dest)
            rec = Emitted(path=rel, source_archive=archive.name, source_member=member)
            if rel == "macos/libPlayerOneCamera.dylib" and sys.platform == "darwin":
                rec.patched = patch_macos_camera(dest)
            rec.sha256 = sha256(dest)
            emitted.append(rec)
            print(f"  {rel}  ({dest.stat().st_size:,} bytes)")

    if with_libusb:
        print("libusb:")
        built = build_libusb()
        dest = SDK_OUT / "macos" / "libusb-1.0.0.dylib"
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        # So the camera dylib's @rpath/libusb-1.0.0.dylib resolves to this copy
        # via the @loader_path rpath added above.
        run("install_name_tool", "-id", "@rpath/libusb-1.0.0.dylib", str(dest))
        run("codesign", "--force", "--sign", "-", str(dest))
        emitted.append(
            Emitted(
                path="macos/libusb-1.0.0.dylib",
                source_archive=f"libusb-{LIBUSB_VERSION}.tar.bz2",
                source_member="built from source (universal x86_64+arm64)",
                sha256=sha256(dest),
                patched=[
                    "install_name_tool -id @rpath/libusb-1.0.0.dylib",
                    "codesign --force --sign -",
                ],
            )
        )
        print(f"  macos/libusb-1.0.0.dylib  ({dest.stat().st_size:,} bytes)")
        shutil.copy2(
            WORK / f"libusb-{LIBUSB_VERSION}" / "COPYING",
            SDK_OUT / "LICENSE-libusb.txt",
        )

    write_provenance(sdk_dir, emitted)
    total = sum((SDK_OUT / e.path).stat().st_size for e in emitted)
    print(f"\n{len(emitted)} files, {total:,} bytes total")


def write_provenance(sdk_dir: Path, emitted: list[Emitted]) -> None:
    lines = [
        "# Generated by scripts/vendor_sdk.py -- do not edit by hand.",
        "#",
        "# Every shipped binary, where it came from, and what we did to it. CI runs",
        "# `vendor_sdk.py --verify` against this file, so an SDK that changes without",
        "# the version changing cannot pass silently.",
        "",
        "[sdk.camera]",
        'name = "PlayerOne Camera SDK"',
        f'version = "{CAMERA_VERSION}"',
        'vendor = "Player One Astronomy Co., Ltd."',
        'licence = "LICENSE-PlayerOne.txt"',
        "",
        "[sdk.filterwheel]",
        'name = "PlayerOne FilterWheel SDK"',
        f'version = "{FILTERWHEEL_VERSION}"',
        'vendor = "Player One Astronomy Co., Ltd."',
        'licence = "LICENSE-PlayerOne.txt"',
        "",
        "[sdk.libusb]",
        'name = "libusb"',
        f'version = "{LIBUSB_VERSION}"',
        f'source = "{LIBUSB_URL}"',
        'licence = "LICENSE-libusb.txt"  # LGPL-2.1-or-later, dynamically linked',
        "",
    ]
    for e in emitted:
        lines += [
            "[[file]]",
            f'path = "{e.path}"',
            f'sha256 = "{e.sha256}"',
            f'source_archive = "{e.source_archive}"',
            f'source_member = "{e.source_member}"',
        ]
        if e.patched:
            inner = ", ".join(f'"{p}"' for p in e.patched)
            lines.append(f"patched = [{inner}]")
        lines.append("")
    (SDK_OUT / "provenance.toml").write_text("\n".join(lines))


def verify() -> int:
    manifest = SDK_OUT / "provenance.toml"
    if not manifest.exists():
        print(f"FAIL no provenance at {manifest}", file=sys.stderr)
        return 1
    data = tomllib.loads(manifest.read_text())
    bad = 0
    for entry in data.get("file", []):
        path = SDK_OUT / entry["path"]
        if not path.exists():
            print(f"FAIL missing   {entry['path']}")
            bad += 1
            continue
        actual = sha256(path)
        if actual != entry["sha256"]:
            print(f"FAIL sha256    {entry['path']}")
            print(f"     recorded  {entry['sha256']}")
            print(f"     actual    {actual}")
            bad += 1
        else:
            print(f"ok   {entry['path']}")
    n = len(data.get("file", []))
    print(f"\n{n - bad}/{n} files match provenance.toml")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--sdk-dir",
        type=Path,
        default=DEFAULT_SDK_DIR,
        help=f"where the vendor archives live (default: {DEFAULT_SDK_DIR})",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="check shipped files against provenance.toml and exit",
    )
    ap.add_argument(
        "--build-libusb",
        action="store_true",
        help="macOS: build and vendor a universal libusb (needs network)",
    )
    args = ap.parse_args()
    if args.verify:
        return verify()
    vendor(args.sdk_dir, with_libusb=args.build_libusb)
    print("\nNow run:  uv run scripts/vendor_sdk.py --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
