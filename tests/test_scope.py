"""PROP-SCOPE-001..006, from spec/capabilities/18-scoped-registration.yaml.

The model is one relation, `Plan.covers`, read off the generated scope tree:
"owner is the subject, or an ancestor of it". Visibility asks it about the
registrant and the querier; event admission asks it about the listener and the
carrier. The runtime is never consulted for ancestry -- which matters here more
than anywhere else in this port, since the failure PROP-SCOPE-002 names is
exactly a runtime that answers the same question with the sign flipped.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.diagnostics import inspect, walk
from cordis.events import Emit, EventBus
from cordis.filter import with_filter
from cordis.plugin import PluginHost, scope_of
from cordis.scope import (
    ScopedRegistry,
    admits,
    create_scope,
    scope_target,
    subject_of,
)
from tests.support.clock import VirtualClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cordis.context import Context
    from cordis.scope import Scope

#: How many turns of the driver a generated disposal may take before the test
#: calls it a hang rather than waiting for the suite's timeout.
TURNS = 400


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------

# A site is where a registration or a dispatch happens: a scope index, or
# `None` for the unscoped context every scope was ultimately created from.


@dataclass(frozen=True)
class Plan:
    """A scope tree: `parents[i]` is scope `i`'s parent, or None if unscoped."""

    parents: tuple[int | None, ...]

    def sites(self) -> tuple[int | None, ...]:
        return (None, *range(len(self.parents)))

    def path(self, site: int | None) -> tuple[int, ...]:
        """``site`` and its ancestors, nearest first."""
        walk: list[int] = []
        while site is not None:
            walk.append(site)
            site = self.parents[site]
        return tuple(walk)

    def covers(self, owner: int | None, subject: int | None) -> bool:
        """Whether ``owner`` is ``subject`` or an ancestor of it.

        An unscoped owner covers everything: the absence of a scope means
        "everywhere", not "nowhere" (SEM-004).
        """
        if owner is None:
            return True
        return owner in self.path(subject)

    def subtree(self, root: int) -> frozenset[int]:
        return frozenset(
            index for index in range(len(self.parents)) if root in self.path(index)
        )


@st.composite
def plans(draw: st.DrawFn, *, max_scopes: int = 5) -> Plan:
    count = draw(st.integers(min_value=1, max_value=max_scopes))
    # Each scope is created either from the unscoped context or from a scope
    # that already exists, which is the only way to build one.
    parents: list[int | None] = [
        draw(st.sampled_from([None, *range(index)])) for index in range(count)
    ]
    return Plan(tuple(parents))


def sites_of(plan: Plan) -> st.SearchStrategy[int | None]:
    return st.sampled_from(list(plan.sites()))


# --------------------------------------------------------------------------
# The world
# --------------------------------------------------------------------------


@dataclass
class World:
    """A plan, built: a host, one scope per node, and a bus."""

    host: PluginHost
    bus: EventBus
    scopes: list[Scope] = field(default_factory=list)
    calls: list[int] = field(default_factory=list)

    def ctx(self, site: int | None) -> Context:
        return self.host.root.context if site is None else self.scopes[site].ctx

    def scope(self, site: int | None) -> Scope | None:
        return None if site is None else self.scopes[site]


async def build(plan: Plan) -> World:
    world = World(host=PluginHost(), bus=EventBus())
    for index, parent in enumerate(plan.parents):
        world.scopes.append(
            create_scope(world.ctx(parent), object(), label=f"s{index}")
        )
    await world.host.runtime.quiesce()
    return world


def listener(world: World, index: int) -> Callable[..., None]:
    def record(*_args: object, **_kwargs: object) -> None:
        world.calls.append(index)

    record.__qualname__ = f"listener#{index}"
    return record


TOPIC: Emit[[]] = Emit("topic")


# --------------------------------------------------------------------------
# PROP-SCOPE-001
# --------------------------------------------------------------------------


@st.composite
def visibility_cases(draw: st.DrawFn) -> tuple[Plan, tuple[int | None, ...]]:
    plan = draw(plans())
    owners = draw(st.lists(sites_of(plan), min_size=1, max_size=6))
    return plan, tuple(owners)


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=visibility_cases())
async def test_a_contribution_is_visible_down_the_scope_chain_and_no_further(
    case: tuple[Plan, tuple[int | None, ...]],
) -> None:
    """PROP-SCOPE-001: visible to the registrant's scope and its descendants.

    Failure value: visibility computed as "same scope only", so a sub-agent
    silently loses every tool its parent registered and the failure looks like
    a missing capability rather than a scoping bug.
    """
    plan, owners = case
    world = await build(plan)
    registry: ScopedRegistry[str] = ScopedRegistry()
    for index, owner in enumerate(owners):
        registry.register(f"item{index}", ctx=world.ctx(owner))

    for querier in plan.sites():
        expected = tuple(
            f"item{index}"
            for index, owner in enumerate(owners)
            if plan.covers(owner, querier)
        )
        assert registry.visible(ctx=world.ctx(querier)) == expected


