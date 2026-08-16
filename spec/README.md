# The capability catalog

This directory is the contract. The code in `src/` implements it; the tests in
`tests/` are transcribed from it. When the two disagree, this is what is right —
or this is what gets changed first, deliberately, in its own commit.

```
schema/capability.v1.yaml    the versioned record format
capabilities/*.yaml          one record per capability, in build order
check_spec.py                cross-file consistency and coverage
```

## Reading a record

| Field | What it answers |
|---|---|
| `problem` | What breaks without this capability. Written before the design, so the design can be judged against it. |
| `origin` | Where this lives in the TypeScript original — file and symbol, so a semantic question has an authority. |
| `semantics` | Numbered normative rules (`SEM-001`, …), each independently citable. This is the actual contract. |
| `python_design` | The idiomatic Python realisation: module, public surface, which Python mechanism replaces which JS mechanism, typing and concurrency obligations. |
| `properties` | Property cards. One falsifiable claim each. |
| `depends_on` | Build order. Generates the task graph in `bd`. |
| `open_questions` | Decisions deliberately deferred, with the tradeoff stated. |

A `semantics` entry may carry a `deviation` block. That marks a rule where this
port intentionally differs from upstream, with the reasoning — for example
requiring explicit entry ids rather than generating them, or replacing arbitrary
JavaScript in config files with a restricted evaluator. Absence of the block
means "same as upstream".

## Reading a property card

Each card follows the structure in `~/projects/_kb-docs/property-based-testing`:

- **`claim`** — one sentence, falsifiable, universally quantified over the
  domain. If it needs an "and", it is two properties.
- **`shape`** — roundtrip, differential, idempotency, safety, conservation,
  monotonicity, state-machine, or metamorphic.
- **`evidence`** — the `SEM-*` rules the claim is derived from. A rule nothing
  cites is a coverage gap; `check_spec.py` reports it.
- **`domain`** — what is generated and, explicitly, what is excluded. An
  unstated exclusion is an untested region. `strategy_hint` prefers constructing
  valid values directly over filtering, so discard rates stay low.
- **`oracle`** — how correctness is decided, plus **`independence`**: why the
  oracle cannot fail the same way the implementation does. "Calls the same
  function" is not an oracle.
- **`failure_value`** — a concrete defect this property catches. Not a
  restatement of the claim. This is the acceptance criterion for the test
  itself: introduce that defect deliberately, and if the test still passes, the
  test is wrong.
- **`test_tier`** — local, pr, nightly, or release. Maps to the Hypothesis
  profiles in `tests/conftest.py` and to pytest markers.

## Validating

```
just spec-check
```

Two layers. `ys` checks each file against `schema/capability.v1.yaml` — required
fields, id patterns, closed enums. `check_spec.py` checks what a per-file schema
cannot: that `depends_on` resolves, that dependencies never point up a tier, that
the dependency graph is acyclic, that property ids are globally unique, that
every `evidence` reference names a rule the same record defines, and that every
`MUST` is cited by at least one property card.

Coverage gaps are reported as **warnings**, not errors. A rule may legitimately
be enforced structurally rather than by a test — that packaging keeps a
dependency out of the kernel, that a mechanism is explicitly *not* a security
boundary, that a derivation is O(1). Each remaining warning should be a decision
someone made, not a card someone forgot.

## Changing the spec

The record changes first, in its own commit, with the reasoning in the
`deviation` or `open_questions` field. Then the tests, then the code.

Changing a test and the contract it checks in the same patch is the review red
flag this workflow exists to prevent: it makes a failing property indistinguishable
from a revised one.

## Versioning

`schema_version` is a constant in every record (`capability/v1`) and selects the
schema file. A breaking change to the record format means a new
`capability.v2.yaml` and a migration of the records — not an edit to v1 that
silently reinterprets what is already written.
