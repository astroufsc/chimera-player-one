# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""What the driver does when a camera stops delivering frames.

Written from a real failure. On 2026-08-20 an Ares-M PRO ran for 6h40m, took two
frames, and then failed every frame of a five-frame batch with::

    POAImageReady failed: POA_ERROR_TIMEOUT (no frame after 15.0 s for a 5.000 s exposure)
    POAGetImageData failed: POA_ERROR_OPERATION_FAILED (operation failed)

with libusb aborting bulk transfers on the image endpoint throughout. It took a
physical re-plug *and* a chimera restart to clear.

The part worth encoding is what the logs proved: the camera was **not** gone.
``get_metadata`` -- five ``POAGetConfig`` calls -- succeeded moments before each
failing exposure. EP0 was healthy and only the bulk image endpoint was dead. So
these tests use ``wedge_image_pipe``, which reproduces exactly that asymmetry,
and not any of the error-injection flags, because no error code was ever
returned: the SDK simply never said the frame was ready.
"""

import time

import pytest
from chimera.interfaces.camera import CameraStatus

from chimera_player_one.instruments.playeronecamera import PlayerOneCamera
from chimera_player_one.sdk import simulator
from chimera_player_one.sdk.bindings import CameraSdk
from chimera_player_one.sdk.camera import Camera, ExposureTimeoutError
from chimera_player_one.sdk.enums import POACameraState, POAErrors
from chimera_player_one.sdk.errors import POAError
from chimera_player_one.sdk.simulator import ARES_M_PRO, SEDNA_M, FakeCameraLibrary

fired: dict = {}


@pytest.fixture
def lib():
    return FakeCameraLibrary([ARES_M_PRO])


@pytest.fixture
def cam(lib):
    camera = Camera.open(CameraSdk(lib), index=0)
    camera.configure(binning=4)
    yield camera
    camera.close()


@pytest.fixture
def driver(manager, monkeypatch, request):
    """A real driver on a fake library we can still reach and break.

    ``_open`` imports ``FakeCameraLibrary`` *inside* the function, so the name is
    resolved on the module at call time -- which is what makes this monkeypatch
    reach the instance the driver builds, in the manager's thread, without a
    single test hook in production code.
    """
    built: list[FakeCameraLibrary] = []
    specs = getattr(request, "param", None) or [ARES_M_PRO, SEDNA_M]

    class Recording(FakeCameraLibrary):
        def __init__(self, _ignored=None, **kwargs):
            super().__init__(list(specs), **kwargs)
            built.append(self)

    monkeypatch.setattr(simulator, "FakeCameraLibrary", Recording)

    def make(**config):
        settings = {
            "simulated": True,
            "gain": 220,
            "offset": 35,
            "exposure_timeout_margin": 0.5,
        }
        settings.update(config)
        manager.add_class(PlayerOneCamera, "poa", settings)
        fired.clear()
        proxy = manager.get_proxy("/PlayerOneCamera/poa")
        proxy.expose_complete += _expose_complete
        proxy.readout_complete += _readout_complete
        return proxy, built[0]

    return make


def _expose_complete(request, status):
    fired["expose_complete"] = status


def _readout_complete(image_url, status):
    fired["readout_complete"] = (image_url, status)


def _awaited(key, timeout=10.0):
    """The event, once it arrives. **Single-frame requests only.**

    Events cross the bus on a handler thread, so `expose()` returning does not
    mean they have landed -- and a *failing* frame returns fast enough to lose
    that race every time. Asserting on `fired[key]` directly is a flake waiting
    for a slow machine.

    `fired` keeps only the most recent of each event, so on a multi-frame batch
    this returns whichever frame got there first and means nothing. Assert on the
    returned image list instead.
    """
    deadline = time.monotonic() + timeout
    while key not in fired and time.monotonic() < deadline:
        time.sleep(0.02)
    assert key in fired, f"{key} never fired"
    return fired[key]


class TestTheWedgeItself:
    """The SDK layer, no chimera, no bus."""

    def test_a_frame_that_never_arrives_says_what_the_camera_said(self, cam, lib):
        lib.wedge_image_pipe = True
        cam.begin_exposure(0.05)
        with pytest.raises(ExposureTimeoutError) as excinfo:
            cam.wait_for_image(0.05, margin=0.3)
        exc = excinfo.value
        # Asserted as facts, not as prose: the message is for humans and will
        # be reworded, these are what code and tests can rely on.
        assert exc.is_timeout
        assert exc.camera_state is POACameraState.STATE_EXPOSING
        assert exc.dropped == 0
        assert exc.stop_error is not None
        assert exc.stop_error.is_operation_failed
        assert exc.polls > 0

    def test_evidence_is_gathered_before_the_stop(self, cam, lib):
        """The header says the dropped counter resets on stop, and the stop also
        flips the state out of STATE_EXPOSING. Diagnose afterwards and both
        numbers are destroyed -- which is exactly what we wished we had."""
        lib.wedge_image_pipe = True
        cam.begin_exposure(0.05)
        with pytest.raises(ExposureTimeoutError):
            cam.wait_for_image(0.05, margin=0.3)
        assert lib.calls.index("POAGetDroppedImagesCount") < lib.calls.index(
            "POAStopExposure"
        )
        assert lib.calls.index("POAGetCameraState") < lib.calls.index("POAStopExposure")

    def test_config_reads_keep_working_while_the_image_pipe_is_dead(self, cam, lib):
        """The asymmetry the whole diagnosis rests on. If the fake ever loses
        this, these tests stop reproducing the real failure."""
        lib.wedge_image_pipe = True
        assert cam.gain == 0 or cam.gain >= 0
        assert isinstance(cam.temperature, float)

    def test_a_stop_that_fails_is_returned_not_swallowed(self, cam, lib):
        lib.fail_always["POAStopExposure"] = POAErrors.POA_ERROR_OPERATION_FAILED
        cam.begin_exposure(0.01)
        error = cam.abort_exposure()
        assert error is not None and error.is_operation_failed
        error = cam.close()
        assert error is not None and error.is_operation_failed
        assert "POACloseCamera" in lib.calls  # closed anyway

    def test_an_argument_error_is_not_transport(self):
        """The predicate table, pinned. Recovery keys on is_transport and a
        wrong answer here either reconnects on a typo or never reconnects."""
        assert (
            POAError("x", int(POAErrors.POA_ERROR_INVALID_ARGU)).is_transport is False
        )
        assert (
            POAError("x", int(POAErrors.POA_ERROR_ACCESS_DENIED)).is_transport is False
        )
        assert POAError("x", int(POAErrors.POA_ERROR_SIZE_LESS)).is_transport is False
        for code in (
            POAErrors.POA_ERROR_NOT_OPENED,
            POAErrors.POA_ERROR_DEVICE_NOT_FOUND,
            POAErrors.POA_ERROR_TIMEOUT,
            POAErrors.POA_ERROR_OPERATION_FAILED,
        ):
            assert POAError("x", int(code)).is_transport

    def test_operation_failed_is_not_disconnected(self):
        """The header calls code 16 "maybe the camera is disconnected suddenly"
        and also "the current mode is not matched". On 2026-08-20 it meant
        neither: the camera was right there, answering."""
        exc = POAError("POAGetImageData", int(POAErrors.POA_ERROR_OPERATION_FAILED))
        assert exc.is_operation_failed
        assert not exc.is_disconnected
        assert exc.is_transport


class TestReopen:
    def test_reopen_finds_the_same_camera_after_a_rescan(self):
        """Camera IDs are enumeration indices and a rescan can renumber them.
        Re-opening by index would silently hand back the other camera."""
        lib = FakeCameraLibrary([ARES_M_PRO, SEDNA_M])
        cam = Camera.open(CameraSdk(lib), serial=SEDNA_M.serial)
        assert cam.properties.serial == SEDNA_M.serial
        lib.reorder([1, 0])
        cam.reopen()
        assert cam.properties.serial == SEDNA_M.serial
        cam.close()

    def test_reopen_resets_the_camera(self):
        """POAInitCamera re-initialises the hardware, so everything a caller set
        is gone. This is what makes _restore_settings load-bearing."""
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        cam.configure(binning=4)
        cam.gain = 220
        assert cam.gain == 220
        cam.reopen()
        assert cam.gain != 220
        assert cam.geometry().binning == 1
        cam.close()

    def test_reopen_of_a_camera_that_is_gone_names_it(self):
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        lib.detached = True
        with pytest.raises(POAError) as excinfo:
            cam.reopen()
        assert ARES_M_PRO.serial in str(excinfo.value)

    def test_a_reopened_camera_is_usable_again(self):
        """`_closed` used to be one-way, so a Camera was single-use."""
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        cam.reopen()
        cam.configure(binning=4)
        exposure = cam.expose(0.01)
        assert exposure.data.shape == (752, 752)
        cam.close()


class TestTheDriver:
    """Through a real Manager and Bus, because the events are half the contract."""

    def test_a_failed_exposure_does_not_attempt_a_readout(self, driver, tmp_path):
        """CameraBase._base_expose throws away what _expose returned and calls
        _readout anyway. That is why every failed frame on 2026-08-20 produced a
        *second* traceback, from POAGetImageData, after paying the transfer
        timeout to fetch pixels the exposure had already said were not there.

        One assertion pins both halves: no bus traffic, and the events still fire.
        """
        camera, lib = driver(reconnect_attempts=0)
        lib.wedge_image_pipe = True
        camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "a")}
        )
        assert _awaited("expose_complete") == CameraStatus.ERROR
        assert _awaited("readout_complete") == (None, CameraStatus.ERROR)
        assert "POAGetImageData" not in lib.calls

    def test_a_wedged_pipe_is_reconnected_and_the_frame_retried(self, driver, tmp_path):
        camera, lib = driver()
        lib.wedge_image_pipe = True
        lib.wedge_clears_on_init = True
        urls = camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "b")}
        )
        assert len(urls) == 1
        assert _awaited("expose_complete") == CameraStatus.OK
        assert _awaited("readout_complete")[1] == CameraStatus.OK
        # once at __start__, once for the reconnect
        assert lib.calls.count("POAInitCamera") == 2

    def test_a_failed_frame_cancels_the_rest_of_the_batch(self, driver, tmp_path):
        """Five frames x ~20 s of identical failure was the worst part of the
        night. Counted in calls, not seconds: timing over the bus measures the
        bus."""
        camera, lib = driver(reconnect_attempts=1)
        lib.wedge_image_pipe = True
        lib.wedge_clears_on_init = False
        urls = camera.expose(
            {
                "exptime": 0.1,
                "frames": 5,
                "binning": "4x4",
                "filename": str(tmp_path / "c"),
            }
        )
        assert urls == ()
        # the frame and its single retry, and then nothing
        assert lib.calls.count("POAStartExposure") == 2

    def test_settings_are_reapplied_after_a_reconnect(self, driver, tmp_path):
        """POAInitCamera resets gain to the sensor default. A reconnect that
        skipped _restore_settings would come back at the wrong gain and the
        frames would look fine."""
        import astropy.io.fits as fits

        camera, lib = driver(gain=220)
        lib.wedge_image_pipe = True
        lib.wedge_clears_on_init = True
        urls = camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "d")}
        )
        assert len(urls) == 1
        path = urls[0].replace("file://", "").split(",")[0]
        with fits.open(path) as hdus:
            assert hdus[0].header["GAIN"] == 220

    def test_a_runtime_setpoint_is_not_reverted_by_a_reconnect(self, driver, tmp_path):
        """start_cooling over the bus does not write back to config, so
        restoring from config would silently undo an observer's change."""
        camera, lib = driver(temperature_setpoint=-10)
        camera.start_cooling(-15)
        lib.wedge_image_pipe = True
        lib.wedge_clears_on_init = True
        camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "e")}
        )
        assert camera.get_set_point() == -15

    @pytest.mark.parametrize("driver", [[ARES_M_PRO, SEDNA_M]], indirect=True)
    def test_the_camera_still_answers_under_the_right_name(self, driver, tmp_path):
        """A rescan can renumber the cameras. Reconnecting by index would put
        the other camera's frames under this camera's name, silently."""
        import astropy.io.fits as fits

        camera, lib = driver(model="Sedna-M", serial=None)
        # Renumbered by the rescan the reconnect itself performs -- the only
        # moment the order can actually change under a live handle.
        lib.reorder_on_next_scan = [1, 0]
        lib.wedge_image_pipe = True
        lib.wedge_clears_on_init = True
        urls = camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "f")}
        )
        assert len(urls) == 1
        path = urls[0].replace("file://", "").split(",")[0]
        with fits.open(path) as hdus:
            assert hdus[0].header["CAMSN"] == SEDNA_M.serial

    def test_a_batch_that_dies_halfway_reports_what_it_did_take(self, driver, tmp_path):
        """The realistic shape: some frames arrive, then the pipe wedges. The
        frames already saved must be returned and counted, not thrown away with
        the rest of the batch."""
        camera, lib = driver(reconnect_attempts=0)
        lib.wedge_after_frames = 2
        urls = camera.expose(
            {
                "exptime": 0.1,
                "frames": 5,
                "binning": "4x4",
                "filename": str(tmp_path / "h-$NUM"),
            }
        )
        # `urls` is the substantive claim: two frames survived and the batch
        # stopped instead of grinding through the remaining three. The events are
        # deliberately not asserted here -- `fired` holds only the most recent of
        # each, so in a multi-frame batch it says nothing about which frame it
        # came from.
        assert len(urls) == 2

    def test_a_camera_that_is_really_gone_gives_up_once(self, driver, tmp_path):
        camera, lib = driver(reconnect_attempts=1)
        lib.wedge_image_pipe = True
        lib.detached = True
        camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "g")}
        )
        assert _awaited("expose_complete") == CameraStatus.ERROR
        # __start__ opened it once; the single reconnect attempt never got far
        # enough to try again, because the rescan found nothing.
        assert lib.calls.count("POAOpenCamera") == 1
