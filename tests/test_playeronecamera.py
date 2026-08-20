# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""The chimera driver, through a real Manager and Bus.

Constructing the object directly would test less than half of it: the four events
are how a caller learns an exposure finished, and a bare object has no bus to
deliver them on. The camera underneath is the fake library, so the ctypes layer
is real and only the vendor blob is absent.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import astropy.io.fits as fits
import pytest
from chimera.interfaces.camera import CameraFeature, CameraStatus

from chimera_player_one.instruments.playeronecamera import PlayerOneCamera

fired = {}


def _expose_begin(request):
    fired["expose_begin"] = request


def _expose_complete(request, status):
    fired["expose_complete"] = status


def _readout_begin(request):
    fired["readout_begin"] = request


def _readout_complete(image_url, status):
    fired["readout_complete"] = (image_url, status)


@pytest.fixture
def camera(manager, tmp_path):
    manager.add_class(
        PlayerOneCamera, "poa", {"simulated": True, "gain": 220, "offset": 35}
    )
    fired.clear()
    proxy = manager.get_proxy("/PlayerOneCamera/poa")
    proxy.expose_begin += _expose_begin
    proxy.expose_complete += _expose_complete
    proxy.readout_begin += _readout_begin
    proxy.readout_complete += _readout_complete
    return proxy


@pytest.fixture
def pool():
    p = ThreadPoolExecutor()
    yield p
    p.shutdown()


def _wait_for(predicate, timeout=20.0):
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout:
        time.sleep(0.05)
    return predicate()


class TestStartup:
    def test_identity_comes_from_the_camera_not_the_config(self, camera):
        """Nothing about the sensor should have to be typed into chimera.config."""
        assert camera["camera_model"] == "Ares-M PRO"
        assert camera["ccd_model"] == "IMX533"
        assert camera["ccd_width"] == 3008
        assert camera["ccd_height"] == 3008
        assert camera["pixel_size_x"] == pytest.approx(3.76)

    def test_saturation_is_the_shifted_full_scale(self, camera):
        """MEASURED, and it is neither obvious answer.

        RAW16 carries the 14-bit sensor value left-shifted by 2, so full scale is
        16383 << 2 == 65532: not 16383 (the data goes past it) and not 65535 (the
        low two bits are always zero, so it is unreachable). Confirmed on hardware
        by a flooded sensor plateauing at exactly 65532 with 97.6 % of pixels
        there, and by 100 % of dark-frame values being multiples of 4."""
        assert camera["ccd_saturation_level"] == 65532

    def test_binnings_index_the_readout_modes(self, camera):
        """If they disagree, _get_readout_mode_info silently falls back to full
        frame and every windowed request is quietly ignored."""
        binnings = camera.get_binnings()
        modes = camera.get_readout_modes()
        assert set(binnings) == {"1x1", "2x2", "3x3", "4x4"}
        for bin_id in binnings.values():
            assert bin_id in modes

    def test_readout_mode_width_is_a_multiple_of_four(self, camera):
        """The SDK rounds ROI width down to a multiple of 4, so a mode advertising
        1002 px at bin 3 promises something the camera cannot deliver."""
        for mode in camera.get_readout_modes().values():
            assert mode.width % 4 == 0

    def test_pixel_size_scales_with_binning(self, camera):
        modes = camera.get_readout_modes()
        assert modes[2].pixel_width == pytest.approx(3.76 * 2)

    def test_features_follow_the_hardware(self, camera):
        assert camera.supports(CameraFeature.TEMPERATURE_CONTROL) is True
        assert camera.supports(CameraFeature.PROGRAMMABLE_GAIN) is True
        assert camera.supports(CameraFeature.PROGRAMMABLE_OVERSCAN) is False


class TestExposure:
    def test_expose_fires_all_four_events(self, camera, tmp_path):
        """CameraBase fires none of them; a driver that forgets looks fine until
        something waits on expose_complete.

        Note the explicit tmp_path filename. A relative `filename` lands in the
        *current directory*, and `ImageUtil.make_filename` then appends -001,
        -002 ... up to 999 on collision -- so a test that writes into the repo
        gets slower every run and eventually starts failing timing assertions
        somewhere else entirely."""
        camera.expose(
            {"exptime": 0.05, "filename": str(tmp_path / "events"), "binning": "4x4"}
        )
        assert _wait_for(lambda: "readout_complete" in fired)
        assert fired["expose_complete"] == CameraStatus.OK
        assert fired["readout_complete"][1] == CameraStatus.OK
        assert fired["readout_complete"][0].startswith("file://")

    def test_frame_has_the_binned_shape(self, camera, tmp_path):
        urls = camera.expose(
            {"exptime": 0.05, "binning": "4x4", "filename": str(tmp_path / "bin4")}
        )
        path = urls[0].replace("file://", "").split(",")[0]
        with fits.open(path) as hdus:
            assert hdus[0].data.shape == (752, 752)

    def test_window_is_honoured(self, camera, tmp_path):
        urls = camera.expose(
            {
                "exptime": 0.05,
                "binning": "1x1",
                "window": "500:1523,600:1623",
                "filename": str(tmp_path / "win"),
            }
        )
        path = urls[0].replace("file://", "").split(",")[0]
        with fits.open(path) as hdus:
            assert hdus[0].data.shape == (1024, 1024)

    def test_multiple_frames(self, camera, tmp_path):
        urls = camera.expose(
            {
                "exptime": 0.02,
                "frames": 3,
                "binning": "4x4",
                "filename": str(tmp_path / "seq"),
            }
        )
        assert len(urls) == 3


