# spec — specification

Validates the library's capability catalog: `spec/capabilities/*.yaml`, the
twenty records that state what Cordis must do, rule by rule, with a property
card for every MUST.

```
just ops spec check    # per-file schema, then cross-file consistency
just ops spec table    # the catalog as a build-order table
```

`check` is `aggregate: check`.

## Two checks, not one

Per file, `check-jsonschema` holds each record to
`spec/schema/capability.v1.yaml` — shape, required fields, vocabulary.

Across files, `spec/check_spec.py` asks the questions a schema cannot: does
every `depends_on` resolve, does every capability sit at a tier above the ones
it depends on, does every piece of evidence point at something that exists, and
does every normative MUST have a property card that covers it. A catalog can be
perfectly well-formed and still describe a build order that cannot be built.

## Why `spec/` is read, not owned

`spec/` is not an operations artefact. It is the contract, it ships in the
sdist, and a downstream fork gets it along with the code — `ops/packaging`
verifies exactly that. This capability runs the checks; it does not author what
it checks, and the manifest says so by listing `spec/**` under `reads`.

The schema is JSON Schema 2020-12 written in YAML. `ys -f
spec/schema/capability.v1.yaml <file>` checks a single record when that is
closer to hand than the whole gate.
