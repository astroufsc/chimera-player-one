# chimera-template: the evaluation, the fixes, and agent orientation files

> **Status: planned, nothing executed.** Written 2026-08-21. No repo has been
> changed — not this one, not `chimera-zwo`, not `chimera-template`, not
> `chimera-plugin`. This document is the whole state; there is nothing else to
> recover.
>
> **Delete or collapse this file when the work lands.** It is a work order, not a
> record — the record goes in `BUILD-LOG.md` as it happens. A plan that outlives
> its execution becomes a to-do list nobody owns, which is one of the failure
> modes this plan exists to fix.
>
> **Constraints that were decided the hard way. Do not re-litigate them:**
>
> - **The `chimera` dependency stays exactly as it is, in every repo, in both
>   directions.** See "left open, deliberately" below. A session that reads
>   BUILD-LOG entry 10 cold will want to "fix" the template. That has been ruled
>   out explicitly.
> - **`main`, never `master`**, for the template and anything new.
> - **Licence files stay.** SPDX is a declaration; GPL-2.0 §1 and the MIT-shaped
>   vendor notices require the text to travel.
> - **Publishing `sky-viewer` is out of scope.** Document it as a condition with
>   its clearing criterion; do not act on it.
> - **One item in workstream D is flagged for veto** (neutralising the skill's
>   host-dependency prescription). Ask before doing it.

## Context

Two things prompted this. First, no `chimera-*` plugin repo has an agent file
worth reading, and the `chimera-driver` skill never says to write one — so every
session re-derives the same facts, and the cross-repo hand-off notes
(`FINDINGS-FROM-CHIMERA-ZWO.md`, `SKILL-FEEDBACK.md`) exist only because there
was nowhere structural for them to live. Both are already stale, in different
ways, which is the drift this is meant to stop.

Second, and larger: **`chimera-template` exists, is a cookiecutter used with
`cruft`, and the skill never mentions it.** The skill says `uv init --lib` and
copy assets — starting from an empty directory when a maintained template was
right there.

**Decisions taken:** the `chimera` dependency question stays open and **nothing
moves in either direction**; licence files stay; `main` replaces `master`; the
template is fixed in its own PR; the evaluation lands as a BUILD-LOG entry here
plus a generalised section in the skill; all small local defects are in scope;
publishing `sky-viewer` is out of scope and gets documented as a condition.

---

## The evaluation: how far are we from the template?

The template generates **11 files**. It is a packaging skeleton, not a plugin
scaffold.

### What it gets right — including the thing that matters most

`src/` layout, the load-bearing `chimera_` package prefix, the `instruments/`
subpackage, `requires-python = ">=3.13"`, `line-length = 88`,
`target-version = "py313"`, the SPDX `notice-rgx`, GPL-2.0-or-later, and uv-only.

Most importantly it encodes **chimera's classloader rule** in
`cookiecutter.json`: `__instrument_module_name` is the class name lowercased with
separators stripped, because `classloader.py` does `__import__(clsname.lower())`.
Its own `test_template.py` pins it. That is the rule that fails *silently* —
nothing loads a driver until a `chimera.config` names the class — and
`chimera-zwo` shipped `ZWOAM5` in `zwo_am5.py` from its first commit until
2026-08-20 with nothing noticing.

### Where the template is genuinely wrong

| # | Defect | Consequence |
|---|---|---|
| 1 | `.gitignore` ignores `*.so`, `lib/`, and `uv.lock` | Silently drops vendored Linux blobs — a wheel that works on the author's Mac and is broken on every Linux deployment. Both our repos carry warnings naming each other. Ignoring `uv.lock` also makes `uv sync --locked` impossible. |
| 2 | No licence file at all | Every generated project claims an SPDX identifier with no licence text. GPL-2.0 §1 and MIT-shaped vendor licences both *require* the text to travel — this is compliance, not style. The GPL text exists at `licenses/CHIMERA.rst` but sits outside `{{cookiecutter.project_slug}}/`, so it is never copied. |
| 3 | No `[dependency-groups]`, no pytest config, no test | Its own `README.md` and `CLAUDE.md` prescribe `uv run pytest` / `ruff` / `pre-commit`, none of which can run from a fresh generation. |
| 4 | **No `.github/workflows/` at all** | 8 of the 10 repos descended from it have no CI whatsoever. |
| 5 | Default branch `master`; CI filters `[master, main]` | New repos use `main`. |

