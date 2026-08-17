---
name: cutting-a-release
description: Take a cordis-python version from green to published without skipping what the tag claims. Use when preparing a release, choosing a version number, writing a changelog section, or deciding what to do when a gate fails after the tag exists.
---

# Cutting a release

## What the tag claims

*These gates passed, this changelog describes it, these artefacts came from
this commit.* Everything below is in service of not making that claim falsely.
A tag is public the moment it is pushed, and an index will never let you reuse
a version number, so the asymmetry is total: checking costs minutes, being
wrong is permanent.

## The order

1. `just ops version next <part>` — see what the number would be.
2. `just ops version set <version>` — it refuses anything not strictly ahead of
   both the current version and the latest tag.
3. Write the `## [x.y.z]` changelog section. This *is* the release notes;
   `notes` extracts it verbatim.
4. Commit. `just ops release gate` — every gate the tag will claim.
5. `just ops release status` — the preconditions, each answered.
6. `just ops release publish` (plan), then `--confirm`.
7. `just ops release verify`.

Steps 4 and 5 are different questions. `gate` runs the work; `status` asks
whether the *state* is releasable — clean tree, tag on HEAD, artefacts built,
`gh` authenticated, nothing already published under that tag.

## Choosing the number

Pre-1.0, the honest rule for this package: a change that would make an existing
`import cordis` program behave differently is a minor bump, not a patch. The
public surface is checked by `tests/test_public_api.py`, so a diff there is the
signal. "It is only a bug fix" is not a defence when the fix changes an
observable ordering.

## Writing the section

Write what someone upgrading needs in order to decide whether to. Not the
commit list: they have that. Anything that changes behaviour, however small,
goes in — the 0.1.0 portability defects were one line of code each, and each
one made the package unusable on a supported interpreter.

## When a gate fails

**Before the tag exists** — fix it, no ceremony. Nothing has been claimed.

**After the tag exists but before the release is published** — delete the tag,
fix, re-tag: `git tag -d v0.2.0 && git push --delete origin v0.2.0`. Awkward,
and much less awkward than the alternative.

**After the release is published** — do not delete it and do not retag. Someone
may already have it. Fix forward with a new patch version, and say in its
changelog section what was wrong with the previous one. A version that was
published is a fact, and the record should match the facts.

## What is not automated, on purpose

`publish` prints its plan and stops until it is given `--confirm`. Read the
plan. It is the last moment at which the version, the notes, and the artefact
list are all in front of you at once.

Nothing publishes to PyPI. That is a separate decision with its own
credentials, and it is not wired up — if it should be, trusted publishing from
the release workflow is the shape to build, not an API token in a script.
