# chimera-player-one — build log

> Written as the work happens, not afterwards. Every entry says what was done, what was
> decided and what it cost, and is tagged **[any vendor]** or **[Player One]** — because this
> repo is the source material for the first skill in `chimera-plugin`, the standard one for
> *vendor C SDK → low-level binding → high-level `ChimeraObject`*, and the split between what
> generalises and what does not cannot be reconstructed later.
>
> Traps lead with the **symptom**, because that is what a reader greps for.

---

## 2026-08-20 — 0. Where this came from

`kepler`'s plan 5 is blocked on a camera. `docs/notes/HANDOFF-hardware.md` §0: the AM5 mount
driver is done and measured against real hardware, stage 1 of the pointing ladder runs end to
end against a simulator, and **task 6 — a real pointing run on real sky — needs a camera
chimera can drive.** Behind it sits stage 3(a), the two-hour periodic-error trace that decides
whether the project's premise holds.

The route is the **vendor SDK**, not the direct-USB reverse engineering in
`chimera-player-one-research/camera.py`. The user's own assessment of that experiment: it
*"was unstable and might need some work"*. The SDK is supported, ships for every platform we
care about, and `tests_sdk/` is already a bring-up ladder against it.

## 2026-08-20 — 1. Scaffold

`uv init --lib`, then the `chimera-zwo` house style: `uv_build` backend, `requires-python
>=3.13`, `chimera = { path = "../chimera", editable = true }`, ruff `select = ["N","I","UP","F"]`
at line-length 88, the SPDX `notice-rgx`, the same dev group and pytest `addopts`.
`uv sync --all-extras --dev` resolves.

**Trap — a template `.gitignore` can silently delete the deliverable. [any vendor]**
*Symptom:* vendored `.so` files are missing from the wheel, and `git status` shows nothing
wrong. `chimera-zwo`'s `.gitignore` is a pre-`src/` classic that excludes `*.so`, `lib`, `bin`,
`dist` and `build`. Copied as-is into a package whose entire purpose is shipping
`_sdk/linux/*/libPlayerOneCamera.so.3`, it would drop them at `git add` with no error. Wrote a
modern one instead, with a comment at the top saying why it is not the inherited file and that
additions must be checked against `_sdk/` first.

**Trap — the house CI cannot have run. [any vendor]**
*Symptom:* `uv sync --locked` on a clean runner fails to resolve `chimera`.
`chimera-zwo/.github/workflows/ci.yml` checks out one repo and then syncs a `pyproject.toml`
whose `chimera` is `{ path = "../chimera" }`. There is no sibling checkout on a runner, so that
resolution cannot succeed — chimera's own `pyproject.toml` documents the same hazard for its
`chz1` dependency in prose. Fixed here by checking out **both** repos as siblings *inside* the
workspace and setting `defaults.run.working-directory`; `actions/checkout` refuses a `path:`
outside the workspace, so the obvious `path: ../chimera` does not work either.

## 2026-08-20 — 2. Decisions taken before any code

| # | decision | why |
|---|---|---|
| D1 | one distribution, not a low-level wheel plus a driver wheel | `chimera-qhy` already layers this way inside one package; one version to bump, one CI |
| D2 | vendor blobs **committed**, with `provenance.toml` + licence | the Player One licence permits redistribution; a fresh clone must build offline |
| D3 | one `py3-none-any` wheel carrying **every** platform | see the trap below — `uv_build` cannot do anything else |
| D4 | camera **and** filter wheel, newest SDKs (3.10.1 / 1.2.3) | user scope |
| D5 | simulate at the `ctypes.CDLL` seam | see entry 5 |
| D6 | **no `*_LIBRARY_PATH` work by users, ever** | user requirement; drives the whole loader design |

**Trap — `uv_build` cannot emit a platform-tagged wheel. [any vendor]**
*Symptom:* you ship `.dylib`/`.so`/`.dll` in a wheel tagged `py3-none-any`, and a Linux
installer happily serves the macOS build. Confirmed three ways: every `uv_build`-produced
`WHEEL` on this machine reads `Root-Is-Purelib: true` / `Tag: py3-none-any` unconditionally;
the backend's settings struct exposes only `module-root`, `module-name`, `source-include`,
`source-exclude`, `wheel-exclude`, `namespace`, `data` — no `tag`, no `plat-name`; and `uv
build` has no tag flag and no build-hook API. Renaming the file afterwards leaves `WHEEL` and
`RECORD` inconsistent.

