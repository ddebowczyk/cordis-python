# docs — documentation

Everything the repository does to keep its documentation true.

```
just ops docs build       # regenerate docs/reference.md from the code
just ops docs check       # fail if the checked-in reference is stale
just ops docs counters    # the numbers the README claims, against the catalog
just ops docs readme      # run the README's quickstart as a reader would
just ops docs all         # check, counters, readme
```

`check` and `counters` are `aggregate: check`.

## Generated, then checked

`docs/reference.md` is derived from the docstrings and the capability records.
It is checked in — a reader should not have to run anything to read it — and
`check` fails if it no longer matches the code, so a renamed export cannot
leave the reference describing the old name.

That gate has one requirement that is easy to miss: **the generator's output
must not depend on the interpreter that ran it.** It once did, and CI went red
for it. Three separate leaks, all the same shape — the generated file borrowing
prose or signatures from the standard library:

- a name bound to a stdlib object documented *that object*: `PluginTarget` is
  `object`, so the reference carried "The base class of the class hierarchy" on
  CPython and "The most base type" on PyPy;
- `SETTLED` is a `frozenset`, so it inherited "Build an immutable unordered
  collection of unique elements";
- an enum rendered as its constructor, which `inspect` reports as `(*values)`
  on 3.13 and as a long lookup signature on 3.11.

The fix removed 62 lines of borrowed prose, and `tests/test_reference.py` holds
it in place. `just ops test mutate documentation` re-derives that those tests
still catch the regression.

## Counters

The README opens with counts: capabilities implemented, normative rules,
property cards. Nothing about adding a capability record forces anyone to edit
that sentence, so the sentence is checked rather than maintained —
`bin/counters.py` recomputes each number from `spec/capabilities/` and compares.

If a claim's wording moves, the check fails loudly rather than silently
matching nothing. A check that quietly stops covering something is worse than
no check.

## Ownership

`docs/reference.md` is under `generates`: derived, never hand-edited.
`docs/build_reference.py` is only *read* — it lives in `docs/` because that
directory ships, and this capability runs it rather than owning it.
