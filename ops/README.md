# Repository operations

Everything this repository can do to itself — lint, test, validate the
capability catalog, generate the reference, move the version, build and check
the artefacts, cut a release — lives here, split into capabilities that own
their own entry points.

A capability is one directory. It carries a manifest that says what it is for
and what it touches, a `justfile` that is the only way to run it, a `README.md`
that explains the decisions behind it, and whatever else it needs: `bin/`,
`schema/`, `skills/`, `tests/`.

```
ops/
  ops.yaml                 which capability provides which interface
  justfile                 the catalogue's entry point
  bin/ops.py               the validator
  capabilities/schema/     the schema every manifest is held to
  <capability>/
    capability.yaml        the manifest
    justfile               the commands
    README.md              why it is the way it is
    bin/ schema/ skills/ tests/
```

## Why it is split this way

The operations existed before this directory did — fifteen recipes in one root
`justfile`, ten jobs in two workflow files, and four operations that lived
nowhere and were done by hand. `docs/ops/inventory.md` is the record of that
state, taken before anything moved.

Nothing there was disorganised. What was missing was **ownership**: no way to
ask who is responsible for `docs/reference.md`, no way to notice that the local
`ci` recipe had quietly stopped matching the workflow it claimed to mirror, and
nowhere obvious to put a release operation, so the release operations were not
written at all. The gaps clustered exactly where ownership was vaguest.

So ownership is written down, and then checked. Each manifest declares:

| field | meaning |
| --- | --- |
| `owns` | files this capability is responsible for. Exactly one capability may claim a path. |
| `reads` | files it consumes but must not write. |
| `generates` | files it produces, which are derived and never hand-edited. |
| `commands` | every recipe in its `justfile`, with a summary and a lane. |
| `requires` | tools that must be on PATH, and capabilities it depends on. |
| `skills` | judgement that cannot be automated, written for whoever has to exercise it. |

`ops/bin/ops.py validate` enforces the rest: no two capabilities claim the same
path, every file under `ops/` is claimed by exactly one, nothing writes into a
peer's territory or reaches into a peer's `bin/`, the dependency graph is
acyclic and resolvable, every declared command exists as a recipe with the
declared arguments — and every recipe is declared, so a command cannot appear
without a summary.

## Running things

```
just ops <capability> <command>     # from the repository root
just --justfile ops/justfile list   # the catalogue as a table
```

The familiar aliases still work — `just check`, `just test`, `just lint`,
`just docs`, `just ci` — and now delegate rather than duplicate.

Two commands run across capabilities:

- `just ops control aggregate check` runs every command marked
  `aggregate: check`. That is what `just check` is.
- `just ops control aggregate test` runs every command marked `aggregate: test`.

A capability joins a repository-wide lane by declaring it in its own manifest.
There is no second list to update.

## `ops.yaml`

```yaml
active:
  documentation: docs
  distribution: release
  ...
```

One provider per interface. The file selects; it does not dispatch. If the
documentation capability were ever replaced, this is the line that changes and
the callers do not.

## Adding a capability

1. `mkdir ops/<id>` and write `capability.yaml`, `justfile`, `README.md`.
2. Claim your paths in `owns`. Claim only what you are responsible for.
3. Declare every recipe under `commands`, with a lane, and an `aggregate` if it
   belongs in a repository-wide gate.
4. Add the interface to `ops/ops.yaml`.
5. `just ops control validate` until it is quiet.

The `repository-operations` skill in `ops/control/skills/` covers the parts
that need judgement rather than steps.