So the choice is binary: **one fat wheel carrying every platform** (keeps `uv_build`, one
artifact, ~8 MB to everyone), or **hatchling plus a `hatch_build.py` hook** setting
`build_data["tag"]` with a CI matrix. Took the fat wheel — it is what was asked for, and a
`ctypes` binding is ABI-independent so there is no platform×Python fan-out to justify the
machinery. `cibuildwheel` solves a problem this package does not have. `chimera-qhy` is
already on hatchling, so the fallback is not a novel deviation if this is ever published to
PyPI.

## 2026-08-20 — 3. libusb, and why vendoring it turned out to be mandatory

The camera library links `@rpath/libusb-1.0.0.dylib` (macOS) / `NEEDED libusb-1.0.so.0` (Linux).
Windows needs nothing — SetupAPI/WinUSB, built `/MT`. The **filter wheel** library needs nothing
anywhere; it is HID, against IOKit/CoreFoundation. So this is a camera-only, Unix-only problem.

The plan allowed a cheaper fallback: rely on a system libusb and just diagnose it well. **A
measurement killed that option.**

**Trap — `@rpath` in a vendor library resolves against *the host interpreter*. [any vendor]**
*Symptom:* the library loads fine when you test it, and fails for the user, with the same
libusb installed.
The vendor dylib's only `LC_RPATH` is `@executable_path/../Frameworks`. dyld resolves `@rpath`
using the rpaths of the loading image **and of the main executable**, so the result depends on
which Python is running:

```
system python3 (homebrew)   -> loads; binds /opt/homebrew/Cellar/libusb/1.0.30/lib/libusb-1.0.0.dylib
uv-managed python 3.13.14   -> OSError: Library not loaded: @rpath/libusb-1.0.0.dylib
                               tried .../uv/python/cpython-3.13.14-.../Frameworks, .../lib,
                                     /usr/local/lib, /usr/lib  -- and stopped
```

Homebrew's `/opt/homebrew/lib` is **not** on dyld's fallback path, so a uv-managed interpreter —
which is what a chimera user runs — never finds it. `brew install libusb` does not fix this.
Only patching the library, or shipping libusb beside it, does.

So, decided and implemented:

- **Vendor a universal libusb for macOS.** Built from the 1.0.30 release tarball with
  `CFLAGS="-arch arm64 -arch x86_64 -mmacosx-version-min=11.0"`; the result is a fat
  `x86_64 + arm64` dylib depending only on `libobjc`, `IOKit`, `CoreFoundation`, `libSystem`.
  Its install name is rewritten to `@rpath/libusb-1.0.0.dylib`.
- **Patch our copy of the camera dylib** with `install_name_tool -add_rpath`, in order:
  `@loader_path` (the vendored copy wins), then `/opt/homebrew/lib`, `/usr/local/lib`,
  `/opt/local/lib` (so a system copy still works if someone deletes ours). The dependency name
  is *not* rewritten, so the library stays swappable.
- **Linux needs no patching at all.** The loader preloads libusb by absolute path with
  `RTLD_GLOBAL` before loading the camera library; the dynamic linker satisfies a `NEEDED`
  soname from an already-loaded object, so `libusb-1.0.so.0` binds without `patchelf`,
  `$ORIGIN` or `LD_LIBRARY_PATH`. That removed a build-time dependency on `patchelf`, which is
  not installed here anyway.

Verified: patched + vendored loads under the uv interpreter that rejects the pristine one,
reports `SDK 3.10.1 / API 20260430`, and enumerates hardware.

## 2026-08-20 — 4. Two SDK call-contract traps, both found by accident

Both cost a confusing five minutes and both would have shipped.

**Trap — `POAGetCameraCount()` is the bus scan, not an accessor. [Player One]**
*Symptom:* a camera enumerates with an empty model name, no bins and no image formats — which
reads as a broken camera or a bad cable, not a bug in your code.
`POAGetCameraProperties(0, &p)` **before** any `POAGetCameraCount()` returns
`POA_ERROR_INVALID_INDEX` and leaves the struct zeroed. Code that ignores the return value —
and the vendor wrapper returns codes rather than raising, so ignoring them is easy — sees a
plausible, empty camera. Measured:

