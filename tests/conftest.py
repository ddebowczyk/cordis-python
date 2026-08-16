"""Hypothesis profiles, mapped to the `test_tier` field of every property card.

The spec assigns each property card a tier: local, pr, nightly, release. The
profiles here are what those tiers mean in practice -- example counts, deadlines
and health checks -- so a card's declared tier translates directly into how hard
its test is exercised.

Two settings are deliberate and should not be relaxed casually:

* The example database is on and, in CI, persisted between runs. A failure
  found once is replayed on every later run without anyone having to transcribe
  a seed.
* ``derandomize`` stays off. Fresh entropy per run is the point of a campaign;
  a deterministic suite only ever re-tests what it already tested.

Per the review criteria in ~/projects/_kb-docs/property-based-testing, lowering
``max_examples`` after a failure, disabling the example database, or suppressing
a health check are all changes that need explicit human sign-off -- they hide
the failure rather than fix it.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Phase, Verbosity, settings

settings.register_profile(
    "local",
    max_examples=50,
    deadline=None,
    print_blob=True,
)

settings.register_profile(
    "pr",
    max_examples=200,
    deadline=None,
    print_blob=True,
)

settings.register_profile(
    "nightly",
    max_examples=2_000,
    deadline=None,
    print_blob=True,
    # Nightly runs are allowed to be slow; they are not allowed to be silently
    # sloppy, so data-generation health checks stay on.
    suppress_health_check=(HealthCheck.too_slow,),
)

settings.register_profile(
    "release",
    max_examples=20_000,
    deadline=None,
    print_blob=True,
    suppress_health_check=(HealthCheck.too_slow,),
    verbosity=Verbosity.normal,
)

settings.register_profile(
    "mutation",
    # For the mutation harness only. The question it asks is "does this test
    # fail at all against the defect its card names", so a handful of examples
    # is enough and shrinking is pure cost -- a mutant that makes an await hang
    # pays the timeout once per shrink attempt.
    # 25 rather than a handful: at ten, a three-value enum crossed with a
    # small set strategy never reached the interesting corner, and a mutant
    # that only bites there reads as a passing test.
    max_examples=25,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "local"))
