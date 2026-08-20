# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Can chimera actually find and load these drivers?

Every other test in this suite imports the driver by its Python path, which
proves nothing about the way chimera loads it. chimera does **not** use entry
points: `findplugins.py` scans for importable top-level packages named
`chimera_*` and adds their `instruments/` directory to a search path, and
`classloader.py` then does `__import__(clsname.lower())`.

So the module filename has to be the class name lowercased, and an underscore
anywhere in it breaks loading -- silently, because nothing tries until a
`chimera.config` names the class. `chimera-zwo` has this latent today: its
`ZWOAM5` lives in `zwo_am5.py`, and it ships no `instruments/` directory and no
example config, so the path has never been exercised there.

These tests are cheap and they fail loudly on the day someone renames a file.
"""

from pathlib import Path

import pytest
from chimera.core.classloader import ClassLoader
from chimera.util.findplugins import find_chimera_plugins

import chimera_player_one

DRIVERS = ["PlayerOneCamera", "PlayerOneFilterWheel"]


def test_package_is_discovered_as_a_plugin():
    """The package name must start with `chimera_` or nothing finds it."""
    _controllers, instruments = find_chimera_plugins()
    ours = Path(chimera_player_one.__file__).resolve().parent / "instruments"
    assert str(ours) in [str(Path(p).resolve()) for p in instruments]


@pytest.mark.parametrize("clsname", DRIVERS)
def test_module_filename_is_the_lowercased_class_name(clsname):
    """The rule, checked directly, so the reason survives a rename."""
    instruments = Path(chimera_player_one.__file__).resolve().parent / "instruments"
    assert (instruments / f"{clsname.lower()}.py").exists(), (
        f"{clsname} must live in {clsname.lower()}.py -- classloader imports the "
        f"lowercased class name, and underscores break it"
    )


@pytest.mark.parametrize("clsname", DRIVERS)
def test_classloader_can_load_the_driver(clsname):
    """The real thing: chimera's own loader, over the real search path."""
    _controllers, instruments = find_chimera_plugins()
    loaded = ClassLoader().load_class(clsname, instruments)
    assert loaded.__name__ == clsname


def test_example_config_matches_the_class_names():
    """The shipped chimera.config must name classes that actually load."""
    config = Path(chimera_player_one.__file__).resolve().parents[2] / "chimera.config"
    if not config.exists():
        pytest.skip("no example config in this layout")
    text = config.read_text()
    for clsname in DRIVERS:
        assert clsname in text, f"{clsname} is not in the example config"
