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


def _repo_root():
    return Path(chimera_player_one.__file__).resolve().parents[2]


#: Same config, two syntaxes, while chimera migrates from YAML to TOML.
EXAMPLE_CONFIGS = ["chimera.config", "chimera.toml"]


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_matches_the_class_names(filename):
    """A shipped example must name classes that actually load."""
    config = _repo_root() / filename
    if not config.exists():
        pytest.skip(f"no {filename} in this layout")
    text = config.read_text()
    for clsname in DRIVERS:
        assert clsname in text, f"{clsname} is not in {filename}"


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_parses(filename):
    """chimera picks its parser from the extension -- .config/.yaml/.yml are
    YAML, .toml is TOML -- so an example in either syntax must actually load.

    Both examples carry two cameras (the bench pair, whose per-sensor gain and
    offset differ) and one filter wheel.
    """
    from chimera.core.chimera_config import ChimeraConfig

    config = _repo_root() / filename
    if not config.exists():
        pytest.skip(f"no {filename} in this layout")
    parsed = ChimeraConfig.from_file(str(config))
    names = sorted("/" + str(url).split("/", 3)[3] for url in parsed.instruments)
    assert names == [
        "/PlayerOneCamera/ares",
        "/PlayerOneCamera/sedna",
        "/PlayerOneFilterWheel/phoenix",
    ]
    for url in parsed.instruments:
        assert any(d in str(url) for d in DRIVERS), f"{url} is not one of our drivers"


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_has_no_duplicate_top_level_keys(filename):
    """A duplicated section is silently accepted and the last one wins.

    This is not hypothetical: an edit to these files once left the whole camera
    block in twice. Both parsed, both agreed with each other, and every other
    test passed -- because the parsers keep the last duplicate and the two files
    were duplicated identically. Only reading the file showed it. Comparing
    parsed output cannot catch this, so compare the text.
    """
    config = _repo_root() / filename
    if not config.exists():
        pytest.skip(f"no {filename} in this layout")
    keys = []
    for line in config.read_text().splitlines():
        if filename.endswith(".toml"):
            if line.startswith("[") and not line.startswith("[["):
                keys.append(line.strip())
        elif line and not line[0].isspace() and line.rstrip().endswith(":"):
            keys.append(line.strip())
    assert len(keys) == len(set(keys)), f"duplicated section(s) in {filename}: {keys}"


def test_the_two_examples_do_not_drift():
    """Two examples that disagree are worse than one.

    A reader has no way to tell which is authoritative, and the difference will
    be in whichever one they are not looking at. Compare the parsed *result*,
    not the text: that is the only comparison that survives the two syntaxes
    legitimately differing.
    """
    from chimera.core.chimera_config import ChimeraConfig

    root = _repo_root()
    if not all((root / f).exists() for f in EXAMPLE_CONFIGS):
        pytest.skip("both examples are needed for this comparison")

    def summarise(filename):
        parsed = ChimeraConfig.from_file(str(root / filename))
        return (
            parsed.host,
            parsed.port,
            {str(u): dict(v) for u, v in parsed.sites.items()},
            {str(u): dict(v) for u, v in parsed.instruments.items()},
        )

    yaml_config, toml_config = (summarise(f) for f in EXAMPLE_CONFIGS)
    assert yaml_config == toml_config, "chimera.config and chimera.toml have drifted"