```
without POAGetCameraCount first :  err=1 "invalid index", name=b'', bins=[0]*8
with    POAGetCameraCount first :  err=0, name='Ares-M PRO', bins=[1,2,3,4,0,0,0,0]
```

The header's own flow chart puts `POAGetCameraCount` first; it just does not say it is
load-bearing. **Our layer never exposes an enumeration that has not scanned first.**

**Trap — sentinel-terminated arrays must be truncated, never filtered. [any vendor]**
*Symptom:* a mono camera reports seven image formats, five of them phantom `RAW8`.
`POACameraProperties.imgFormats` is `int[8]` terminated by `POA_END = -1`, with the tail left as
zeros — and `0` is `POA_RAW8`, a perfectly valid member. So the natural-looking
`[f for f in imgFormats if f != -1]` yields `[RAW8, RAW16, RAW8, RAW8, RAW8, RAW8, RAW8]`.
Measured on the Ares-M PRO:

```
raw       : [0, 1, -1, 0, 0, 0, 0, 0]
filtered  : [0, 1, 0, 0, 0, 0, 0]      <- wrong, and looks fine
truncated : [0, 1]                      <- RAW8, RAW16
```

`bins` has the same shape with `0` as its terminator (`[1,2,3,4,0,0,0,0]`), where filtering
falsy values happens to give the right answer — which is worse, because it teaches the wrong
rule. Both are decoded by **truncation at the sentinel**, and a test pins the exact raw arrays
above.

## 2026-08-20 — 5. The hardware is on the desk

Both cameras enumerate, so step 10 is not hypothetical:

| | Ares-M PRO | Sedna-M |
|---|---|---|
| sensor | IMX533 mono | IMX178 mono |
| frame | 3008 x 3008, 14-bit | 3096 x 2078, 14-bit |
| pixel | 3.76 um | 2.40 um |
| cooler | **yes** | no |
| ST4 | no | yes |
| bins / formats | 1,2,3,4 / RAW8, RAW16 | 1,2,3,4 / RAW8, RAW16 |
| pID | `0x5335` | `0x1783` |

`sizeof(POACameraProperties)` measures **992**, matching a field-by-field reading of the 3.10.1
header — so the ctypes struct is right before a line of driver code exists. Worth pinning as a
test: a struct that is silently the wrong size reads plausible garbage.

Note `isUSB3Speed` is **false** for the Ares-M PRO as currently plugged, so readout will be
USB2-slow until it moves to a USB3 port. Not a bug; worth knowing before timing anything.

## 2026-08-20 — 6. The whole stack, on real hardware

Frames off the bench Ares-M PRO through `loader` -> `bindings` -> `camera`:

```
bin4: (752, 752)   uint16  mean=142.7  in 0.25 s
bin1: (3008, 3008) uint16  mean=143.2  in 1.50 s
window (500,500,1024,1024) -> exactly that, mean=143.1
gain=220 offset=35 e-/ADU=0.355 temp=30.8 C
```

Read-back geometry matches what was asked for in every case, including the
multiple-of-four rounding and the bin-resets-window behaviour. `e-/ADU` and the
vendor's gain presets come from the camera rather than a table, so they follow the
model.

**Unsettled, and deliberately not guessed: the saturation level. [Player One]**
These are 14-bit sensors delivered in a 16-bit container, and whether the SDK
left-shifts into the top bits or leaves the value at 0-16383 is not stated in the
header and cannot be read off a dark frame. `ccd_saturation_level` defaults to
`(1 << bitDepth) - 1`, the conservative reading, and the bring-up ladder gets a
`saturation` stage that measures it against a flooded sensor. Written down because
a wrong saturation level silently corrupts every flat and every non-linearity
correction downstream.

## 2026-08-20 — 7. `ty` for type checking, and it paid for itself on the first run

Added `ty` to the dev group and to CI, configured in `pyproject.toml`.
**Not strict**, on purpose: the goal is catching what ctypes code gets wrong, not
winning an argument with a checker. The policy is that **seams and high-level code
stay clean** -- `loader.py`, `camera.py`, the instruments, the fake -- while the
**binding layer may take narrow, documented exceptions** where `restype`/`argtypes`
assignment to opaque handles genuinely defeats static typing.

