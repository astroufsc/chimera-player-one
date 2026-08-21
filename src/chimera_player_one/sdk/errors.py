# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Turning SDK return codes into exceptions, which the vendor's wrapper does not.

Every Player One function returns a code and the vendor binding hands it back for
the caller to check. Nothing enforces that, and the failure mode is quiet: skip
one check and you get a camera with an empty model name rather than an error.
So the rule at this boundary is that a non-``POA_OK`` return raises, and callers
opt out explicitly where a failure is genuinely expected.
"""

from __future__ import annotations

from .enums import POAErrors, PWErrors

__all__ = ["POAError", "PWError", "PlayerOneError"]

#: Codes where the SDK is certain the handle is no longer usable. 5 is what a
#: call on a handle whose device went away returns; 6 is what a rescan says
#: afterwards. Note what is **not** here: see `POAError.is_operation_failed`.
_GONE = frozenset(
    {POAErrors.POA_ERROR_NOT_OPENED, POAErrors.POA_ERROR_DEVICE_NOT_FOUND}
)

#: Failures about the *link* to the camera rather than about what was asked of
#: it. Recovery acts on this set and no other: re-opening a camera cannot fix an
#: out-of-range window or an unknown image format, and trying turns one bad
#: config into a reconnect storm.
_TRANSPORT = _GONE | {
    POAErrors.POA_ERROR_TIMEOUT,
    POAErrors.POA_ERROR_OPERATION_FAILED,
}


class PlayerOneError(RuntimeError):
    """Base for both SDKs, so a caller can catch either with one except."""


class POAError(PlayerOneError):
    """A camera SDK call failed.

    ``code`` is the raw integer even when it is not a known ``POAErrors`` member,
    because an SDK newer than this binding may return one we have never seen and
    losing it would be worse than not naming it.
    """

    def __init__(self, function: str, code: int, detail: str = "") -> None:
        self.function = function
        self.code = code
        try:
            self.error = POAErrors(code)
            name = self.error.name
        except ValueError:
            self.error = None
            name = f"unknown code {code}"
        message = f"{function} failed: {name}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)

    @property
    def is_access_denied(self) -> bool:
        """True for the Linux 'you forgot the udev rules' failure.

        Worth a named property because it is the single most common first-run
        problem on Linux and it deserves an explanation rather than a raw code.
        """
        return self.error is POAErrors.POA_ERROR_ACCESS_DENIED

    @property
    def is_timeout(self) -> bool:
        """True for POA_ERROR_TIMEOUT, whoever raised it.

        `Camera.wait_for_image` synthesises this code for its own deadline, so a
        caller that needs to tell "our poll gave up" from "the SDK timed out"
        must check for `ExposureTimeoutError`, not for this.
        """
        return self.error is POAErrors.POA_ERROR_TIMEOUT

    @property
    def is_operation_failed(self) -> bool:
        """True for POA_ERROR_OPERATION_FAILED. Named after the code on purpose.

        The code's *meaning* is not knowable from the code. The vendor header
        attaches it both to "maybe the camera is disconnected suddenly"
        (POASetSensorMode) and to "the current mode is not matched"
        (POAGetSensorMode) -- and POAGetImageData documents no timeout return at
        all, so a transfer that ran out of time surfaces here too.

        What disambiguates it is whether EP0 still answers. On 2026-08-20 this
        code came out of POAGetImageData while POAGetConfig kept working
        perfectly on the same handle: the camera was present and only the bulk
        image endpoint was dead. So it must not be folded into
        `is_disconnected`, however much the header's first gloss invites it.
        """
        return self.error is POAErrors.POA_ERROR_OPERATION_FAILED

    @property
    def is_disconnected(self) -> bool:
        """True only where the SDK is certain the handle is gone. See `_GONE`."""
        return self.error in _GONE

    @property
    def is_transport(self) -> bool:
        """True for failures of the link rather than of the request. See `_TRANSPORT`.

        This is the predicate recovery keys on. It deliberately includes
        POA_ERROR_OPERATION_FAILED even though that code is ambiguous: when the
        image endpoint has stopped delivering, reconnecting is the only move we
        have, and the cost of being wrong is one needless re-open.
        """
        return self.error in _TRANSPORT


class PWError(PlayerOneError):
    """A filter wheel SDK call failed."""

    def __init__(self, function: str, code: int, detail: str = "") -> None:
        self.function = function
        self.code = code
        try:
            self.error = PWErrors(code)
            name = self.error.name
        except ValueError:
            self.error = None
            name = f"unknown code {code}"
        message = f"{function} failed: {name}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
