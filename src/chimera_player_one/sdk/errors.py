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