**The first run found 34 real defects, all in the fake library. [any vendor]**
*Symptom:* a fake that raises `AttributeError` where the real library returns an
error code, so a test of the error path passes for the wrong reason.
Every `self._camera(camera_id)` returns `_CameraState | None`, and fifteen call
sites dereferenced it without checking. Passing an invalid camera id to
`POAGetImageStartPos` would have raised `AttributeError` out of the fake instead
of returning `POA_ERROR_INVALID_ID` -- which is exactly the divergence that makes
a fake worse than useless, because the driver's error handling would be tested
against behaviour the real library never produces. Fixed by adding the guards, not
by suppressing the rule. **A fake's error paths need type checking more than the
real code does**, because nothing else forces them to match the contract.

**Trap — `platform.system()` cannot be narrowed; `sys.platform` can. [any vendor]**
*Symptom:* `os.add_dll_directory` reported as missing on a non-Windows checkout,
with no way to silence it that does not also hide real errors.
`if platform.system() == "Windows"` is opaque to a type checker, so the
Windows-only call inside looks unconditional. `if sys.platform == "win32"` is
understood by every Python type checker and narrows the branch. The better idiom
anyway; the checker just made it visible.

Nothing is suppressed today. The `[[tool.ty.overrides]]` table is documented in
`pyproject.toml` as a pressure valve, not a starting point.

## 2026-08-20 — 8. Typed arguments, enforced rather than remembered

`ty` catches wrong types; it does not demand that arguments *have* them. So the
rule "the high-level code and the seams are typed" was true only by inspection --
`_expose(self, image_request)` and seven of its neighbours had bare arguments.

Fixed by annotating them, and then by making the rule mechanical: **ruff's `ANN`
rules are on**, with `ANN401` off (`Any` is the honest type for a ctypes handle)
and per-file exemptions for the four modules that mirror C signatures --
`enums`, `structs`, `bindings`, `simulator`. Tests and PEP 723 scripts are exempt
too: a test name is its documentation.

The line is the same one the type-checking policy draws, and it is worth stating
in the skill: **annotate where a human calls in, not where C calls in.**
`_expose(request: ImageRequest) -> CameraStatus` tells a reader what chimera
promises; `_POAGetImageData(self, camera_id, buf, size, timeout_ms)` would gain
nothing from four `Any`s, and the noise would hide the two annotations that mean
something.

`ty` also runs as a pre-commit hook now, pinned to the same version as the dev
group. Note it checks the **whole project**, not the changed files -- the hook
says why, and it is right: a change in one file can create a diagnostic in
another, so per-file checking gives a false all-clear.

## 2026-08-20 — 9. The fake agreed with the header, and both were wrong

The bring-up ladder's `geometry` stage printed this on real hardware:

```
bin 1: 3008x3008    bin 2: 1504x1504
bin 3: 1000x1002  (rounded from 1002)
bin 4:  748x750                        <- should be 752x752
```

3008/4 is exactly 752 and needs no rounding, so 748x750 could not come from
binning full frame. It comes from binning **whatever ROI was already set**.

**Trap — `POASetImageBin` rescales the current ROI; it does not reset to full
frame. [Player One, but the shape is universal]**
*Symptom:* frames get slightly smaller each time you change binning, and only on
some paths through the code.
Measured directly:

```
1 -> 4          : 3008x3008 -> 752x752            (full frame stays full frame)
1 -> 3 -> 4     : 3008x3008 -> 1000x1002 -> 748x750
window survives : bin1 1024x1024 @(500,500) -> bin2 512x512 @(250,250)
```

The start position scales too. And because each step rounds, **the rounding loss
compounds**: bin 3 loses 1002 -> 1000, and bin 4 then rescales *from the rounded
value*, 1000·(3/4) = 750 -> 748.

Two things came out of it, and the second matters more than the first.

**A real bug in our driver.** `Camera.configure()` set binning and then only
touched the ROI if a window was requested, so consecutive `configure(binning=N)`
calls inherited the previous window. Fixed: with no window, full frame is now
requested *explicitly*. Re-ran the ladder; bin 4 reports 752x752.

