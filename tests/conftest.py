# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Shared fixtures, taken from chimera-zwo's, which took them from chimera's.

The drivers are exercised through a real Manager and Bus rather than constructed
directly, because events are half of what a camera does: a caller learns an
exposure finished from `expose_complete`, and a bare object has no bus to deliver
it on.

`has_hardware` gates the handful of tests that want a real camera. Everything
else runs against the fake library and must stay that way -- CI has no camera,
and a suite that quietly skips its own subject is worse than no suite.
"""

import random
import threading
import time

import pytest


@pytest.fixture
def wait_for():
    """Poll a predicate until true or timed out. Use instead of fixed sleeps."""

    def waiter(predicate, timeout=10.0, interval=0.05):
        t0 = time.monotonic()
        while not predicate() and time.monotonic() - t0 < timeout:
            time.sleep(interval)
        return predicate()

    return waiter


@pytest.fixture
def manager():
    from chimera.core.bus import Bus
    from chimera.core.manager import Manager
    from chimera.core.site import Site

    bus = Bus(f"tcp://127.0.0.1:{random.randint(20000, 60000)}")
    bus_thread = threading.Thread(
        target=bus.run_forever, name="test-manager-bus", daemon=True
    )
    bus_thread.start()
    assert bus._bus_started.wait(5)

    site = Site()
    for k, v in {
        "name": "lna",
        "latitude": "-27 36 13",
        "longitude": "-48 31 20",
        "altitude": "20",
    }.items():
        site[k] = v

    manager = Manager(bus, site=site)
    yield manager
    manager.shutdown()
    bus.shutdown()
    bus_thread.join(timeout=10)


@pytest.fixture(scope="session")
def has_hardware():
    """True when a real Player One camera is attached to this machine."""
    try:
        from chimera_player_one.sdk import loader

        lib = loader.camera_library()
        return lib.POAGetCameraCount() > 0
    except Exception:
        return False