# --------------------------------------------------------------------------
# PROP-SCOPE-002
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reg:
    """One listener: the scope it registers at, and whether it opts out."""

    site: int | None
    global_: bool


@st.composite
def admission_cases(draw: st.DrawFn) -> tuple[Plan, tuple[Reg, ...], int | None]:
    plan = draw(plans())
    regs = draw(
        st.lists(
            st.builds(Reg, site=sites_of(plan), global_=st.booleans()),
            min_size=1,
            max_size=6,
        )
    )
    return plan, tuple(regs), draw(sites_of(plan))


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=admission_cases())
async def test_an_event_reaches_the_carriers_scope_and_every_ancestor(
    case: tuple[Plan, tuple[Reg, ...], int | None],
) -> None:
    """PROP-SCOPE-002: admission runs *up* the chain, the opposite of visibility.

    Failure value: admission computed downward instead of upward, so a
    supervisor never sees its sub-agent's events while every sibling session
    sees them -- a correctness bug and a data-leak bug in one sign error.
    """
    plan, regs, carrier = case
    world = await build(plan)
    for index, reg in enumerate(regs):
        world.bus.through(world.ctx(reg.site)).on(
            TOPIC,
            listener(world, index),
            scope=scope_of(world.ctx(reg.site)),
            global_=reg.global_,
        )

    await world.bus.through(world.ctx(carrier)).emit(TOPIC)

    assert world.calls == [
        index
        for index, reg in enumerate(regs)
        if reg.global_ or plan.covers(reg.site, carrier)
    ]


# --------------------------------------------------------------------------
# PROP-SCOPE-003
# --------------------------------------------------------------------------


@st.composite
def teardown_cases(
    draw: st.DrawFn,
) -> tuple[Plan, tuple[int | None, ...], tuple[int, ...]]:
    plan = draw(plans())
    owners = draw(st.lists(sites_of(plan), min_size=1, max_size=6))
    order = draw(
        st.lists(st.integers(min_value=0, max_value=len(plan.parents) - 1), max_size=3)
    )
    return plan, tuple(owners), tuple(order)


@pytest.mark.tier_pr
@settings(max_examples=200, deadline=None)
@given(case=teardown_cases())
async def test_disposing_a_scope_takes_everything_registered_through_it(
    case: tuple[Plan, tuple[int | None, ...], tuple[int, ...]],
) -> None:
    """PROP-SCOPE-003: gone from the unfiltered view, and off the bus.

    Failure value: session teardown that removes the scope tag but not the
    registrations, so a long-running process accumulates the tools and
    listeners of every session it has ever served.
    """
    plan, owners, order = case
    world = await build(plan)
    registry: ScopedRegistry[str] = ScopedRegistry()
    for index, owner in enumerate(owners):
        registry.register(f"item{index}", ctx=world.ctx(owner))
        world.bus.through(world.ctx(owner)).on(
            TOPIC,
            listener(world, index),
            scope=scope_of(world.ctx(owner)),
            global_=True,  # global: what is checked here is removal, not routing
        )

    gone: set[int] = set()
    for target in order:
        await world.scopes[target].dispose()
        await world.host.runtime.quiesce()
        gone |= plan.subtree(target)

        live = tuple(
            f"item{index}" for index, owner in enumerate(owners) if owner not in gone
        )
        # The unfiltered view: a bug that merely hides entries from filtered
        # queries while leaving them registered still fails here.
        assert registry.all() == live

        world.calls.clear()
        await world.bus.emit(TOPIC)
        assert world.calls == [
            index for index, owner in enumerate(owners) if owner not in gone
        ]


# --------------------------------------------------------------------------
# PROP-SCOPE-004
# --------------------------------------------------------------------------


async def drive(task: asyncio.Task[None], clock: VirtualClock) -> None:
    """Run ``task`` to completion on logical time, one deadline at a time."""
    for _ in range(TURNS):
        await clock.drain()
        if task.done():
            await task
            return
        if clock.pending:
            await clock.advance_to(clock.pending[0])
        else:
            await asyncio.sleep(0)
    task.cancel()
    raise AssertionError("the disposal never finished")


@st.composite
def timing_cases(draw: st.DrawFn) -> tuple[Plan, tuple[float, ...]]:
    plan = draw(plans())
    durations = draw(
        st.lists(
            st.sampled_from([0.0, 1.0, 2.0, 5.0]),
            min_size=len(plan.parents),
            max_size=len(plan.parents),
        )
    )
    return plan, tuple(durations)