**The fake had the same wrong belief, so no test could have caught it.** The
simulator was written from the header, the header reads like binning resets the
frame, and the fake did exactly that -- so driver and fake agreed, and the suite
was green. **A fake built only from documentation encodes the documentation's
errors and then certifies them.** The fake now rescales the way the hardware
does, the measured numbers above are pinned as tests, and the assertion that used
to demand `start == (0, 0)` after a bin change now demands `(50, 50)`, with a
comment saying hardware overruled it.

The general rule for the skill: **every behaviour a fake asserts is a claim about
hardware, and it stays a guess until the ladder runs.** Build the fake from the
header, then run the ladder early and treat every disagreement as the fake's
fault until proven otherwise.

**Bonus, undocumented: height rounds down to a multiple of 2. [Player One]**
The header states the width-multiple-of-4 rule and says nothing about height.
Measured:

```
(1023,1024) -> (1020,1024)      (1024,1023) -> (1024,1022)
(1021,1024) -> (1020,1024)      (1024,1021) -> (1024,1020)
```

Both rules are now in the fake and in `set_image_size`'s docstring.

## 2026-08-20 — 10. The host application's name on PyPI belongs to someone else

The clean-install check -- install the built wheel into an isolated environment
and see whether the promise holds -- failed before it got as far as loading
anything:

```
x Failed to build `chimera==0.4.7`
  print "Setuptools version",version,"or greater has been installed."
  SyntaxError: Missing parentheses in call to 'print'
```

**Trap — a path source in `[tool.uv.sources]` does not travel in a built wheel.
[any plugin whose host is unpublished]**
*Symptom:* everything resolves perfectly in the checkout, and `pip install`
pulls a completely different project.
`chimera` on PyPI is an unrelated Python 2 package last touched a decade ago.
This repo redirected the name with `chimera = { path = "../chimera" }`, which
works for *this checkout* and is **not** recorded in the wheel's metadata --
`Requires-Dist: chimera` is all a consumer sees, and they get the wrong one.
`chimera-zwo`'s pyproject warns about the name in a comment; nothing enforced it,
because nothing had ever built a wheel and installed it anywhere else.

Fixed by not declaring it: `chimera` moved to the **dev group**, where the path
source applies and tests still resolve it. It was never needed at runtime anyway
-- a chimera plugin is loaded *by* chimera, so the host is present by
construction. `Requires-Dist` is now `numpy` and `astropy`, both of which mean on
PyPI what we mean by them.

The general rule: **declare a dependency only on a name that means the same thing
to the person installing it as it does to you.** For a plugin whose host is not
published, that means not declaring the host at all.

## 2026-08-20 — 11. The promise, checked the only way that counts

```
$ cd /tmp/poa-clean
$ uv run --isolated --no-project --with chimera_player_one-0.1-py3-none-any.whl python
cwd: /private/tmp/poa-clean
*LIBRARY_PATH set: none
sdk root: ~/.cache/uv/archive-v0/.../site-packages/chimera_player_one/_sdk
camera SDK: 3.10.1
  Ares-M PRO   IMX533   3008x3008  sn=CAMD03F2895002109000
  Sedna-M      IMX178   3096x2078  sn=CAMGE0E2182022109000
wheel SDK: 1.2.3.0
  POA Phoenix Wheel  7 positions
```

From the wheel alone: no repo, no SDK download, no compiler, no working directory
requirement, and **no `DYLD_LIBRARY_PATH`**. Compare the research repo's
`tests_sdk/_common.py`, which sets one, `os.execvp`s itself to apply it, and
`chdir`s into the SDK directory because the vendor wrapper dlopens a relative
path.

Wheel contents, 53 files / 5.5 MB uncompressed / 2.2 MB on the wire:

| | camera | filter wheel | other |
|---|---|---|---|
| macOS | universal dylib | universal dylib | universal libusb |
| Linux | x64, arm64, arm32, x86 | same four | -- |
| Windows | x64, x86 | x64, x86 | -- |

plus both licences, both headers, the udev rules and `provenance.toml`.

## 2026-08-20 — 12. A flaky timing test that was neither flaky nor about timing

`test_abort_stops_a_long_exposure_promptly` failed in the full suite and passed
on its own. The tempting fix is to relax the bound; the suite runtime said not to
— **32 s**, when the file alone took 2 s. A suite that takes exactly one 30 s
exposure longer than it should is not flaky, it is failing to abort.

