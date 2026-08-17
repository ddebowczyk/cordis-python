# packaging — package integrity

```
just ops packaging build     # wheel and sdist, from a clean dist/
just ops packaging verify    # what the artefacts are
just ops packaging smoke     # whether they work, on every supported interpreter
just ops packaging all       # build, verify, smoke
```

## The two questions

`verify` asks what the artefacts *are*: the wheel is `py3-none-any` (the
package is pure Python and must stay that way), it ships `py.typed` (the type
information is part of what is published), the sdist carries
`spec/capabilities/` (a downstream fork gets the contract, not just the code),
and `twine check` can render the metadata (a description that fails to render
is a broken page on the index, discovered after upload).

`smoke` asks whether they *work*: a fresh virtual environment per supported
interpreter — 3.11, 3.12, 3.13, pypy3.11 — the built wheel installed into it,
then an import and a plugin mount.

## Why `smoke` is not paranoia

Version 0.1.0 passed every gate that ran against the source tree and could not
be imported on the oldest interpreter it claimed to support. Python 3.11's
`dataclasses` rejects a `mappingproxy` default that 3.12 accepts, so
`import cordis` raised `ValueError` at class-definition time. A second defect
— reading `type(target).__weakrefoffset__`, which is a CPython implementation
detail PyPy does not define — broke every plugin mount on PyPy: 108 failures.

Both were found by installing the artefact, not by running the suite, on an
interpreter the developer was not using. That is the whole reason this command
exists, and it is why the smoke test mounts a plugin rather than stopping at
`import cordis`: an import alone would have sailed straight past the second
one.

`just ops test mutate portability` is the other half of that lesson — it
re-derives that the tests added afterwards still catch both defects.

## dist/

`dist/**` is under `generates`: derived output, removed and rebuilt by `build`,
never edited and never committed.
