"""PROP-EFFECT-001..008, transcribed from spec/capabilities/01-effect-scope.yaml.

Every resource in these tests is acquired and released against a
:class:`ResourceLedger`. That is the independence argument the cards make: the
assertions read what happened to the resources, never what the scope believes
it recorded, so a scope that drops a disposer from its own list still fails.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cordis.effect import EffectScope
from cordis.errors import InactiveScopeError, InvalidEffectError
from tests.support import EffectShape, ResourceLedger
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from tests.support import EffectSpec


class MarkerError(RuntimeError):
    """Carries the index of the effect or disposer that raised it."""

    def __init__(self, index: int) -> None:
        super().__init__(f"marker {index}")
        self.index = index


# --------------------------------------------------------------------------
# Turning an EffectSpec into a real effect
# --------------------------------------------------------------------------


def build_effect(
    ledger: ResourceLedger, spec: EffectSpec, index: int = 0
) -> Callable[[], object]:
    """Realise one generated spec as an effect function.

    Each shape acquires its resources through the ledger and hands back
    disposers that release them, so the same scenario description drives every
    supported form.
    """

    def sync_disposer() -> Callable[[], object]:
        return ledger.disposer(spec.resources[0])

    async def async_setup() -> Callable[[], object]:
        return ledger.disposer(spec.resources[0])

    def iterable() -> list[Callable[[], object]]:
        return [ledger.disposer(resource) for resource in spec.resources]

    def generator() -> Iterator[Callable[[], object]]:
        for resource in spec.resources:
            yield ledger.disposer(resource)

    async def async_generator() -> AsyncIterator[Callable[[], object]]:
        for resource in spec.resources:
            yield ledger.disposer(resource)

    @contextlib.contextmanager
    def manager() -> Iterator[None]:
        ledger.acquire(spec.resources[0])
        try:
            yield
        finally:
            ledger.release(spec.resources[0])

    @contextlib.asynccontextmanager
    async def async_manager() -> AsyncIterator[None]:
        ledger.acquire(spec.resources[0])
        try:
            yield
        finally:
            ledger.release(spec.resources[0])

    builders: dict[EffectShape, Callable[[], object]] = {
        EffectShape.NONE: lambda: None,
        EffectShape.SYNC_DISPOSER: sync_disposer,
        EffectShape.ASYNC_DISPOSER: async_setup,
        EffectShape.SYNC_CONTEXT: manager,
        EffectShape.ASYNC_CONTEXT: async_manager,
        EffectShape.ITERABLE: iterable,
        EffectShape.GENERATOR: generator,
        EffectShape.ASYNC_GENERATOR: async_generator,
        EffectShape.INVALID: lambda: 42,
    }
    del index  # only the failing-effect tests need it, and they build their own
    return builders[spec.shape]


async def register(scope: EffectScope, fn: Callable[[], object]) -> None:
    """Register and wait for setup, so the assertion sees a settled scope."""
    await scope.effect(fn)


plans = gen.effect_plans(max_size=6)


def clean(plan: tuple[EffectSpec, ...]) -> tuple[EffectSpec, ...]:
    """Strip the failure flags. PROP-EFFECT-001's domain excludes raising effects."""
    return tuple(
        replace(spec, raises_on_setup=False, raises_on_dispose=False) for spec in plan
    )


# --------------------------------------------------------------------------
# PROP-EFFECT-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(plan=plans)
async def test_disposing_a_scope_releases_every_resource(
    plan: tuple[EffectSpec, ...],
) -> None:
    """After dispose, no resource acquired through the scope is still live.

    Failure value: a shape-dispatch branch that treats an async generator's
    first yield as the whole effect, silently discarding every later-yielded
    disposer, so a composite registration half-leaks on every unload.
    """
    ledger = ResourceLedger()
    scope = EffectScope("root")

    for index, spec in enumerate(clean(plan)):
        await register(scope, build_effect(ledger, spec, index))

    expected = [r for spec in clean(plan) for r in spec.acquires]
    assert sorted(ledger.live) == sorted(expected)

    await scope.dispose()

    assert ledger.balanced
    assert set(ledger.counts().values()) <= {1}


