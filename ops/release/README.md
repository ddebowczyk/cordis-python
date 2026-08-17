# release — distribution

Cutting a GitHub Release, and refusing to cut one that would not be true.

```
just ops release status         # every precondition, answered yes or no
just ops release notes 0.2.0    # the changelog section, as the release body
just ops release gate           # every gate the tag will claim passed
just ops release publish        # print the plan; add --confirm to do it
just ops release verify         # the published release is real and complete
```

## What a release claims

A tag says: *these gates passed, this changelog describes it, these artefacts
came from this commit*. Every command here keeps one part of that promise
checkable before it is made.

`status` never changes anything and answers seven questions: the version, a
changelog section with a body, a clean working tree, an annotated tag pointing
at HEAD, both artefacts present for this version, an authenticated `gh`, and no
release already published under that tag.

`gate` runs the work: spec, quality, docs, version, the release-tier test
campaign, and packaging's build-verify-smoke. It is kept here rather than only
in CI so a release can be rehearsed on a laptop before the tag exists.

`publish` is the only command in the whole `ops/` tree that changes anything
outside the checkout. Run bare, it prints exactly what it would do and stops;
`just ops release publish --confirm` is what creates the tag and the release.
Nothing here publishes to an index — PyPI is a separate decision and is not
wired up.

## Notes come from the changelog

`notes` extracts the `## [x.y.z]` section verbatim and hands it to
`gh release create --notes-file -`. Release notes written separately from the
changelog become a second account of the same work, and second accounts drift.
This way the changelog is the release notes, and the requirement that a section
exists is already enforced by `just ops version check`.

## Dependencies

`requires.capabilities` names all six: `version`, `packaging`, `quality`,
`test`, `docs`, `spec`. That is not decoration — `ops/bin/ops.py` resolves the
graph, so a release cannot depend on a capability that has been removed or
renamed without the validator saying so.

The composition goes through *commands*, never through peer scripts. `gate`
delegates to each capability's `justfile`; `release.py` reads the version by
importing the package rather than by reaching into `ops/version/bin/`. The
validator enforces that too (`peer-bin-reach`), because a command is declared
and a script is not.

The `cutting-a-release` skill covers the judgement: what to do when a gate
fails late, and what is not worth releasing.
