# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Who is allowed to talk to the camera, and when.

Nothing in this stack used to serialise POA calls on a camera handle. The
control loop polled temperature on its own thread while the exposure thread
polled ``POAImageReady`` and then blocked in ``POAGetImageData``, and
``bindings.py`` had no ``threading`` import at all. Whether that caused the
2026-08-20 wedge is still unproven -- a Python mutex cannot order our calls
against the SDK's *own* transfer thread -- but it is the one thing we could fix
and did.

The shape being pinned here comes from INDIGO's driver for this same hardware:
one mutex per library, held **per call**, plus a rule that the temperature timer
may run while the sensor integrates and must not run during the readout. That
distinction is the whole point. Blanking a temperature widget for the length of a
five-frame batch would be a far worse cure than the disease, so there are tests
for both halves: nothing during the transfer, and something during integration.
"""

import threading
import time

import pytest
from chimera.core.bus import _is_locked_method
from chimera.core.metaobject import MethodWrapper
from chimera.instruments.camera import CameraBase

from chimera_player_one.instruments.playeronecamera import PlayerOneCamera
from chimera_player_one.sdk import simulator
from chimera_player_one.sdk.bindings import CameraSdk
from chimera_player_one.sdk.camera import Camera
from chimera_player_one.sdk.simulator import ARES_M_PRO, FakeCameraLibrary

#: Methods CameraBase marks @lock that this driver deliberately leaves unlocked.
#:
#: Every one of them is a *single* SDK call, and every SDK call is already
#: serialised by the per-library lock in bindings.py; the two-call cooling
#: sequence holds that lock as a transaction inside Camera.start_cooling. What
#: @lock would add is chimera's per-object monitor and FIFO lane, and `expose`
#: holds those for the entire _base_expose loop -- all frames plus intervals,
#: with no default request timeout on the bus. A status panel calling
#: get_temperature would freeze for minutes with no error.
#:
#: This set is the record of that decision. The test below exists so that the
#: *next* override anyone adds has to be a decision too: an override replaces the
#: base method and the base's @lock does not come with it, and nothing warns.
DELIBERATELY_UNLOCKED = frozenset(
    {
        "start_cooling",
        "stop_cooling",
        "get_temperature",
        "get_set_point",
        "start_fan",
        "stop_fan",
    }
)


def _locked_methods(cls):
    return {
        name
        for name, obj in vars(cls).items()
        if isinstance(obj, MethodWrapper) and _is_locked_method(obj)
    }


class TestLockMarkers:
    def test_expose_is_still_locked(self):
        """The one method that genuinely needs the object monitor. Two concurrent
        exposures on one camera are nonsense, and it is long enough that the FIFO
        lane is the right place for it."""
        assert _is_locked_method(PlayerOneCamera.expose)

    def test_the_demoted_methods_are_exactly_the_documented_ones(self):
        """@override does not carry @lock -- it sets an attribute and returns the
        same function, so the two compose fine in either order. What loses the
        marker is simply *overriding*: the subclass's method never had it.

        The marker has two consumers, the instance monitor and bus.py's
        per-object FIFO lane, so an unmarked override does not merely skip a lock
        -- it moves from "queued behind the exposure" onto the shared handler
        pool, i.e. from serialised to concurrent. Found in the wild in
        chimera-fli; chimera-qhy re-applies and is the other valid choice.
        """
        base_locked = _locked_methods(CameraBase)
        demoted = {
            name
            for name in base_locked
            if name in vars(PlayerOneCamera)
            and not _is_locked_method(getattr(PlayerOneCamera, name))
        }
        assert demoted == DELIBERATELY_UNLOCKED, (
            "an override changed which CameraBase @lock methods this driver "
            "demotes. That is allowed, but it must be deliberate: see the "
            "comment on DELIBERATELY_UNLOCKED and the one in the cooling section "
            "of playeronecamera.py."
        )


class TestTheLibraryLock:
    def test_only_one_thread_is_in_the_library_at_a_time(self):
        """Park a thread inside POAGetImageData and have another try to read the
        temperature. Before the lock existed the second walked straight in."""
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        geometry = cam.configure(binning=4)
        cam.begin_exposure(0.01)
        time.sleep(0.05)

        gate = threading.Event()
        lib.hold_in["POAGetImageData"] = gate
        reader = threading.Thread(target=cam.read_frame, args=(geometry,))
        reader.start()
        # Let the reader get inside the library and stop there.
        time.sleep(0.2)

        seen = []
        poller = threading.Thread(target=lambda: seen.append(cam.temperature))
        poller.start()
        time.sleep(0.2)

        gate.set()
        reader.join(timeout=10)
        poller.join(timeout=10)
        cam.close()

        assert seen, "the temperature read never completed"
        assert lib.max_concurrency == 1, (
            f"{lib.max_concurrency} threads were inside the library at once"
        )

    def test_a_busy_library_makes_read_if_idle_give_up_rather_than_wait(self):
        """What keeps a status panel responsive: it never queues behind a frame
        transfer, it just reports the value it already had."""
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        geometry = cam.configure(binning=4)
        cam.begin_exposure(0.01)
        time.sleep(0.05)

        gate = threading.Event()
        lib.hold_in["POAGetImageData"] = gate
        reader = threading.Thread(target=cam.read_frame, args=(geometry,))
        reader.start()
        time.sleep(0.2)

        t0 = time.monotonic()
        assert cam.read_if_idle("temperature", timeout=0.1) is None
        assert time.monotonic() - t0 < 1.0

        gate.set()
        reader.join(timeout=10)
        cam.close()

    def test_an_idle_library_answers_read_if_idle(self):
        lib = FakeCameraLibrary([ARES_M_PRO])
        cam = Camera.open(CameraSdk(lib), index=0)
        assert isinstance(cam.read_if_idle("temperature", timeout=0.5), float)
        cam.close()


class TestTheExposureWindow:
    """Which calls may land between arming and fetching, and which may not."""

    @pytest.fixture
    def driver(self, manager, monkeypatch, tmp_path):
        built = []

        class Recording(FakeCameraLibrary):
            def __init__(self, _ignored=None, **kwargs):
                super().__init__([ARES_M_PRO], **kwargs)
                built.append(self)

        monkeypatch.setattr(simulator, "FakeCameraLibrary", Recording)
        manager.add_class(
            PlayerOneCamera, "poa", {"simulated": True, "gain": 220, "offset": 35}
        )
        proxy = manager.get_proxy("/PlayerOneCamera/poa")
        return proxy, built[0], tmp_path

    @staticmethod
    def _run_control_loop_fast(camera, lib, timeout=15.0):
        """Speed the control loop up and wait until it has actually sped up.

        `set_hz` does not interrupt the sleep already in flight, so a test that
        sets it and exposes immediately races a wait of up to the old period --
        which is how this test first passed for the wrong reason.
        """
        camera.set_hz(50)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            before = lib.calls.count("POAGetConfig")
            time.sleep(0.3)
            if lib.calls.count("POAGetConfig") - before >= 2:
                return
        pytest.fail("the control loop never sped up")

    def test_the_temperature_sample_is_taken_before_arming(self, driver):
        """The driver's own per-frame CCD-TEMP read used to sit *inside* the
        armed window, on every frame including the ones that worked. It is
        astronomically identical a millisecond earlier, and it keeps our only
        config read out of the window where bulk transfers are pending.

        The control loop is slowed right down here so the only config reads in
        the trace are the driver's own -- what the *loop* is allowed to do during
        integration is the next test's business, not this one's.
        """
        camera, lib, tmp_path = driver
        camera.set_hz(0.005)  # 200 s: the control loop will not fire during this
        time.sleep(0.2)
        lib.calls.clear()
        camera.expose(
            {"exptime": 0.1, "binning": "4x4", "filename": str(tmp_path / "t")}
        )
        arm = lib.calls.index("POAStartExposure")
        fetch = lib.calls.index("POAGetImageData")
        assert "POAGetConfig" in lib.calls[:arm], "no temperature sample before arming"
        assert "POAGetConfig" not in lib.calls[arm:fetch], (
            "the driver is still reading config inside the armed window"
        )

    def test_nothing_else_touches_the_camera_during_the_transfer(self, driver):
        """The window INDIGO protects, and the one that matters: from the frame
        being ready to the transfer completing, this thread is alone."""
        camera, lib, tmp_path = driver
        self._run_control_loop_fast(camera, lib)
        lib.calls.clear()
        camera.expose(
            {"exptime": 0.5, "binning": "4x4", "filename": str(tmp_path / "u")}
        )
        assert lib.max_concurrency == 1

    def test_temperature_still_updates_while_the_sensor_integrates(self, driver):
        """The deliberate counterpart, and the one that keeps a widget alive. The
        control loop is *not* gated on is_exposing() -- only on whether the
        library happens to be busy right now. Fails if anyone re-adds that skip
        to "protect" the exposure."""
        camera, lib, tmp_path = driver
        self._run_control_loop_fast(camera, lib)
        lib.calls.clear()
        camera.expose(
            {"exptime": 1.0, "binning": "4x4", "filename": str(tmp_path / "v")}
        )
        arm = lib.calls.index("POAStartExposure")
        fetch = lib.calls.index("POAGetImageData")
        assert "POAGetConfig" in lib.calls[arm:fetch], (
            "no config read happened while the sensor was integrating, so the "
            "control loop is locked out of the whole exposure and a temperature "
            "widget would go blank for the length of a batch"
        )
