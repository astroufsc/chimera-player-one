# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""The loader is the whole promise of this package: install it, and it works.

So these tests are about *not needing anything* -- no environment variable, no
working directory, no vendor download.
"""

import ctypes
import os
import platform
import tomllib
from pathlib import Path

import pytest

from chimera_player_one.sdk import loader


class TestVendoredTree:
    def test_every_platform_ships(self):
        """A wheel built on macOS must still carry the Linux and Windows blobs.

        This is the failure a `.gitignore` with `*.so` in it produces, and it is
        invisible until someone installs on the platform you did not build on.
        """
        root = loader.sdk_root()
        expected = [
            "macos/libPlayerOneCamera.dylib",
            "macos/libPlayerOnePW.dylib",
            "macos/libusb-1.0.0.dylib",
            "windows/x64/PlayerOneCamera.dll",
            "windows/x86/PlayerOneCamera.dll",
        ]
        expected += [
            f"linux/{arch}/lib{stem}.so"
            for arch in ("x64", "arm64", "arm32", "x86")
            for stem in ("PlayerOneCamera", "PlayerOnePW")
        ]
        missing = [rel for rel in expected if not (root / rel).exists()]
        assert missing == [], f"vendored libraries missing from the package: {missing}"

    def test_licence_travels_with_the_blobs(self):
        """The Player One licence requires the notice to accompany copies."""
        text = (loader.sdk_root() / "LICENSE-PlayerOne.txt").read_text()
        assert "Player One Astronomy" in text

    def test_provenance_covers_every_shipped_binary(self):
        """Anything loadable must be attributable. A blob with no recorded origin
        is one nobody can check, and CI's --verify would not notice it."""
        root = loader.sdk_root()
        data = tomllib.loads((root / "provenance.toml").read_text())
        recorded = {entry["path"] for entry in data["file"]}
        on_disk = {
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and p.suffix in {".dylib", ".so", ".dll"}
        }
        assert on_disk <= recorded, f"unattributed binaries: {on_disk - recorded}"

    def test_udev_rules_are_shipped(self):
        """A wheel cannot install them, so it must at least carry them."""
        assert loader.udev_rules_path().exists()


class TestLoading:
    def test_camera_library_loads(self):
        lib = loader.camera_library()
        lib.POAGetSDKVersion.restype = ctypes.c_char_p
        assert lib.POAGetSDKVersion().decode().startswith("3.")

    def test_filterwheel_library_loads(self):
        lib = loader.filterwheel_library()
        lib.POAGetPWSDKVer.restype = ctypes.c_char_p
        assert lib.POAGetPWSDKVer().decode().startswith("1.")

    def test_loads_from_any_working_directory(self, tmp_path):
        """The vendor wrapper does `LoadLibrary("./libPlayerOneCamera.dylib")`,
        which works only from the SDK directory -- and a dev shell is always
        sitting in the checkout, so the bug hides. Load from somewhere else."""
        previous = Path.cwd()
        try:
            os.chdir(tmp_path)
            loader.camera_library.cache_clear()
            assert loader.camera_library() is not None
        finally:
            os.chdir(previous)

    def test_no_library_path_variables_are_required(self, monkeypatch):
        """Nothing here may depend on a `*_LIBRARY_PATH`. A process cannot set one
        for itself anyway -- the dynamic linker reads them at exec -- which is why
        the vendor's sample scripts set one and then re-exec themselves."""
        for var in (
            "DYLD_LIBRARY_PATH",
            "DYLD_FALLBACK_LIBRARY_PATH",
            "LD_LIBRARY_PATH",
        ):
            monkeypatch.delenv(var, raising=False)
        loader.camera_library.cache_clear()
        assert loader.camera_library() is not None

    def test_library_is_cached(self):
        loader.camera_library.cache_clear()
        assert loader.camera_library() is loader.camera_library()


class TestFailureIsLoud:
    def test_missing_library_raises_named_error_not_oserror(
        self, monkeypatch, tmp_path
    ):
        """Never a DummyDLL that returns -1 from every call: -1 is not a valid
        POAErrors, so the real failure surfaces as a ValueError somewhere else
        entirely. Fail here, once, with the reason."""
        monkeypatch.setattr(loader, "_platform_dir", lambda: tmp_path / "nowhere")
        loader.camera_library.cache_clear()
        with pytest.raises(loader.SdkNotAvailableError) as excinfo:
            loader.camera_library()
        message = str(excinfo.value)
        assert "vendor_sdk.py" in message, "the error should say how to fix it"

    def test_unsupported_platform_names_what_is_supported(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Haiku")
        with pytest.raises(loader.SdkNotAvailableError) as excinfo:
            loader._platform_dir()
        assert "Haiku" in str(excinfo.value)

    def test_unknown_machine_names_the_known_ones(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "sparc64")
        with pytest.raises(loader.SdkNotAvailableError) as excinfo:
            loader._platform_dir()
        assert "aarch64" in str(excinfo.value)


class TestDescribe:
    def test_describe_never_raises(self, monkeypatch):
        """`doctor` runs on the machine where something is broken. If describe()
        raised, it would be useless exactly when it is needed."""
        monkeypatch.setattr(platform, "system", lambda: "Haiku")
        info = loader.describe()
        assert "unsupported" in str(info["platform_dir"])

    def test_describe_reports_versions_here(self):
        loader.camera_library.cache_clear()
        loader.filterwheel_library.cache_clear()
        info = loader.describe()
        camera, wheel = info["camera"], info["filterwheel"]
        assert isinstance(camera, dict) and isinstance(wheel, dict)
        assert camera["version"].startswith("3.")
        assert wheel["version"].startswith("1.")