# --------------------------------------------------------------------------
# PROP-EFFECT-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    count=st.integers(min_value=1, max_value=12),
    asyncness=st.lists(st.booleans(), min_size=12, max_size=12),
)
async def test_disposers_run_in_exact_reverse_of_registration(
    count: int, asyncness: list[bool]
) -> None:
    """Failure value: switching the internal store to a dict for dedup and
    iterating it forward, quietly turning LIFO into FIFO so a connection pool
    is closed before the tasks using it."""
    ledger = ResourceLedger()
    scope = EffectScope("root")
    registered: list[str] = []

    for index in range(count):
        resource = f"r{index}"

        def make(resource: str = resource) -> Callable[[], object]:
            ledger.acquire(resource)
            return lambda: ledger.release(resource)

        async def make_async(resource: str = resource) -> Callable[[], object]:
            ledger.acquire(resource)

            async def release() -> None:
                ledger.release(resource)

            return release

        await register(scope, make_async if asyncness[index] else make)
        registered.append(resource)

    await scope.dispose()

    assert ledger.release_order == tuple(reversed(registered))
    ledger.assert_unwound(registered)


# --------------------------------------------------------------------------
# PROP-EFFECT-003
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    plan=plans,
    repeats=st.integers(min_value=1, max_value=5),
    concurrent=st.booleans(),
)
async def test_dispose_is_idempotent_however_it_is_called(
    plan: tuple[EffectSpec, ...], repeats: int, concurrent: bool
) -> None:
    """Failure value: a `disposed` flag set at the end of the unwind rather
    than the start, so two concurrent disposals both pass the guard and every
    disposer runs twice -- the double-close that only appears under load."""
    ledger = ResourceLedger()
    scope = EffectScope("root")
    for index, spec in enumerate(clean(plan)):
        await register(scope, build_effect(ledger, spec, index))

    if concurrent:
        await asyncio.gather(*(scope.dispose() for _ in range(repeats)))
    else:
        for _ in range(repeats):
            await scope.dispose()

    assert ledger.balanced
    assert set(ledger.counts().values()) <= {1}


# --------------------------------------------------------------------------
# PROP-EFFECT-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    before=st.integers(min_value=0, max_value=3),
    yielded=st.integers(min_value=0, max_value=5),
)
async def test_a_failed_setup_releases_only_its_own_resources(
    before: int, yielded: int
) -> None:
    """Failure value: rollback that unwinds the whole scope instead of only
    the failing effect, tearing down a plugin's earlier, valid registrations
    because one late registration had a bad argument."""
    ledger = ResourceLedger()
    scope = EffectScope("root")

    good = [f"good{i}" for i in range(before)]
    for resource in good:
        await register(scope, partial(ledger.disposer, resource))
    nodes_before = len(scope.tree().children)

    def failing() -> Iterator[Callable[[], object]]:
        for i in range(yielded):
            yield ledger.disposer(f"partial{i}")
        raise MarkerError(99)

    with pytest.raises(MarkerError):
        scope.effect(failing, label="failing")

    assert ledger.live == set(good), "the failed effect released what it held"
    assert len(scope.tree().children) == nodes_before, "no record of the failed effect"

    await scope.dispose()
    assert ledger.balanced


@pytest.mark.tier_local
@given(yielded=st.integers(min_value=2, max_value=6))
async def test_a_failed_setup_unwinds_its_own_resources_in_reverse(
    yielded: int,
) -> None:
    """Rollback is a disposal, so PROP-EFFECT-002's order governs it too.

    Failure value: rolling a half-built effect forward instead of in reverse,
    releasing the connection it opened first while the cursor opened on top of
    that connection is still the thing being released.
    """
    ledger = ResourceLedger()
    scope = EffectScope("root")

    def failing() -> Iterator[Callable[[], object]]:
        for index in range(yielded):
            yield ledger.disposer(f"partial{index}")
        raise MarkerError(99)

    with pytest.raises(MarkerError):
        scope.effect(failing, label="failing")

    # Compared against the sequence this test issued, not against anything the
    # scope recorded: a rollback that drops a disposer fails here as well.
    ledger.assert_unwound([f"partial{index}" for index in range(yielded)])

    await scope.dispose()
    assert ledger.balanced


# --------------------------------------------------------------------------
# PROP-EFFECT-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(flags=st.lists(st.booleans(), min_size=1, max_size=8))
async def test_a_raising_disposer_does_not_strand_the_others(
    flags: list[bool],
) -> None:
    """Failure value: a try/except around the unwind loop rather than around
    each disposer, so the first failing teardown strands every resource
    registered before it and hides the remaining errors."""
    # Precondition "at least one disposer raises" is met by construction, not
    # by filtering: index 0 always raises.
    raising = [True, *flags]
    ledger = ResourceLedger()
    scope = EffectScope("root")

    for index, should_raise in enumerate(raising):
        resource = f"r{index}"

        def make(
            resource: str = resource,
            index: int = index,
            should_raise: bool = should_raise,
        ) -> Callable[[], object]:
            ledger.acquire(resource)

            def dispose() -> None:
                ledger.release(resource)
                if should_raise:
                    raise MarkerError(index)

            return dispose

        await register(scope, make)

    with pytest.raises(ExceptionGroup) as caught:
        await scope.dispose()

    seen = {
        exc.index for exc in caught.value.exceptions if isinstance(exc, MarkerError)
    }
    assert seen == {i for i, raises in enumerate(raising) if raises}
    assert ledger.balanced, "every disposer ran, including those behind a failure"
    assert set(ledger.counts().values()) == {1}


