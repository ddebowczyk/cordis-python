# Repository operations: inventory

What this repository can currently *do to itself* — every operation that exists
today, where it lives, how it is invoked, and who owns it. This is the survey
taken before the operations were split into capabilities under [`ops/`](../../ops);
it is the "as found" record, so a later reader can tell what was reorganised
from what was invented.

Taken at commit `19f20d3`, when every operation lived in one of three places: a
flat root `justfile`, two GitHub workflow files, or a shell history nobody else
could replay.

## The shape of the problem

Twenty-one operations exist. Their homes:

| Home | Count | What that costs |
| --- | --- | --- |
| Root `justfile` | 15 recipes | One file owns lint, tests, spec, docs and CI. Nothing declares what a recipe reads or writes, so a recipe cannot be moved without reading all of it. |
| `.github/workflows/` | 2 files, 10 jobs | The CI order is written twice — once in `ci` and once in `ci.yml` — and the two drifted already. |
| Nowhere | 4 operations | Mutation verification, interpreter-matrix smoke, release tagging and toolchain checks were performed by hand and survive only in transcripts. |

The four homeless operations are the reason for this document. Each was run,
each produced a decision that shaped the library, and none can be re-run by
anyone but the person who ran it.

## Inventory

### Quality gates

| Op | Invoked as | Implementation | Reads | Writes |
| --- | --- | --- | --- | --- |
| Lint | `just lint` | `uv run ruff check .` + `ruff format --check .` | `**/*.py`, `pyproject.toml` | — |
| Autofix | `just fix` | `uv run ruff check --fix .` + `ruff format .` | `**/*.py` | `**/*.py` |
| Types | `just types` | `uv run mypy` | `src`, `tests`, `examples` | — |
| Gate | `just check` | `lint` + `types` | — | — |

Configuration lives in `pyproject.toml`: 24 ruff rule sets with per-file
ignores that carry their justification in comments, and `mypy --strict` with no
per-module relaxations. Both are settings, not scripts — nothing declares that
the `just` recipes and the `[tool.*]` tables are one operation with two halves.

### Test lanes

| Op | Invoked as | Profile | Selection |
| --- | --- | --- | --- |
| Fast loop | `just test` | `local` (50 examples) | not nightly, not release |
| Pull request | `just test-pr` | `pr` (200) | not release |
| Nightly | `just test-nightly` | `nightly` (2000) | everything |
| Pre-release | `just test-release` | `release` (20000) | everything |
| Replay | `just replay TEST` | `local` | one test, `-x` |
| Generator health | `just discards` | `pr` | reports invalid-draw rates, worst first |

The four tiers mirror the `test_tier` field on every property card in
`spec/capabilities/`, and the markers are declared in `pyproject.toml` under
`--strict-markers`. Hypothesis profiles are registered in `tests/conftest.py`.
CI persists the shrunk-example database between runs so a failure found once is
replayed forever after.

**Homeless:** *mutation verification*. Every capability was closed by mutating
the implementation and confirming the tests fail — the harness lists
`(title, test selector, [(path, before, after)])`, restores originals in a
`finally`, and runs under a dedicated `mutation` Hypothesis profile. It lived in
a scratchpad directory and is gone. The practice is load-bearing (it caught an
equivalent mutant and two tests that asserted nothing) and it has no home in the
repository.

### Specification catalog

| Op | Invoked as | Implementation |
| --- | --- | --- |
| Schema validation | `just spec-check` (first half) | `uvx check-jsonschema --schemafile spec/schema/capability.v1.yaml spec/capabilities/*.yaml` |
| Cross-file checks | `just spec-check` (second half) | `spec/check_spec.py` — reference resolution, tier ordering as a real build order, MUST-to-property-card coverage |
| Build-order table | `just spec-table` | inline `yq` + `awk` over every record |

`spec/check_spec.py` is a `uv run --script` with a PEP 723 header, so it needs
no project environment. The catalog it checks is 20 records, 145 normative
rules and 126 property cards, and it ships in the sdist.

This is the one operation whose *subject* is unusual: the specification is the
contract the library is held to, so validating it is not a lint pass but the
repository checking its own premises.

### Documentation

| Op | Invoked as | Implementation |
| --- | --- | --- |
| Generate reference | `just docs` | `docs/build_reference.py` → `docs/reference.md` |
| Staleness gate | `just docs-check` | the same script with `--check`, exit 1 if the file on disk differs |
| README execution | (test only) | `tests/test_readme.py` extracts the quickstart fences, concatenates and runs them |