class TestHeaders:
    """The FITS cards that are silently absent if `extras` is incomplete."""

    @pytest.fixture
    def header(self, camera, tmp_path):
        urls = camera.expose(
            {"exptime": 0.05, "binning": "2x2", "filename": str(tmp_path / "hdr")}
        )
        path = urls[0].replace("file://", "").split(",")[0]
        with fits.open(path) as hdus:
            return dict(hdus[0].header)

    def test_date_obs_is_present(self, header):
        """Not cosmetic. kepler's periodic-error fold anchors on mid-exposure,
        computed as DATE-OBS + EXPTIME/2; a missing one cannot be reconstructed
        and a wrong one slides the phase of the signal being measured."""
        assert "DATE-OBS" in header, "frame_start_time was not passed to _save_image"

    def test_exptime_is_present_and_correct(self, header):
        assert header["EXPTIME"] == pytest.approx(0.05)

    def test_ccd_temp_is_present(self, header):
        assert "CCD-TEMP" in header, "frame_temperature was not passed to _save_image"

    def test_ccdsum_reports_the_binning(self, header):
        assert header["CCDSUM"] == "2 2"

    def test_camera_identity_is_recorded(self, header):
        """Which camera took this, in a two-camera observatory."""
        assert header["INSTRUME"] == "Ares-M PRO"
        assert header["SENSOR"] == "IMX533"
        assert header["CAMSN"].startswith("FAKE")

    def test_photometric_calibration_cards(self, header):
        """Gain and e-/ADU are what turn ADU back into electrons; neither is
        recoverable from the pixels afterwards."""
        assert header["GAIN"] == 220
        assert header["OFFSET"] == 35
        assert "EGAIN" in header


class TestAbort:
    """Abort is tested in two places, on purpose.

    The *latency* claim -- "an abort is bounded by the poll interval, not by the
    exposure" -- belongs to our polling loop, so it is measured directly against
    the SDK layer with no bus in the way. Asserting it through a proxy measured
    something else: `expose` is `@lock`ed, the bus routes locked calls through a
    per-object FIFO lane, and `abort_exposure` can therefore queue behind a 30 s
    exposure depending on worker availability. That produced an intermittent
    failure whose signature was the suite taking exactly one extra 30 s exposure.
    Relaxing the bound would have hidden it; moving the assertion to the layer
    that owns the behaviour is the actual fix.

    What the bus-level test still proves is the part that matters there: the
    abort reaches the driver, the exposure ends, and the status is ABORTED.
    """

    def test_abort_latency_is_bounded_by_the_poll_interval(self):
        """No bus, no proxy: our loop, a 600 s exposure, and an abort flag.

        This is the reason readout never blocks inside POAGetImageData. If it
        did, abort latency on a long sub would be the transfer timeout.
        """
        import threading

        from chimera_player_one.sdk.bindings import CameraSdk
        from chimera_player_one.sdk.camera import Camera, ExposureAbortedError
        from chimera_player_one.sdk.simulator import FakeCameraLibrary

        with Camera.open(CameraSdk(FakeCameraLibrary())) as cam:
            cam.configure(binning=4)
            abort = threading.Event()
            cam.begin_exposure(600.0)
            threading.Timer(0.1, abort.set).start()
            t0 = time.monotonic()
            with pytest.raises(ExposureAbortedError):
                cam.wait_for_image(600.0, abort)
            elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"abort took {elapsed:.2f}s of a 600 s exposure"

    def test_abort_over_the_bus_ends_the_exposure(self, camera, pool, tmp_path):
        """No timing bound here -- see the class docstring."""
        future = pool.submit(
            camera.expose,
            {"exptime": 30.0, "binning": "4x4", "filename": str(tmp_path / "abort")},
        )
        assert _wait_for(camera.is_exposing, timeout=10)
        assert camera.abort_exposure() is True
        assert _wait_for(lambda: not camera.is_exposing(), timeout=40)
        future.result(timeout=40)
        assert fired["expose_complete"] == CameraStatus.ABORTED

    def test_abort_when_idle_is_false(self, camera):
        assert camera.abort_exposure() is False


class TestCooling:
    def test_cooling_round_trip(self, camera):
        camera.start_cooling(-10)
        assert camera.is_cooling() is True
        assert camera.get_set_point() == pytest.approx(-10)
        assert camera.get_temperature() == pytest.approx(-10)
        camera.stop_cooling()
        assert camera.is_cooling() is False

    def test_temperature_is_readable_before_cooling(self, camera):
        assert camera.get_temperature() == pytest.approx(25.0)
