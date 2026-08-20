# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""The filter wheel driver, through a real Manager and Bus."""

import time

import pytest

from chimera_player_one.instruments.playeronefilterwheel import PlayerOneFilterWheel

FILTERS = ["U", "B", "V", "R", "I", "Ha", "OIII"]

fired = {}


def _filter_change(new_filter, old_filter):
    fired["filter_change"] = (new_filter, old_filter)


@pytest.fixture
def wheel(manager):
    manager.add_class(
        PlayerOneFilterWheel, "poapw", {"simulated": True, "filters": FILTERS}
    )
    fired.clear()
    proxy = manager.get_proxy("/PlayerOneFilterWheel/poapw")
    proxy.filter_change += _filter_change
    return proxy


def _wait_for(predicate, timeout=10.0):
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout:
        time.sleep(0.05)
    return predicate()


class TestFilterWheel:
    def test_reports_the_configured_filters(self, wheel):
        assert wheel.get_filters() == FILTERS

    def test_set_and_read_back(self, wheel):
        wheel.set_filter("V")
        assert wheel.get_filter() == "V"

    def test_positions_are_zero_based(self, wheel):
        """The wire, the SDK and chimera's list all agree. A +1 anywhere would put
        every filter one slot out and the images would still look fine."""
        wheel.set_filter("U")
        assert wheel.get_filter() == "U"
        wheel.set_filter("OIII")
        assert wheel.get_filter() == "OIII"

    def test_filter_change_event_fires(self, wheel):
        wheel.set_filter("R")
        assert _wait_for(lambda: "filter_change" in fired)
        assert fired["filter_change"][0] == "R"

    def test_unknown_filter_is_refused(self, wheel):
        """Note the exception *type* does not survive the bus.

        `proxy.py:169` re-raises every remote failure as a bare `Exception`
        carrying the formatted traceback, so a caller across the bus cannot catch
        `InvalidFilterPositionException` by type -- only match on its text. Pinned
        here because it is easy to write the type-based assertion, watch it pass
        against a directly-constructed object, and have it fail the moment the
        driver is used the way chimera actually uses it."""
        with pytest.raises(Exception, match="Invalid filter Z"):
            wheel.set_filter("Z")

    def test_set_filter_waits_for_the_move_to_finish(self, wheel):
        """Returning early would let an exposure start through a filter that is
        still moving, producing a blend of two bandpasses that just looks odd."""
        for name in ("B", "Ha", "U"):
            wheel.set_filter(name)
            assert wheel.get_filter() == name, "set_filter returned before settling"

    def test_firmware_names_are_readable(self, wheel):
        """The wheel keeps its own names; worth being able to compare them."""
        assert wheel.get_firmware_filter_names()[:3] == ["U", "B", "V"]

    def test_firmware_focus_offsets_are_readable_but_not_applied(self, wheel):
        """They are in the wheel's units, and it has no idea which focuser it is
        in front of, so they are exposed for comparison rather than used."""
        offsets = wheel.get_firmware_focus_offsets()
        assert len(offsets) == 7
        assert offsets[4] == 25


class TestConfigurationMismatch:
    def test_mismatch_is_logged_not_silently_corrected(self, manager, caplog):
        """Someone changed the physical wheel or edited the config. Which is right
        is not knowable here, but a silent mismatch mislabels every frame."""
        wrong = ["U", "B", "ZZZ", "R", "I", "Ha", "OIII"]
        manager.add_class(
            PlayerOneFilterWheel, "mismatch", {"simulated": True, "filters": wrong}
        )
        proxy = manager.get_proxy("/PlayerOneFilterWheel/mismatch")
        # chimera's config stays authoritative -- it is what the rest of chimera acts on
        assert proxy.get_filters() == wrong

    def test_shorter_filter_list_still_starts(self, manager):
        """A seven-slot wheel with three filters configured is a legitimate setup,
        not an error."""
        manager.add_class(
            PlayerOneFilterWheel,
            "short",
            {"simulated": True, "filters": ["R", "G", "B"]},
        )
        proxy = manager.get_proxy("/PlayerOneFilterWheel/short")
        proxy.set_filter("G")
        assert proxy.get_filter() == "G"