`docs/reference.md` is derived, never written: 189 exports grouped by the
capability record whose declared surface names them. The generator must produce
byte-identical output on every interpreter — CI pins no version for this step,
and the first CI run went red precisely because it did not.

Also documentation, but held nowhere: `CHANGELOG.md` (hand-maintained, with the
project's rule that a normative spec rule is public contract), `README.md`
status counters (`20 capabilities`, `145/126`, test count), and this directory.
Nothing checks that the counters still match the catalog.

### Continuous integration

| Job | Workflow | What it runs |
| --- | --- | --- |
| `capability catalog` | `ci.yml` | schema validation, then `check_spec.py` |
| `lint and types` | `ci.yml` | ruff, ruff format, mypy, `docs-check` |
| `wheel and sdist` | `ci.yml` | `uv build`, `py3-none-any` assertion, sdist-carries-spec assertion, `twine check`, artifact upload |
| `tests (3.11 / 3.12 / 3.13 / pypy3.11)` | `ci.yml` | property tests, profile chosen by event, Hypothesis DB cached |
| `gates` | `release.yml` | every gate above, at the `release` profile |
| `build` | `release.yml` | tag-matches-`__version__`, wheel shape, sdist contents, `twine check` |
| `publish` | `release.yml` | `pypa/gh-action-pypi-publish`, environment `pypi`, trusted publishing |

`just ci` claims to be "everything CI runs, in CI's order" and is not: it omits
the packaging job entirely and cannot run the interpreter matrix. That is the
clearest single symptom of operations without owners — two descriptions of one
process, with no mechanism that makes them agree.

### Versioning and release

| Op | Invoked as | Notes |
| --- | --- | --- |
| Read version | — | `cordis.__version__` in `src/cordis/__init__.py`; hatch reads it via `[tool.hatch.version]` |
| Set version | by hand | edit the module, remember `CHANGELOG.md` |
| Verify tag ↔ version | `release.yml` only | shell in the `build` job; unavailable locally |
| Publish | tag push | `release.yml` → PyPI |
| GitHub Release | **absent** | no release is created, so the tag carries no notes and no artefacts |

**Homeless:** *release management*. There is no way to ask "what version are we
on, is the tree clean, does the tag exist, is it annotated, does it point at
HEAD" without reading a workflow file and running its shell by hand. A version
bump is an unassisted edit in two files, and nothing forbids going backwards.

**Homeless:** *interpreter-matrix smoke*. The built wheel was installed into
fresh 3.11 / 3.12 / 3.13 / pypy3.11 environments and imported — which is how the
`mappingproxy` dataclass defect and the `__weakrefoffset__` PyPy defect were both
found. Neither would have been caught by the source-tree test run, and the
procedure exists only as a shell loop in a transcript.

**Homeless:** *toolchain doctor*. `ruff`, `mypy` and `check-jsonschema` are not
on `PATH` — they are reached through `uv run` / `uvx`. `yq`, `just`, `gh` and
`bd` must be installed. Nothing states the requirement or checks it, so a fresh
checkout fails at the first recipe with a message about the wrong thing.

### Issue tracking

`bd` (Beads, Dolt backend) holds the development plan: one epic, one task per
capability, closed with the evidence that closed it. `.beads/hooks/` contains
`pre-commit`, `pre-push`, `post-merge`, `post-checkout` and
`prepare-commit-msg`, none of which are installed into `.git/hooks` — a
`bd hooks install` that nothing runs and nothing checks.

## What the inventory says

Three conclusions, which set the shape of `ops/`:

1. **Ownership is missing, not structure.** Most operations exist and work. What
   none of them declare is what they read, what they write, and which other
   operation they depend on — so none can be moved, tested, or reasoned about in
   isolation.
2. **The gaps cluster around the release.** Every homeless operation —
   mutation, matrix smoke, tagging, doctor — is one a release depends on. That
   is not a coincidence: the fast loop is run daily and stayed tidy, while the
   operations run once were never given a home.
3. **Two descriptions of CI is one too many.** `just ci` and `ci.yml` must
   become one description with two renderings, or they will drift again.

## The split

Nine capabilities, each owning its directory, its manifest and its entry points.
See [`capabilities.md`](capabilities.md) for the mapping from every row above to
its new owner, and [`ops/README.md`](../../ops/README.md) for the contract each
manifest is held to.
