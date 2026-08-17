# workflow — local delivery

The toolchain this repository needs, and the single description of CI.

```
just ops workflow doctor     # every declared tool, and whether this machine has it
just ops workflow sync       # install or refresh the development environment
just ops workflow ci         # what CI runs, in CI's order, on this machine
just ops workflow ci-check   # fail if the local lane and the workflow have drifted
```

`ci-check` is `aggregate: check`.

## doctor

The list of required tools is not kept here. It is derived from every
`capability.yaml`'s `requires.tools`, so a capability that starts needing `gh`
is reported the moment its manifest says so, and there is no second list of
prerequisites for anyone to forget. The output names the tool, where it was
found, and which capabilities need it.

This was one of the operations that lived nowhere before: the inventory found
that `ruff`, `mypy` and `check-jsonschema` are reached through `uv`/`uvx` while
`just`, `yq`, `gh` and `uv` must already be installed, and nothing anywhere
said so.

## One CI, described twice

`just ops workflow ci` and `.github/workflows/ci.yml` are two renderings of one
list. The local recipe delegates to capability commands; the workflow invokes
the same commands as `just ops <capability> <command>`.

`ci-check` compares them, and fails if either list contains a step the other
does not, or a command no manifest declares. This is not hypothetical: the
previous root `justfile` had a `ci` recipe whose comment read "everything CI
runs, in CI's order" while omitting a whole job, because the two descriptions
were written separately and nothing ever compared them.

The comparison is textual and deliberately narrow — it matches the delegation
syntax on one side and the invocation syntax on the other. If the spelling of
either changes, `ci-check` fails with "no delegations found" rather than
silently passing. A drift check that can quietly match nothing is worse than
none.

## Ownership

`workflow` owns `.github/workflows/**` and the root `justfile`: the two places
where operations are *invoked* rather than defined. Both are thin — the root
`justfile` is aliases and delegation, and the workflow is checkout, `uv`, and
`just ops ...` — because anything with logic in it belongs in the capability
that owns the logic.
