# control — operations control

Keeps the catalogue honest. This is the capability that knows what a capability
is: it validates every manifest, prints the catalogue, and runs the
repository-wide lanes.

```
just ops control validate            # the invariants, all of them
just ops control list                # the catalogue as a table
just ops control aggregate check     # every command marked aggregate: check
just ops control aggregate test      # every command marked aggregate: test
just ops control test                # the validator's own tests
```

## What `validate` actually enforces

Each rule exists because the alternative is a convention, and a convention is
something that holds until someone is in a hurry.

| rule | what it prevents |
| --- | --- |
| `schema` | a manifest that has drifted from the schema — checked with `check-jsonschema`, the same tool the library's own catalog uses |
| `id-mismatch` | a manifest whose `id` is not its directory name, so `just ops <id>` finds nothing |
| `missing-file` | a capability with no `justfile` (unrunnable) or no `README.md` (unexplained) |
| `overlapping-claim` | two capabilities claiming the same path, which is how two things quietly write the same file |
| `read-write-overlap` | a capability declaring a path as both `reads` and `owns`/`generates` — one of the two is a lie |
| `unowned-path` | a file under `ops/` no capability claims. New files get an owner on the day they appear, not eventually |
| `foreign-write` | a capability writing into a peer's territory |
| `peer-bin-reach` | a capability calling into a peer's `bin/` instead of through its `justfile`. Capabilities compose through commands, which are declared, not through scripts, which are not |
| `missing-capability`, `capability-cycle` | a `requires.capabilities` entry that does not exist, or a dependency cycle |
| `missing-recipe`, `argument-mismatch` | a declared command with no recipe, or with different parameters than declared |
| `undeclared-recipe` | a recipe nobody declared, so it has no summary and appears in no listing |
| `missing-provider`, `provider-interface` | `ops.yaml` selecting a capability that does not exist, or one that provides something else |
| `unselected-capability` | a capability nothing selects — dead weight, or a forgotten line in `ops.yaml` |

`aggregate` is the other half. A lane is not a list kept here; it is the set of
commands whose own manifests declare `aggregate: check` or `aggregate: test`.
Adding a gate is a one-line change in the capability that owns the gate.

## Ownership

`control` owns the catalogue itself: `ops/ops.yaml`, `ops/justfile`,
`ops/bin/**`, `ops/capabilities/**`, `ops/README.md`, and `docs/ops/**` — the
inventory and the design record, which describe this structure and would
otherwise be documentation with no owner.

It does not own the individual capabilities. It reads their manifests and
justfiles, and says when they are wrong.