@pytest.mark.tier_pr
@settings(max_examples=100, deadline=None)
@given(case=timing_cases())
async def test_a_scope_finishes_only_after_every_scope_beneath_it(
    case: tuple[Plan, tuple[float, ...]],
) -> None:
    """PROP-SCOPE-004: descendants first, and completion is awaitable.

    Failure value: a parent session closing its transport while a sub-agent is
    still flushing its final message, losing the last turn of every nested
    conversation.
    """
    plan, durations = case
    world = await build(plan)
    clock = VirtualClock()
    finished: list[int] = []

    for index, scope in enumerate(world.scopes):
        scope_of(scope.ctx).effect(
            flusher(clock, durations[index], index, finished), f"flush#{index}"
        )

    roots = [index for index in range(len(plan.parents)) if plan.parents[index] is None]
    for root in roots:
        task = asyncio.ensure_future(world.scopes[root].dispose())
        await drive(task, clock)

    assert sorted(finished) == list(range(len(plan.parents)))
    place = {index: position for position, index in enumerate(finished)}
    for index in range(len(plan.parents)):
        for ancestor in plan.path(index)[1:]:
            assert place[index] < place[ancestor], "an ancestor finished first"


def flusher(
    clock: VirtualClock, duration: float, index: int, finished: list[int]
) -> Callable[[], Callable[[], Awaitable[None]]]:
    """An effect whose teardown takes time and says when it is done."""

    def start() -> Callable[[], Awaitable[None]]:
        async def flush() -> None:
            await clock.sleep(duration)
            finished.append(index)

        return flush

    return start


# --------------------------------------------------------------------------
# PROP-SCOPE-005
# --------------------------------------------------------------------------


@pytest.mark.tier_nightly
@settings(max_examples=25, deadline=None)
@given(cycles=st.integers(min_value=1, max_value=12), nested=st.booleans())
async def test_a_finished_scope_leaves_nothing_behind(
    cycles: int, nested: bool
) -> None:
    """PROP-SCOPE-005: no scope objects, no entries, no listeners.

    Failure value: a parent scope holding strong references to disposed
    children so it can report on them, turning a long-lived server into a
    monotonic memory leak proportional to sessions served.
    """
    host = PluginHost()
    bus = EventBus()
    registry: ScopedRegistry[str] = ScopedRegistry()
    calls: list[int] = []
    refs: list[weakref.ref[Scope]] = []

    for cycle in range(cycles):
        outer = create_scope(host.root.context, object(), label=f"session{cycle}")
        inner = create_scope(outer.ctx, object(), label="sub") if nested else outer
        registry.register(f"tool{cycle}", ctx=inner.ctx)
        bus.through(inner.ctx).on(
            TOPIC, calls.append, scope=scope_of(inner.ctx), global_=True
        )
        await host.runtime.quiesce()

        refs.append(weakref.ref(outer))
        if inner is not outer:
            refs.append(weakref.ref(inner))
        await outer.dispose()
        await host.runtime.quiesce()
        del outer, inner

    gc.collect()
    assert registry.all() == ()
    await bus.emit(TOPIC)
    assert calls == []
    assert [ref for ref in refs if ref() is not None] == [], "a scope outlived itself"


# --------------------------------------------------------------------------
# PROP-SCOPE-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(case=visibility_cases())
async def test_the_unfiltered_view_is_unfiltered(
    case: tuple[Plan, tuple[int | None, ...]],
) -> None:
    """PROP-SCOPE-006: `all()` is every live registration, from anywhere.

    Failure value: a "scoped" registry that drops entries rather than hiding
    them, so the session recorder and the diagnostics tree -- both of which
    read the unfiltered view -- quietly under-report, and SEM-005's caveat
    reads as a guarantee it never was.
    """
    plan, owners = case
    world = await build(plan)
    registry: ScopedRegistry[str] = ScopedRegistry()
    everything = tuple(f"item{index}" for index in range(len(owners)))
    for index, owner in enumerate(owners):
        registry.register(f"item{index}", ctx=world.ctx(owner))

    assert registry.all() == everything
    for querier in plan.sites():
        assert registry.all() == everything, "the caller's scope changed the view"
        seen = registry.visible(ctx=world.ctx(querier))
        kept = set(seen)
        assert kept <= set(everything)
        assert seen == tuple(item for item in everything if item in kept)


