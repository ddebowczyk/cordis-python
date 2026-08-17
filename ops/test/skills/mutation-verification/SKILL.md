---
name: mutation-verification
description: Decide whether the cordis-python test suite actually holds a capability, and what to do when a mutation survives. Use when adding a property card, when a bug reaches a released artefact, or when `just ops test mutate` reports a SURVIVED result.
---

# Mutation verification

## The question this answers

A green suite proves the tests pass. It does not prove they would fail if the
code were wrong. A property that constrains nothing — one whose strategy
generates only inputs it happens to accept, or whose assertion restates the
implementation — is green forever and protects nothing.

The only way to tell the difference is to break the code and watch.

## Reading a result

- **CAUGHT** — the expected answer. The tests named in the mutation object to
  the defect.
- **SURVIVED** — the code was wrong and the suite said nothing. This is a
  finding about the *tests*, not about the mutation.
- **CAUGHT (hung)** — the defect is visible, but as a hang rather than a
  failure. Worth a look: a hang in CI costs the full timeout and gives a much
  worse diagnostic than an assertion.

## What to do about a survivor

Work in this order. The first two are usually right.

1. **The property is too weak.** The most common cause. The assertion holds for
   both the correct and the broken implementation — typically because it checks
   a shape (a length, a type, "no exception") rather than the behaviour the
   rule actually names. Strengthen it to say what the capability record says.

2. **The generator never reaches the case.** The property is fine but the
   strategy does not produce inputs that distinguish the two implementations —
   ordering mutations survive suites that only ever generate one disposer. Add
   the case; `just ops test discards` will tell you if the strategy is mostly
   being filtered away.

3. **The mutation is not actually a defect.** Sometimes the "broken" code is
   equivalent, or the rule genuinely does not constrain that direction. Then
   the mutation is wrong, not the suite — delete it from `mutations.yaml` and
   say in the campaign summary why that behaviour is unconstrained. Do this
   last, and be suspicious of reaching for it early: it is the option that
   makes the red go away without learning anything.

Never weaken the mutation to make it pass.

## When to add a mutation

Add one when a real defect escapes. Every entry in `mutations.yaml` today is a
bug that shipped or nearly shipped: the two `mappingproxy` defaults that made
`import cordis` fail on Python 3.11, the CPython-only attribute read that broke
every plugin mount on PyPy, the reference generator borrowing stdlib prose so
the checked-in file depended on which interpreter built it.

The discipline is: fix the bug, add the test that would have caught it, then
add the mutation that proves the test would have caught it. The last step is
the one that is easy to skip and the only one that verifies the second.

Ordering rules are worth mutating pre-emptively — disposal, shutdown, delivery
— because both directions run, only one is right, and nothing about the code
looks wrong when it is reversed.

## Keeping it honest

`before` must match exactly once. If it does not, the harness refuses to run
rather than testing nothing. When a refactor moves the code, update the
mutation — do not delete it because it stopped matching.
