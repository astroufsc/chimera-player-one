
The binding is ``ctypes``, so there is nothing to build on any of them.

Configuration
-------------

Two runnable examples ship at the repo root, with identical content:
``chimera.config`` (YAML) and ``chimera.toml`` (TOML). chimera picks its parser
from the extension — ``.config``, ``.yaml`` and ``.yml`` are YAML, ``.toml`` is
TOML — and looks for ``~/.chimera/chimera.config`` by default. Both are kept
while chimera migrates from YAML to TOML; a test parses both and fails if they
drift.

Each carries **both bench cameras**, because the per-sensor values are not
interchangeable and the contrast is the useful part:

===============  ================  ==============
                 Ares-M PRO        Sedna-M
===============  ================  ==============
sensor           IMX533 3008²      IMX178 3096×2078
pixel            3.76 µm           2.40 µm
cooler           yes               **no**
gain range       0–600             0–510
**unity gain**   **130**           **0**
default offset   35                50
===============  ================  ==============

A Sedna-M is already 0.92 e⁻/ADU at minimum gain, so copying the Ares's
``gain: 220`` onto it only costs dynamic range. Read your own with
``chimera-player-one-doctor`` or ``poa_probe.py --stage config``.

``serial`` is optional: selection is serial → model → ``camera_index``, and with
none of them set you get the first camera the SDK enumerates. Set it once there
is more than one device — USB enumeration order does not survive a re-plug.

Linux: udev rules
-----------------

A wheel cannot install udev rules, and without them the camera enumerates but
every open fails with ``POA_ERROR_ACCESS_DENIED``. Once, as root::

    uv run chimera-player-one-doctor --install-udev

The driver detects that specific failure and says so, rather than surfacing the
raw SDK error.

Checking an installation
------------------------

::

    uv run chimera-player-one-doctor

Reports which library was loaded and from where, whether ``libusb`` resolved
and to which copy, the udev rule status on Linux, and the cameras and wheels it
can enumerate.

Testing
-------

::

    uv run pytest

No hardware required, and no hardware is mocked away either. The fake is a
stand-in for the **shared library**, at the ``ctypes.CDLL`` boundary — it
answers with the same out-parameters and the same ``POAErrors`` codes as the
real one. So the struct packing, the ``byref`` marshalling, the error mapping
and the 16-bit buffer handling are all exercised by the suite; only the vendor
blob is absent. Mocking a layer higher would skip exactly the code most likely
to be wrong.

It also injects faults real hardware will not perform on request: a timeout
mid-exposure, a disconnect between frames, a cooler that never reaches
setpoint, a short read.

Hardware bring-up
-----------------

``scripts/poa_probe.py`` is a staged ladder — discover, configure, cool,
expose — where the first failing stage names the problem::

    uv run scripts/poa_probe.py --stage discover

Releasing
---------

Tagging is the release; nothing else publishes::

    git tag -a v0.2.0 -m "what changed"
    git push origin v0.2.0

That runs the full CI suite, checks the tag against ``pyproject.toml``'s
version, and attaches the sdist and wheel to a GitHub release. PyPI upload is
wired up via trusted publishing and switched on with the repository variable
``PUBLISH_TO_PYPI``; until it is set, a tag still produces a downloadable
release.

Pull requests and pushes to ``master`` run lint, type checking and the test
matrix (Linux x86-64 and arm64, macOS, Windows) and build the wheel, but never
publish.

Provenance
----------

The vendor libraries under ``src/chimera_player_one/_sdk/`` are redistributed
under the Player One SDK licence, which permits it and requires the notice to
travel with them; see ``_sdk/LICENSE-PlayerOne.txt``. Every file records its
SDK version, source archive and SHA-256 in ``_sdk/provenance.toml``, checked in
CI so a silent SDK swap cannot pass.
