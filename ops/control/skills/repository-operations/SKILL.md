---
name: repository-operations
description: Add or change a repository operation in cordis-python without leaving it unowned. Use when adding a just recipe, a script under ops/, a CI step, or a new capability directory, and when ops validate reports overlapping-claim, unowned-path, foreign-write or peer-bin-reach.
---

# Adding a repository operation

The mechanical part is in `ops/README.md`. This is the part that needs
judgement.

## Where does it go?

Ask what the operation is *responsible for*, not what it uses. A command that
regenerates the reference belongs to `docs` even though it runs Python; a
command that checks the version belongs to `version` even though it reads
`CHANGELOG.md`, which `release` also reads. Reading is shared. Writing is not.

If two capabilities both plausibly own it, that is usually a sign the
capability boundary is in the wrong place, not that ownership should be shared.
Move the boundary.

If none does, and it is more than one command, it is a new capability. If it is
one command with no home, it usually belongs to whichever capability *reads*
what it produces.

## Deciding `owns` vs `reads` vs `generates`

- `generates` means **derived**: this command produces it, nobody edits it by
  hand, and deleting it loses nothing. `docs/reference.md`, `dist/**`.
- `owns` means **responsible**: you may write it, and no one else may.
- `reads` means **consumed**: you depend on it and must not write it.

Two traps:

- A capability that lints or tests the whole tree reads everything, including
  its own files. Do not list the tree in `reads` — the validator will
  (correctly) call that a `read-write-overlap` with what you own. List what you
  consume from *outside* your own territory.
- A command that edits a file and puts it back is a transaction, not
  ownership. The mutation harness rewrites `src/` and restores it; `src/` stays
  under `reads`, and the README says why. Ownership is about who consumes the
  result, and nobody consumes a file that no longer exists.

## Composing with another capability

Call its `justfile`, never its `bin/`:

```
@just --justfile ops/version/justfile --working-directory . check
```

A command is declared, summarised, and checked by the validator. A script is a
path that can be renamed. This is what `peer-bin-reach` is protecting.

If you need a *value* from another capability rather than an action, prefer its
public output over either — `release` reads the version by importing `cordis`,
which is a contract, rather than by parsing what `version` parses.

## Joining a repository-wide lane

Add `aggregate: check` (or `aggregate: test`) to the command in your own
manifest. There is no central list. If you find yourself editing another
capability's file to make your gate run, stop — that is the pattern this
structure exists to remove.

## Before you finish

`just ops control validate` until it is quiet, then `just check`. A rule that
fires is not an obstacle to route around: every one of them names a specific
way that operations previously went wrong here.
