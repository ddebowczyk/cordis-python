# Changelog

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with
one addition: **a normative rule in `spec/capabilities/` is part of the public
contract.** Changing what a rule requires is a breaking change even when no
signature moves, and the entry here says which rule and which record.

Before 1.0.0 the minor version carries breaking changes.

## [Unreleased]

## [0.1.0] — unreleased

The first release: all twenty capabilities of the catalog, implemented and held
to their specifications by property-based tests.

### Added

Tier 0 — the kernel:

- **Context tree** (`00`): derived contexts, scoped metadata, a service view.
- **Effect scope** (`01`): every registration returns its own undo; disposal is
  reverse-order, exactly-once and total, whether setup or teardown raises.
- **Service registry** (`02`): `(name, realm)` bindings owned by the provider's
  scope, so a provider's teardown removes its service and tells everyone.
- **Plugin mounting** (`03`): functions, modules, classes and `Service`
  subclasses as plugins, with isolation and interception declared at the mount.
- **Fiber lifecycle** (`04`): one state machine per mounted instance, with the
  transition table as data.

Tier 1 — composition:

- **Dependency injection** (`05`): `@inject`, continuous rather than one-shot —
  losing a dependency unloads the consumer, regaining it brings it back.
- **Event bus** (`06`): five dispatch modes as explicit contracts.
- **Config validation** (`07`): a plugin never starts half-configured; schemas
  are duck-typed, so a dataclass or a pydantic model both work.
- **Service isolation** (`08`) and **interception** (`09`): a subtree with its
  own implementation, or the shared one configured differently.
- **Event filtering** (`10`): listener admission decided by registration site.

Tier 2 — operating an application:

- **Diagnostics** (`11`): `inspect`, `pending`, `render_tree` — the runtime
  explains why something has not started.
- **Logging** (`12`): a structured record channel with pluggable exporters and
  no stdlib `logging` in the core.
- **Scheduling** (`13`): timers that unload with their owner.
- **Declarative loader** (`14`): the application is a config file; reconcile
  diffs it against what is running.
- **Config expressions** (`15`): computed values without arbitrary code.
- **Config layering** (`16`): bundles, profiles and patches fold into one tree,
  with provenance for every field.
- **Hot reload** (`17`): a consequence of unload being total.

Tier 3 — application patterns:

- **Scoped registration** (`18`): per-subject contributions in one process.
- **Capability seam** (`19`): Definition, Provider, Consumer — and nothing
  across. `Definition.of(ctx)` is the consumer's typed spelling.

Also shipped:

- `py.typed`; the package is checked with `mypy --strict` and has no runtime
  dependencies.
- The capability catalog in the sdist: 20 records, 145 normative rules, 126
  property cards.
- `docs/reference.md`, generated from the docstrings and the catalog.
- `examples/notes`, an application that boots from YAML and swaps a provider
  while it runs, asserted by `tests/test_example.py`.

[Unreleased]: https://github.com/ddebowczyk/cordis-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ddebowczyk/cordis-python/releases/tag/v0.1.0