**Trap — a relative `filename` in an `ImageRequest` writes into the current
directory, and the collision suffix scan is O(files). [any chimera camera]**
*Symptom:* a test suite that gets slower every time it runs, and starts failing a
timing assertion in an unrelated test.
One test used `"filename": "$DATE-test"`. `ImageUtil.make_filename` expands that
relative to the cwd — the repo root — and on collision appends `-001`, `-002` …
**up to 999**. Every run left another frame behind, so every run had a longer scan
to do before it could write, inside the exposure path, which is what the abort
test was measuring around.

Fixed at the cause: every test that exposes now passes an explicit `tmp_path`
filename, and `*.fits` is in `.gitignore` so a hand-run probe cannot leave one
either. Suite back to 2.6 s, 88/88, stable across repeated runs.

Worth keeping as a rule: **when a test is flaky, check the suite's wall time
before touching the assertion.** The bound was not wrong; something really was
taking 30 seconds.

## 2026-08-20 — 13. The saturation question, answered — and neither candidate was right

The `saturation` stage, run on a flooded sensor:

```
   0.01 s  max=   449   0.10 s  max=  3264
   1.00 s  max= 38655   4.00 s  max= 65532  (97.60 % of pixels at max)
```

**RAW16 is left-shifted, and full scale is `16383 << 2` = 65532. [Player One]**
Not 16383, because the data plainly goes past it. And **not 65535 either**, which
was the other candidate — the low two bits are always zero, so 65535 is
unreachable. The driver's default was 16383; it is now
`((1 << bitDepth) - 1) << (16 - bitDepth)`.

Why this is worth the fuss: a flat-field or linearity routine told the saturation
level is 65535 **never sees a saturated pixel**, and fits its curve straight
through the clipped end of its own data.

**And it can be answered without light.** If the data is shifted, every value is a
multiple of `1 << (16 - bitDepth)`. On a dark frame: 100.00 % of values were
multiples of 4 — the same answer, from a capped sensor, before the lamp came out.
The `saturation` stage now reports both and says they must agree, so the next
sensor can be characterised on a cloudy afternoon.

## 2026-08-20 — 14. The fake was wrong a second time, in the same way

The `wheel` stage failed on its first line of real work:

```
wheel: FAILED -- POAGetCurrentPosition failed: PW_ERROR_IS_MOVING (PW is moving)
```

**Trap — the filter wheel reports "moving" as an ERROR RETURN, not as a sentinel
position. [Player One]**
*Symptom:* an exception from the position poll, at exactly the moment you are most
likely to be polling.
The wire protocol uses `0xFF` for between-detents and `PlayerOnePW.h` describes a
`-1`, so **both** obvious readings say "check the value". The SDK does neither: it
returns `PW_ERROR_IS_MOVING` and writes nothing. Our binding raised, the ladder
died, and `PlayerOneFilterWheel.get_filter()` would have raised instead of
returning `"MOVING"`.

Fixed by normalising in one place — `FilterWheelSdk.get_position` maps
`PW_ERROR_IS_MOVING` to `-1` and re-raises everything else, with a test pinning
that it does not swallow other failures. Callers that need to *wait* poll
`get_state` instead, which is what the driver and the ladder now do.

**This is the second time the fake encoded a wrong belief taken from the header,
and it is the same lesson as entry 9**: the fake returned `-1` because that is
what the documentation says, driver and fake agreed, and the suite was green. Two
independent instances in one day is enough to make it a rule for the skill rather
than an anecdote: **a fake written from documentation is a hypothesis. Run the
ladder before trusting a single green test that depends on it.**

With that fixed, the wheel steps cleanly:

```
POA Phoenix Wheel  sn=38FF67063046463922600943  7 positions
  -> 1 in 1.68 s   -> 2 in 1.92 s   -> 3 in 1.92 s
  -> 4 in 1.61 s   -> 5 in 1.64 s   -> 6 in 1.75 s
```

~1.7 s per single-slot move. This unit's firmware filter names are empty and its
focus offsets are all zero, so the config-vs-firmware comparison stays quiet —
`_check_configuration_against_firmware` only warns on a *non-empty* disagreement,
which is the right behaviour for a wheel nobody has labelled.