# --------------------------------------------------------------------------
# PROP-EFFECT-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(plan=plans, attempts=st.integers(min_value=1, max_value=4))
async def test_registering_on_a_disposed_scope_never_runs_the_effect(
    plan: tuple[EffectSpec, ...], attempts: int
) -> None:
    """Failure value: a late registration from a background task that survived
    teardown, silently acquiring a socket into a dead scope -- a leak with no
    owner and no diagnostic."""
    ledger = ResourceLedger()
    scope = EffectScope("root")
    for index, spec in enumerate(clean(plan)):
        await register(scope, build_effect(ledger, spec, index))

    await scope.dispose()

    calls = 0

    def spy() -> None:
        nonlocal calls
        calls += 1

    for _ in range(attempts):
        with pytest.raises(InactiveScopeError):
            scope.effect(spy)

    assert calls == 0
    assert ledger.balanced


# --------------------------------------------------------------------------
# PROP-EFFECT-007
# --------------------------------------------------------------------------

#: Values that are not valid effect results. Note the `min_size=1` on the list:
#: since an effect may return an iterable of disposers, an *empty* iterable is
#: a valid no-op effect, not a rejected one. Generating it here would assert
#: the opposite of the contract.
not_disposers = st.one_of(
    st.integers(),
    st.text(),
    st.lists(st.integers(), min_size=1),
    st.builds(object),
    st.builds(lambda: type("HasClose", (), {"close": 3})()),
    st.dictionaries(st.text(), st.integers(), min_size=1),
)


@pytest.mark.tier_local
@given(plan=plans, bad=not_disposers)
async def test_an_unsupported_shape_is_rejected_and_changes_nothing(
    plan: tuple[EffectSpec, ...], bad: object
) -> None:
    """Failure value: accepting a truthy non-callable as a disposer and calling
    it at teardown, turning an author's typo into a TypeError during unload --
    at the exact moment the process is trying to shut down cleanly."""
    ledger = ResourceLedger()
    scope = EffectScope("root")
    for index, spec in enumerate(clean(plan)):
        await register(scope, build_effect(ledger, spec, index))

    before = scope.tree()
    live_before = ledger.live

    with pytest.raises(InvalidEffectError):
        scope.effect(lambda: bad, label="bad")

    assert scope.tree() == before
    assert ledger.live == live_before

    await scope.dispose()
    assert ledger.balanced


# --------------------------------------------------------------------------
# PROP-EFFECT-008
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    shape=st.lists(
        st.tuples(st.booleans(), st.integers(min_value=0, max_value=2)),
        min_size=1,
        max_size=6,
    )
)
async def test_a_child_scope_unwinds_at_its_place_in_the_parents_order(
    shape: list[tuple[bool, int]],
) -> None:
    """Failure value: child scopes disposed after the parent's own disposers,
    so a child's background task outlives the connection its parent closed."""
    ledger = ResourceLedger()
    root = EffectScope("root")
    order: list[str] = []
    scopes: list[EffectScope] = [root]

    for step, (descend, effects) in enumerate(shape):
        current = scopes[-1]
        if descend:
            scopes.append(current.child(f"s{step}"))
            current = scopes[-1]
        for n in range(effects):
            resource = f"r{step}.{n}"
            await register(current, partial(ledger.disposer, resource))
            order.append(resource)

    await root.dispose()

    # The model: entries unwind LIFO within a scope, and a child scope unwinds
    # at the position it was created. Because every child is created at the
    # current innermost scope and effects only ever go into the innermost
    # scope, the registration order is itself the nesting order -- so plain
    # reversal is the expected disposal order.
    assert ledger.release_order == tuple(reversed(order))
    assert ledger.balanced


# --------------------------------------------------------------------------
# SEM-008: identity dedup
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_a_re_yielded_disposer_is_recorded_once() -> None:
    """A composite effect that re-yields an inner disposer must not double-close."""
    ledger = ResourceLedger()
    scope = EffectScope("root")
    shared = ledger.disposer("shared")

    def effect() -> Iterator[Any]:
        yield shared
        yield shared

    scope.effect(effect)
    await scope.dispose()

    assert ledger.counts()["shared"] == 1