# --------------------------------------------------------------------------
# The seams the properties do not reach
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_an_unscoped_context_has_no_subject() -> None:
    host = PluginHost()
    assert subject_of(host.root.context) is None
    scope = create_scope(host.root.context, "session", label="one")
    await host.runtime.quiesce()
    assert subject_of(scope.ctx) is scope
    assert scope.parent is None
    assert scope.key == "session"

    child = create_scope(scope.ctx, "sub", label="two")
    await host.runtime.quiesce()
    assert child.parent is scope
    assert [item.key for item in child.lineage()] == ["sub", "session"]


@pytest.mark.tier_local
async def test_the_scope_owner_is_a_mounted_instance_with_a_label() -> None:
    """SEM-001: the lifetime owner is a real fiber, findable in the tree."""
    host = PluginHost()
    scope = create_scope(host.root.context, object(), label="session-7")
    await host.runtime.quiesce()

    labels = [node.label for node in walk(inspect(host))]
    assert any(label.endswith("session-7#0") for label in labels)

    await scope.dispose()
    await host.runtime.quiesce()
    after = [node.label for node in walk(inspect(host))]
    assert not any("session-7" in label for label in after)


@pytest.mark.tier_local
async def test_a_scope_narrows_the_filter_it_was_created_under() -> None:
    """The deviation: scope admission composes with an installed filter."""
    host = PluginHost()
    bus = EventBus()
    calls: list[str] = []

    tagged = with_filter(host.root.context, lambda carrier: carrier.get("tag") == "x")
    scope = create_scope(tagged, object(), label="inner")
    await host.runtime.quiesce()

    bus.through(scope.ctx).on(
        TOPIC, lambda: calls.append("listener"), scope=scope_of(scope.ctx)
    )

    # In the scope, but without the tag the outer filter insists on.
    await bus.through(scope_target(host.root.context, scope)).emit(TOPIC)
    assert calls == [], "the outer filter was replaced instead of narrowed"

    inside = scope_target(host.root.context.extend(tag="x"), scope)
    await bus.through(inside).emit(TOPIC)
    assert calls == ["listener"]


@pytest.mark.tier_local
async def test_scope_target_dispatches_as_a_subject_from_anywhere() -> None:
    """A recorder holds a scope and dispatches into it without being in it."""
    host = PluginHost()
    bus = EventBus()
    calls: list[str] = []

    scope = create_scope(host.root.context, object(), label="session")
    await host.runtime.quiesce()
    bus.through(scope.ctx).on(
        TOPIC, lambda: calls.append("in-scope"), scope=scope_of(scope.ctx)
    )

    await bus.through(host.root.context).emit(TOPIC)
    assert calls == [], "an unscoped carrier is not in anyone's scope"

    await bus.through(scope_target(host.root.context, scope)).emit(TOPIC)
    assert calls == ["in-scope"]

    # And back out again: an explicit `None` leaves the scope rather than
    # falling through to the one the base context is already in.
    outside = scope_target(scope.ctx, None)
    assert subject_of(scope.ctx) is scope
    assert subject_of(outside) is None
    calls.clear()
    await bus.through(outside).emit(TOPIC)
    assert calls == [], "a carrier that left the scope is still in it"


@pytest.mark.tier_local
async def test_a_registration_is_an_effect_the_caller_can_undo() -> None:
    host = PluginHost()
    registry: ScopedRegistry[str] = ScopedRegistry()
    scope = create_scope(host.root.context, object(), label="session")
    await host.runtime.quiesce()

    handle = registry.register("tool", ctx=scope.ctx, label="tool")
    assert registry.all() == ("tool",)
    result = handle()
    if result is not None:
        await result
    assert registry.all() == ()
    assert registry.visible(ctx=scope.ctx) == ()


@pytest.mark.tier_local
def test_the_model_agrees_with_itself() -> None:
    """The covers relation, against a hand-worked tree."""
    plan = Plan((None, 0, 1, None))
    assert plan.path(2) == (2, 1, 0)
    assert plan.covers(0, 2)
    assert plan.covers(None, 2)
    assert plan.covers(2, 2)
    assert not plan.covers(2, 0)
    assert not plan.covers(3, 2)
    assert not plan.covers(0, None)
    assert plan.subtree(0) == frozenset({0, 1, 2})
    assert plan.subtree(3) == frozenset({3})


@pytest.mark.tier_local
async def test_admits_is_the_same_relation_the_registry_uses() -> None:
    """`admits` is public because a caller writing its own registry needs it."""
    host = PluginHost()
    root = create_scope(host.root.context, object(), label="root")
    child = create_scope(root.ctx, object(), label="child")
    await host.runtime.quiesce()

    assert admits(None, child), "an unscoped listener hears everything"
    assert admits(root, child), "a parent hears its child"
    assert admits(child, child)
    assert not admits(child, root), "a child does not hear its parent"
    assert not admits(child, None), "nor an unscoped carrier"
    assert root.covers(child)
    assert not child.covers(root)
