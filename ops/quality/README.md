# quality — lint, format, types

```
just ops quality lint     # ruff rules and formatting, changing nothing
just ops quality yaml     # yamllint, in strict mode, over every YAML file
just ops quality types    # mypy, strict
just ops quality fix      # apply every safe fix and reformat
just ops quality all      # lint, yaml, then types
```

All three gates are `aggregate: check`, so `just check` runs them.

## What this capability owns, and what it does not

It owns *when* the tools run. It does not own *what they say* — the rule sets,
the strictness, the ignore list all live in `pyproject.toml`, where ruff and
mypy look for them and where an editor or a pre-commit hook will find the same
answer. Splitting a tool's configuration away from where the tool reads it
would create two descriptions of one policy, which is the failure this whole
directory exists to avoid.

## Settings worth knowing before you write code here

The selected rule sets are broad but specific, and two of the omissions bite:

- **`S` (bandit) and `PLR` are not enabled.** A `# noqa: S101` or
  `# noqa: PLR0911` is therefore an *unused* suppression, and `RUF100` fails
  the lint for it. Suppressions are only for rules that are actually on.
- **`TC003`** requires typing-only stdlib imports to sit inside
  `if TYPE_CHECKING:`.
- **`PT018`** forbids compound assertions: `assert a and b` must be two
  statements, so a failure says which half failed.

## YAML is a source language here, so it is linted like one

Capability manifests, the schemas themselves, the library's capability catalog
and the workflows are all YAML. `yamllint --strict` asks whether those files
parse and read consistently; `check-jsonschema` (in `ops/spec` and
`ops/control`) asks whether they *mean* the right thing. Neither substitutes
for the other, and the gap between them is real: the first run of this gate
found `summary: The full gate: lint then types.` in this capability's own
manifest — an unquoted colon, so the file was not a YAML document at all, and
no schema check would ever have seen it, because there was nothing to validate.

Settings live in `.yamllint.yaml`. Three rules are relaxed, each for a reason
written down there: `line-length` (records quote code that has its own line
budget), `truthy` (GitHub Actions spells its trigger `on:`, which YAML 1.1
reads as a boolean), and `braces` (`${{ matrix.python }}` is the expression
syntax Actions requires).

The linter is run through `uvx`, not a system install, so CI and a laptop get
the same version. `ys`/`ysv` do the same schema job as `check-jsonschema` and
are pleasant locally, but they arrive via cargo and Homebrew; a gate that
depends on them is a gate that has to be installed differently everywhere.

## Everything else

mypy runs in strict mode over `src`, `tests`, `examples` and `ops` — the
operations scripts are held to the same standard as the library, because they
are the things that decide whether a release happens.

The floor is Python 3.11 (`requires-python`, and ruff's `target-version`), so
PEP 695 type-parameter syntax is not available however new the local
interpreter is.