Plus hygiene: pre-commit pinned to ruff `v0.8.0` with the old `ruff` hook id
(renamed `ruff-check` upstream) and no type checker; `target-version` hardcoded
rather than following `python_version`; the `flake8-copyright` table inert
because `CPY` is not in `select` (true in all three repos); instrument and
controller examples byte-identical apart from the word, both subclassing
`ChimeraObject` directly rather than `CameraBase`/`TelescopeBase`; `README`
recommending `pip install` while `CLAUDE.md` says "ONLY use `uv`, NEVER `pip`";
`cruft` documented only inside the generated `CLAUDE.md` and never to a human; an
untracked pre-`src/` legacy tree at the template root.

### Where we are behind, or just wrong

`py.typed` is missing in **all three** — both our repos are fully annotated and
run `ty`, and consumers see an untyped package. Locally: `README.rst` is
truncated (headless, mid-sentence, and it is the wheel's long description);
`README.rst:99` says `master` while `ci.yml` filters `main`; `[project.urls]
source` points at `astroufsc/chimera` rather than this repo; no pytest
`hardware` marker.

### The cruft picture

Six repos have live `.cruft.json`; two are detached (boilerplate, no link).
**Ours were never linked, `cruft` is declared nowhere, and `cruft` is not
installed** — so "use `cruft` to manage template updates" has never been
exercised. **Do not retro-link yet:** `cruft diff` today reports a near-total
rewrite of `pyproject.toml`, `.gitignore` and `README`, plus deletion of five
files we never had; a `skip` list narrow enough to be safe leaves one tracked
file. Linking becomes worth doing once the template owns a coherent, low-churn
slice — CI, pre-commit, `COPYING`, `py.typed`, `test_plugin_discovery.py` — which
is what workstream A creates.

---

## The `chimera` dependency: left open, deliberately

The template and the two repos disagree, and the disagreement is **not resolved
by this work**. Nothing moves in either direction.

| | template | both repos |
|---|---|---|
| declared in | `[project.dependencies]` | `[dependency-groups] dev` |
| source | `git = "…/chimera.git"` | `path = "../chimera"` |

Recorded here so the next session does not re-litigate it from scratch, and so
neither side is mistaken for settled practice:

- **For declaring it** (template): a plugin imports `chimera`, so the dependency
  is real; a git source means a fresh checkout resolves with no sibling repo.
- **Against** (repos, BUILD-LOG entry 10, measured): `[tool.uv.sources]` does not
  travel into wheel metadata — uv's own docs recommend `uv build --no-sources` to
  check exactly this — so the wheel says `Requires-Dist: chimera` and a consumer
  gets an unrelated Python 2 package. Both `package` jobs install the built wheel
  isolated and would fail.
- **Neither spelling fixes CI.** `lint`/`test` are red because `sky-viewer` is
  unpublished and chimera's own `chz1` path source has no remote. That is
  independent of how the dependency is written, and out of scope by decision —
  both conditions get written into the `ci.yml` headers with their clearing
  criteria, the form `chimera-zwo` already uses well.
- **There is no uv mechanism** for "committed git source, silently overridden by
  a local path": `sources` is project metadata and cannot live in a user-level
  `uv.toml`. The options are `uv add --editable ../chimera` (rewrites pyproject,
  leaves the tree dirty), `uv run --with-editable` per invocation, or a real uv
  workspace.

**Consequence for scope:** BUILD-LOG entry 10 is *not* superseded, no `uv.lock`
is regenerated, and the `package` jobs are untouched. What the template still
gains is a dev-tools group — see A3, which adds pytest/ruff/ty/pre-commit and
touches `chimera` not at all.

---

## The rule that keeps all this from drifting

One question decides every placement: **what would make this sentence wrong?**

| answer | home |
|---|---|
| a change to the code on the next line | the comment at the definition |
| a file moving, or a command being renamed | **`AGENTS.md`** |
| nothing — it already happened | `docs/notes/BUILD-LOG.md` |
| doing the work | `docs/notes/OPEN-ISSUES.md` |
| the recipient acting on it | `FINDINGS-*.md` — outbound, deletable |
| a second vendor disagreeing | the `chimera-driver` skill |

Two enforcement clauses do the real work:

1. **`AGENTS.md` carries rules and paths, never measured numbers.** A number
   belongs in the BUILD-LOG where it is dated, or in a test where it fails when
   it stops being true. A rule cannot drift; a number can only drift.
2. **It may repeat a conclusion; never the evidence.** Two lines maximum, ending
   in a path.

Perishable facts get **one contained block** near the top — the only place in the
file allowed to hold something time can break — each line naming what makes it
false and where the authoritative statement lives.

---

## A. `chimera-template` — its own PR

Branch off `master`, and **rename the default branch to `main`** as part of it.

1. **Fix `.gitignore`**: drop `*.so`, `lib/`, `lib64/`; anchor `/dist/`,
   `/build/`; stop ignoring `uv.lock`; add the warning naming what it would eat.
2. **Ship `COPYING`** inside `{{cookiecutter.project_slug}}/` (from
   `licenses/CHIMERA.rst`, conditional on the licence choice) and add
   `license-files = ["COPYING"]`.
3. **Make the documented commands work**: `[dependency-groups] dev` with pytest,
   pre-commit, ruff, ty; `[tool.pytest.ini_options]` with `testpaths`,
   `--import-mode=importlib`, `markers = ["hardware: needs real hardware"]`;
   `[tool.ty.environment]`. Drop the nested empty `tests/{{package_name}}/`.
4. **Generate `tests/test_plugin_discovery.py`** — the template already holds the
   class and module names as variables, and this guards the rule that fails
   silently.
5. **Generic `.github/workflows/ci.yml` + `release.yml`** — device-independent
   half only (lint, test matrix, `workflow_call` from release, tag↔version check,
   `push: branches: [main]`). Derived from the skill's assets, stripped of the
   vendored-blob and libusb steps.
6. Hygiene: `py.typed`; pre-commit to `v0.14.4` / `ruff-check` / add `ty`; `ANN`
   in `select`; `target-version` follows `python_version`; resolve the inert
   `flake8-copyright` table; fix the README's `pip install` contradiction and
   stale structure diagram; **document `cruft` in the template's own README**;
   delete the untracked legacy `chimera_{{module_name}}/` tree.

Left alone deliberately: **the `chimera` dependency and its source** — that
question is open and A3 adds only the dev *tools* beside it — and everything
driver-specific (`_sdk/`, `sdk/` layers, `doctor.py`, probes, simulators,
`vendor_sdk.py`), which belongs to the skill's `assets/`.

## B. `chimera-player-one`

**`AGENTS.md`** (~115 lines) plus a 3-line **`CLAUDE.md`** pointer. Sections:
`# title` · `## Ground rules` · `## Things that are true today` (the only
perishable block) · `## The shape` (layer diagram, seam named) · `## Commands`
(each with what it proves) · `## What will bite you` · `## Do not "fix" these` ·
`## Where knowledge lives` (routing table of questions).

- **`## Do not "fix" these` is the section that pays for the file.** Layout,
  commands and most traps are recoverable from the repo in twenty minutes; the
  list of things that look wrong and are right is not recoverable at any price —
  and `assess.py` reports two of them as gaps, so an agent with a linter and good
  intentions will close them. Contents: the six deliberately unlocked
  cooling/fan methods, the bespoke `.gitignore`, the one fat `py3-none-any`
  wheel, the vendored libusb and our rpath, `_TRANSFER_TIMEOUT_MS = 2000`, and
  that superseded BUILD-LOG entries are marked rather than rewritten.
- **State the template relationship positively**: *this repo did not come from
  `chimera-template`; the structural reference is the `chimera:chimera-driver`
  skill.* "Rule 3 does not apply" is invisible to someone who never saw rule 3.
- The shared ground-rules bullets open both new files **in the words of the
  nine-repo boilerplate**, so the 7-line file reads as the head of the long one
  rather than a superseded pattern.

**New `docs/notes/OPEN-ISSUES.md`**, seeded from the live half of
`FINDINGS-FROM-CHIMERA-ZWO.md` — without it `AGENTS.md` grows into the
open-issues list, which is what guarantees it rots. Then split
`FINDINGS-FROM-CHIMERA-ZWO.md`: live items move, delivered items are marked, and
its `@lock` section is corrected — it calls the six methods "a bug, still open"
while BUILD-LOG 19 and `tests/test_concurrency.py` record them as decided.

**Local fixes:** restore the truncated `README.rst` title and opening; `master` →
`main` at `README.rst:99`; add `src/chimera_player_one/py.typed`; fix
`[project.urls] source`; add the `hardware` marker and convert
`test_hardware.py`'s module-level skip to it.

**New `tests/test_agents_md_paths_resolve.py`** — pull backticked paths out of
`AGENTS.md`, assert each exists. Ten lines. `AGENTS.md` is the only file here
with no mechanism keeping it true, and a moved path is its commonest rot;
`check_skills.py` already does this for the skill.

**BUILD-LOG entry 21** — the evaluation, the dependency reversal with entry 10
marked superseded, why we are not retro-linking yet, and the `@lock` finding as
the worked example of three-way drift (entry 19 vs FINDINGS §1 vs the test, all
disagreeing within a day).

## C. `chimera-zwo`

Same seven-section `AGENTS.md` (~145 lines — two devices and two seams, so `## The shape` is a two-column
table) plus the `CLAUDE.md` pointer. Its `## The shape` must name the **external
evidence ledger** (`~/dev/pH/kepler/am5/claims.toml`, classes A/F/D/C) — the
least guessable fact about the repo — and that where `docs/am5-protocol.md` and
the code disagree, the code wins.

**Trim the OPEN-ISSUES appendix** rather than superseding it: it is addressed to
one agent doing one job, is consumed when the AM5 work lands, and is imperative
where `AGENTS.md` is declarative. Remove the six lines now permanently in
`AGENTS.md` (skill-first, commit style, BUILD-LOG tagging, the gate, the CI
expectation, "read entry 14") and open it with *"`AGENTS.md` is already loaded;
this says only what is specific to the job."* Test: delete any sentence equally
true of a session fixing a typo.

Mark `SKILL-FEEDBACK.md` delivered — all twelve items have landed.

Same class of local fixes: `uv sync` → `uv sync --locked` in both CI jobs;
`.python-version`; `py.typed`; SPDX headers on `tests/conftest.py`,
`scripts/am5_probe.py`, `scripts/pe_certificate.py`.

## D. The `chimera-driver` skill

Baseline `main`, clean, v1.1, HEAD `97c5850`.

- **Neutralise the host-dependency guidance — flagged, veto if you disagree.**
  `references/vendoring.md:244` is titled "## Never declare a dependency on the
  host" and `references/traps.md:122` says "do not declare a runtime dependency
  on it at all". Once the skill tells people to scaffold from a template that
  does the opposite, it contradicts itself. I would not swap the prescription for
  the other one — I would state that the two live drivers differ, keep the
  measured evidence (a `uv.sources` redirect does not travel into wheel metadata;
  PyPI's `chimera` is an unrelated Python 2 package that breaks an isolated
  install), and leave the choice to the repo. That is a change *away* from a
  stated rule, so it is called out separately rather than buried.
- **New `## 2. Start from the template`**, before the ladder — scaffolding is step
  one, not an afterthought. `cruft create gh:astroufsc/chimera-template` gives
  layout, the `chimera_` prefix, the classloader filename rule and a `.cruft.json`
  so later template fixes arrive via `cruft update`. It must also say what the
  template does *not* give (no CI, no fake, no `sdk/` layer, no vendoring) and
  that a plugin with vendored blobs fixes its `.gitignore` immediately. Ladder
  step 2 becomes "scaffold from the template", replacing `uv init --lib`.
- **New `## 7. Write the orientation file`**, modelled on `## 5. Keep a build
  log` — 12 lines: the prescription, the rules-and-paths-not-numbers constraint,
  and the grounded cost (the `@lock` three-way drift). Ends with **do not copy
  another repo's file**: every load-bearing line is repo-specific, and a copy
  looks complete while being wrong. `## Done means` becomes `## 8` and stays
  terminal — verified safe, nothing cross-references a section by number.
- **New `references/orientation.md`** — the skeleton as *headings plus the
  question each answers*, plus the placement-rule table and the `CLAUDE.md` text
  inline. Deliberately **not** an `assets/` file: assets are "copied and
  renamed", which here yields a file whose skeleton is right and whose every
  sentence is false. Add its row to the §4 table.
- **New `traps.md` entry**: a template `.gitignore` that eats `*.so`.
- **`assets/pyproject.toml` is stale** — snapshotted from this repo @ `de53e52`,
  before `license-files = ["COPYING"]` landed in `82ccdef`, which
  `references/packaging-ci.md` already prescribes. Re-snapshot — which carries
  this repo's dependency spelling, unchanged, exactly as it is today.
- **`## Done means`** gains: *AGENTS.md exists, names the decisions that must not
  be "fixed", and every path it cites resolves.*
- Bump `metadata.version` to `1.2`; extend `metadata.source`.

`scripts/check_skills.py` enforces in CI: `SKILL.md` under 500 lines (212 now,
~235 after), restricted frontmatter keys, every `(references/…)` and `(assets/…)`
link resolving.

---

## Verification

- **Template**: `uv run pytest tests/` (its cookiecutter self-tests), then
  generate into a temp dir and prove the fixes — `uv sync && uv run pytest &&
  uv run pre-commit run --all-files` all succeed in the generated project, and
  `uv build` yields a wheel whose `dist-info/licenses/` contains `COPYING`.
- **Skill**: `check_skills.py`; `assess.py` against both plugins reports what it
  did before.
- **Both plugins**: `uv run pytest -q`; `uv run pre-commit run --all-files`; the
  new `test_agents_md_paths_resolve`. Nothing here touches dependency resolution,
  so `uv.lock` and both `package` jobs must come out byte-identical — if either
  moves, something strayed out of scope. Then **run every command in each
  `AGENTS.md`** — a command in an agent file that does not run is worse than no
  file.
- Re-check the perishable block at the moment of writing. `sky-viewer` and `chz1`
  belong in the `ci.yml` headers, not in `AGENTS.md`: they are facts about a
  third repo, and nobody working here would notice them change.

## Delivery

Four PRs, four repos: **`chimera-template` first** (the skill's new section
describes what the template does, so writing it before the fixes land would
document the broken version), then `chimera-player-one` (fork workflow — `origin`
is your fork, PR against `astroufsc`), `chimera-zwo` (`origin` is `astroufsc`
directly, branch `main`), then `chimera-plugin`.

**Note on `chimera-plugin`:** another session has been committing there
concurrently — it swept an earlier edit of mine into `d976348`. Worth checking
for in-flight work before branching.
