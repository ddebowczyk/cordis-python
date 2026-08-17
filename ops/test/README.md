# test — the property-test tiers, and their verification

```
just ops test fast              # local tier: 50 examples
just ops test pr                # 200 examples, everything but the release tier
just ops test nightly           # 2000 examples, every marker
just ops test release           # 20000 examples
just ops test replay <selector> # re-run one test against the recorded failures
just ops test discards          # generator health, worst first
just ops test campaigns         # the declared mutation campaigns
just ops test mutate <campaign> # break the code on purpose; require a failure
```

`fast` is `aggregate: test`.

## Tiers

A tier is one Hypothesis profile and one marker selection. The profiles are
registered in `tests/conftest.py`; the markers (`tier_local`, `tier_pr`,
`tier_nightly`, `tier_release`) mirror the `test_tier` field of every property
card in `spec/capabilities/`, so a card's declared cost is what actually
decides when its test runs.

`filterwarnings = ["error"]` is on: a warning is a failure, in every tier.

## `discards`

Read it as a trend, not as a filter rate. Hypothesis counts its own overruns
and duplicate draws as "invalid", so a strategy containing no filter at all
still reports 10–20%. A number well above its neighbours is the one worth
chasing.

## Mutation verification

A green suite says the tests pass. It does not say they would fail if the code
were wrong, and those are different claims — a property that constrains nothing
stays green forever.

`mutations.yaml` declares campaigns of real defects. `bin/mutate.py` applies
each one, runs the tests that are supposed to notice, and requires them to
fail. A **survivor** is a hole in the suite and fails the command.

Three campaigns today:

| campaign | what it covers |
| --- | --- |
| `portability` | the three defects that shipped in 0.1.0 and were found by installing the wheel rather than running the suite: two `mappingproxy` dataclass defaults that Python 3.11 refuses, and a CPython-only `__weakrefoffset__` read that broke every plugin mount on PyPy |
| `documentation` | the reference generator borrowing stdlib prose or enum signatures, which made the checked-in file depend on the interpreter that generated it |
| `core` | ordering rules that run in either direction and are only correct in one: effect disposal, child-fiber shutdown, listener delivery |

Two design points:

- **`before` must occur exactly once.** If a refactor moves the code, the
  harness stops and says so. A mutation that silently matches nothing is a
  green result that means nothing.
- **A hang counts as caught, and is reported as such.** The run is killed by
  process group after five minutes — pytest and Hypothesis both spawn children,
  and signalling the leader alone leaves them behind.

The harness edits files under `src/` and `docs/` and restores them in a
`finally`. That is a transaction, not ownership, which is why those paths stay
under `reads` in the manifest: nothing here produces a file anyone else should
consume.

The `mutation-verification` skill covers what to do about a survivor, which is
the part that needs judgement.
