# Repository operations: the split

Nine capabilities. Each owns a directory under [`ops/`](../../ops), declares
itself in a schema-validated `capability.yaml`, and exposes its entry points
through its own `justfile`. The root `justfile` delegates and adds nothing.

This document is the mapping from [the inventory](inventory.md) to those owners:
every operation that existed, where it went, and every operation that did not
exist, where it was put.

## The nine

| Capability | Provides | Owns, in one line |
| --- | --- | --- |
| [`control`](../../ops/control) | `operations-control` | The ops catalogue itself: schemas, the validator, the invariants every other manifest is held to. |
| [`quality`](../../ops/quality) | `quality` | Lint, format and types — the gate that must be clean before anything closes. |
| [`test`](../../ops/test) | `test` | The four property-test tiers, replay, generator health, and mutation verification. |
| [`spec`](../../ops/spec) | `specification` | The capability catalog: per-file schema, cross-file consistency, build-order table. |
| [`docs`](../../ops/docs) | `documentation` | The generated reference, the executable README, and the counters that claim what the catalog contains. |
| [`version`](../../ops/version) | `version` | One authoritative version, changed forward-only, and the tag that must agree with it. |
| [`packaging`](../../ops/packaging) | `package-integrity` | Wheel and sdist: their shape, their contents, and whether they import on every supported interpreter. |
| [`release`](../../ops/release) | `distribution` | Cutting a GitHub Release: readiness, notes from the changelog, artefacts, verification. |
| [`workflow`](../../ops/workflow) | `local-delivery` | The toolchain doctor and the one description of CI that both the local runner and GitHub follow. |

## Where each operation went

### Moved, unchanged in behaviour

| Was | Is | Note |
| --- | --- | --- |
| `just lint` | `just ops quality lint` | |
| `just fix` | `just ops quality fix` | |
| `just types` | `just ops quality types` | |
| `just check` | `just ops quality all` | Root `just check` still works, but is now wider: it runs the whole `check` lane, of which `quality all` is one step. |
| `just test` | `just ops test fast` | |
| `just test-pr` | `just ops test pr` | |
| `just test-nightly` | `just ops test nightly` | |
| `just test-release` | `just ops test release` | |
| `just replay TEST` | `just ops test replay TEST` | |
| `just discards` | `just ops test discards` | |
| `just spec-check` | `just ops spec check` | |
| `just spec-table` | `just ops spec table` | |
| `just docs` | `just ops docs build` | |
| `just docs-check` | `just ops docs check` | |
| `just sync` | `just ops workflow sync` | |

### Given a home for the first time

| Operation | Owner | Why it had none |
| --- | --- | --- |
| Mutation verification | `test` | Ran from a scratchpad; the harness is now `ops/test/bin/mutate.py` with its campaigns declared in `ops/test/mutations.yaml`, so a claim of "8/8 caught" is reproducible. |
| Interpreter-matrix smoke | `packaging` | A shell loop in a transcript. It found both portability defects in 0.1.0, and now runs as `just ops packaging smoke`. |
| Toolchain doctor | `workflow` | Nothing stated what must be installed. `just ops workflow doctor` reports every tool each manifest declares under `requires.tools`, so the requirement is derived from the catalogue rather than restated. |
| Version management | `version` | An unassisted edit in two files with nothing forbidding a backwards move. |
| GitHub Releases | `release` | Tags published to PyPI but created no release, so a tag carried neither notes nor artefacts. |
| Counter checking | `docs` | `README.md` claims a capability count, a rule count and a card count. Now checked against the catalog. |
| Catalogue validation | `control` | New with `ops/` itself. |
| YAML linting | `quality` | Nothing checked that a YAML file was well-formed YAML. Schema validation only runs on files a schema is pointed at, so `ops/quality/capability.yaml` shipped with an unquoted colon that made it unparseable, and only `just ops quality yaml` found it. |

### Deliberately left where it is

| Operation | Where | Why |
| --- | --- | --- |
| ruff and mypy settings | `pyproject.toml` | Tool configuration belongs where the tools look for it. The `quality` manifest *reads* it and says so. |
| Hypothesis profiles | `tests/conftest.py` | They are test-suite code, not an operation. `test` reads them. |
| `spec/check_spec.py` | `spec/` | It ships in the sdist as part of the contract a downstream fork receives. `spec` reads and runs it rather than owning it. |
| `docs/build_reference.py` | `docs/` | Same reason: `docs/` is published, `ops/` is not. |
| Workflow files | `.github/workflows/` | GitHub requires the path. `workflow` owns them and is the only capability that may write there. |

## The one rule worth stating twice

A capability declares `owns`, `reads` and `generates`, and the validator holds
it to them: no two capabilities may claim the same path, no capability may write
into another's territory, and every file under `ops/` must have exactly one
owner. That is what makes the split real rather than cosmetic — without it,
nine directories are just a longer path to the same tangle.

The full contract is in [`ops/README.md`](../../ops/README.md); the schema that
encodes it is [`ops/capabilities/schema/capability.v1.yaml`](../../ops/capabilities/schema/capability.v1.yaml).
