# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""A Player One filter wheel as a chimera instrument.

Same filename rule as the camera: ``PlayerOneFilterWheel`` must live in
``playeronefilterwheel.py``, because ``classloader`` imports the lowercased class
name.

``FilterWheelBase`` already owns the parts worth not reimplementing -- validating
the requested filter against the configured list, applying the focuser offset and
firing ``filter_change``. A driver supplies ``_set_filter`` and ``get_filter``.

Two things are specific to this wheel and worth knowing before reading the code:

**Positions are 0-based on the wire, in the SDK, and in chimera's filter list.**
All three agree, so there is no ``+1`` anywhere. Adding one "to match the
labels on the wheel" would put every filter one slot out, and the images would
still look fine.

**The wheel stores its own filter names and focus offsets in firmware.** Those
are the observatory's real configuration as far as anyone standing at the
telescope is concerned, and chimera has its own copy in ``filters`` and
``focus_offsets``. When they disagree, one of them is wrong and nobody finds out
from the data. This driver does not silently prefer either: it reads the wheel's
copy at startup and **logs the disagreement**, leaving chimera's config
authoritative because that is what the rest of chimera acts on.
"""

from __future__ import annotations

import time
from typing import override

from chimera.instruments.filterwheel import FilterWheelBase

from chimera_player_one.sdk.bindings import FilterWheelSdk
from chimera_player_one.sdk.enums import PWState
from chimera_player_one.sdk.errors import PWError
from chimera_player_one.sdk.structs import PWProperties

__all__ = ["PlayerOneFilterWheel"]

_SETTLE_POLL = 0.05


class PlayerOneFilterWheel(FilterWheelBase):
    """Player One filter wheel, over the vendor SDK bundled with this package."""

    __config__ = {
        #: Serial number, the identifier that survives a re-plug. Wins over
        #: `wheel_index` when set; read it from `doctor`.
        "serial": None,
        #: USB enumeration index, used when `serial` is unset.
        "wheel_index": 0,
        #: Seconds to wait for a move to complete before giving up. A seven-slot
        #: wheel crosses at most half the wheel, so this is generous.
        "move_timeout": 30.0,
        #: Always approach a position from the same direction, trading time for
        #: repeatability. Worth it if the wheel's detents have backlash.
        "one_way": False,
        #: Run against the in-process fake library instead of hardware.
        "simulated": False,
    }

    def __init__(self) -> None:
        FilterWheelBase.__init__(self)
        self._sdk: FilterWheelSdk | None = None
        self._properties: PWProperties | None = None
        self._handle: int | None = None

    # -- lifecycle --------------------------------------------------------

    @override
    def __start__(self) -> bool:
        # FilterWheelBase.__start__ validates the focus-offset table; skipping it
        # would defer a config error to the first filter change, at night.
        FilterWheelBase.__start__(self)

        self._sdk = self._make_sdk()
        wheels = self._sdk.enumerate()
        if not wheels:
            raise PWError("POAGetPWCount", 5, "no Player One filter wheels found")
        self._properties = self._select(wheels)
        self._handle = self._properties.Handle
        self._sdk.open(self._handle)

        if self["one_way"]:
            self._sdk.set_one_way(self._handle, True)

        self._check_configuration_against_firmware()
        self.log.info(
            "%s (%s) ready, %d positions, currently at %s",
            self._properties.name,
            self._properties.serial,
            self._properties.PositionCount,
            self.get_filter(),
        )
        return True

    @override
    def __stop__(self) -> bool:
        if self._sdk is not None and self._handle is not None:
            try:
                self._sdk.close(self._handle)
            except PWError:
                self.log.debug("closing the filter wheel failed", exc_info=True)
        self._sdk, self._handle, self._properties = None, None, None
        return True

    def _make_sdk(self) -> FilterWheelSdk:
        if self["simulated"]:
            from chimera_player_one.sdk.simulator import FakeFilterWheelLibrary

            self.log.warning("running against the SIMULATED filter wheel library")
            return FilterWheelSdk(FakeFilterWheelLibrary())
        return FilterWheelSdk()

    def _select(self, wheels: list[PWProperties]) -> PWProperties:
        serial = self["serial"]
        if serial:
            for wheel in wheels:
                if wheel.serial == serial:
                    return wheel
            available = ", ".join(f"{w.name} ({w.serial})" for w in wheels)
            raise PWError(
                "POAOpenPW", 5, f"no wheel with serial {serial!r}; have: {available}"
            )
        index = int(self["wheel_index"])
        if index >= len(wheels):
            raise PWError(
                "POAOpenPW", 1, f"wheel index {index} of {len(wheels)} attached"
            )
        return wheels[index]

    def _check_configuration_against_firmware(self) -> None:
        """Compare chimera's filter list with the names the wheel itself holds.

        Not corrected automatically in either direction. A mismatch means someone
        changed the physical wheel or edited the config, and which one is right is
        not knowable from here -- but a silent mismatch means every frame is
        labelled with the wrong filter and the images look perfect.
        """
        configured = self.get_filters()
        if not configured:
            self.log.warning(
                "no `filters` configured; chimera cannot name a position and "
                "set_filter will refuse every request"
            )
            return
        if len(configured) != self._positions():
            self.log.warning(
                "configured %d filters but the wheel has %d positions",
                len(configured),
                self._positions(),
            )
        for position, name in enumerate(configured[: self._positions()]):
            try:
                stored = self._require_sdk().get_filter_alias(
                    self._require_handle(), position
                )
            except PWError:
                self.log.debug(
                    "wheel has no alias for position %d", position, exc_info=True
                )
                continue
            if stored and stored != name:
                self.log.warning(
                    "position %d: chimera says %r, the wheel says %r -- chimera's "
                    "config wins, but one of them is wrong",
                    position,
                    name,
                    stored,
                )

    # -- the driver's two jobs --------------------------------------------

    @override
    def _set_filter(self, filter_name: str) -> None:
        """Move to the named filter and wait until it has actually arrived.

        Returning before the wheel settles would let an exposure start through a
        filter that is still moving, which produces a frame that is a blend of two
        bandpasses and looks merely odd.
        """
        sdk, handle = self._require_sdk(), self._require_handle()
        position = self.get_filters().index(filter_name)
        sdk.goto_position(handle, position)
        self._wait_until_settled(position)

    @override
    def get_filter(self) -> str:
        """The current filter name, or ``"MOVING"`` while between detents."""
        sdk, handle = self._require_sdk(), self._require_handle()
        position = sdk.get_position(handle)
        if position < 0:
            return "MOVING"
        filters = self.get_filters()
        if position >= len(filters):
            # The wheel is somewhere chimera has no name for. Say so rather than
            # raising: this is reachable just by shortening `filters` in a config.
            return f"UNKNOWN({position})"
        return filters[position]

    def _wait_until_settled(self, position: int) -> None:
        sdk, handle = self._require_sdk(), self._require_handle()
        deadline = time.monotonic() + float(self["move_timeout"])
        while time.monotonic() < deadline:
            if sdk.get_state(handle) is not PWState.PW_STATE_MOVING:
                arrived = sdk.get_position(handle)
                if arrived == position:
                    return
                raise PWError(
                    "POAGotoPosition",
                    8,
                    f"wheel stopped at position {arrived}, not {position}",
                )
            time.sleep(_SETTLE_POLL)
        raise PWError(
            "POAGotoPosition",
            8,
            f"wheel did not reach position {position} within {self['move_timeout']} s",
        )

    def _positions(self) -> int:
        return 0 if self._properties is None else self._properties.PositionCount

    def _require_sdk(self) -> FilterWheelSdk:
        if self._sdk is None:
            raise PWError(
                "POAOpenPW", 4, "filter wheel is not open; was __start__ run?"
            )
        return self._sdk

    def _require_handle(self) -> int:
        if self._handle is None:
            raise PWError(
                "POAOpenPW", 4, "filter wheel is not open; was __start__ run?"
            )
        return self._handle

    # -- extras worth exposing --------------------------------------------

    def get_firmware_filter_names(self) -> list[str]:
        """The names the wheel stores for itself, position by position."""
        sdk, handle = self._require_sdk(), self._require_handle()
        return [sdk.get_filter_alias(handle, p) for p in range(self._positions())]

    def get_firmware_focus_offsets(self) -> list[int]:
        """The focus offsets the wheel stores, in the wheel's own units.

        Deliberately *not* fed into chimera's `focus_offsets`: those are in the
        focuser's units and this wheel has no idea which focuser it is in front
        of. Exposed so a human can compare the two and decide.
        """
        sdk, handle = self._require_sdk(), self._require_handle()
        return [sdk.get_focus_offset(handle, p) for p in range(self._positions())]
