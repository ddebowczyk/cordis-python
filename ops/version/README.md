# version — the authoritative version

```
just ops version show            # the version, where it lives, what the tags say
just ops version check           # everything that must be true right now
just ops version next patch      # preview a bump without writing it
just ops version set 0.2.0       # move it forward
just ops version verify-release  # HEAD carries the annotated tag for this version
just ops version sync            # fetch tags, so a local check sees what shipped
```

`check` is `aggregate: check`.

## One place

`cordis.__version__` is the version. `pyproject.toml` declares
`dynamic = ["version"]` and points hatch at that assignment, so the built
artefact cannot disagree with the source, and there is no second value to keep
in step. `check` fails if either of those two lines in `pyproject.toml` ever
changes, because the moment there are two declarations one of them is wrong.

## Forward only

`set` refuses a version that is not strictly greater than both the current one
*and* the latest release tag. This is the one rule here with no undo: an index
will not let you republish a name that already means something else, so a
repeat or a step backwards is a mistake you get to keep. The check is cheap;
the failure is permanent.

`check` also refuses a tag that is ahead of the declared version — that state
means a release was cut and the source never caught up — and requires a
`## [x.y.z]` changelog section to exist, because a version nobody can read
about is not a release.

## Why `generates` names a whole file

`set` writes exactly one line: the `__version__` assignment. Ownership claims
are per-path, so the honest statement in the manifest is the coarser one —
`src/cordis/__init__.py` — and this paragraph is the fine print. Nothing else
in that file is touched, and the substitution is anchored to the start of a
line so a mention inside a docstring cannot be mistaken for the declaration.

## `sync`

Tags are the record of what was published, and a checkout that has not fetched
them will happily approve a version that already exists on GitHub. Run this
before `check` if the local clone has been idle.
