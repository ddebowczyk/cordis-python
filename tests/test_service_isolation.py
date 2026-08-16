"""PROP-ISO-001..005, from spec/capabilities/08-service-isolation.yaml.

The model these tests compare against is written once, in `Plan`: a node's
realm for a name is decided by the nearest enclosing isolation of that name --
its label if it had one, the isolating node's identity otherwise -- and by
nothing else. Everything the cards assert is read off that, never off the
runtime's own bookkeeping.
"""

from __future__ import annotations

import asyncio
import gc
import itertools
import weakref
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.errors import ServiceConflictError
from cordis.fiber import FiberState
from cordis.inject import inject
from cordis.plugin import PluginHost
from cordis.realm import isolate, isolated_names
from cordis.registry import DEFAULT_REALM, ServiceRegistry, realm_for

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Stamps the labels of PROP-ISO-005 so no two examples share an interned realm.
_EXAMPLE = itertools.count()

NAMES = ("alpha", "beta", "gamma")
LABELS = (None, "one", "two")


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One context in a generated tree."""

    index: int
    parent: int | None
    #: name -> label, where None means "unlabelled isolation"
    isolates: tuple[tuple[str, str | None], ...] = ()
    #: names this node provides
    provides: tuple[str, ...] = ()


@dataclass
class Plan:
    """A generated tree, and the realm arithmetic the cards are checked against.

    Deliberately ignorant of `cordis.realm`: it knows only what the generator
    decided, which is what makes it an oracle rather than a second copy of the
    implementation.
    """

    nodes: tuple[Node, ...]

    def lineage(self, index: int) -> list[int]:
        """The node, then its ancestors, outward."""
        walk = [index]
        node = self.nodes[index]
        while node.parent is not None:
            walk.append(node.parent)
            node = self.nodes[node.parent]
        return walk

    def realm_id(self, index: int, name: str) -> object:
        """Which realm node ``index`` resolves ``name`` in.

        A label names a realm globally, so two unrelated nodes isolating the
        same name under the same label land on one id. An unlabelled isolation
        is identified by the node that made it, which is the strongest form of
        "not shared".
        """
        for step in self.lineage(index):
            for isolated, label in self.nodes[step].isolates:
                if isolated == name:
                    return ("label", name, label) if label else ("node", step, name)
        return ("outer", name)

    def bindings(self) -> dict[tuple[str, object], int]:
        """(name, realm) -> the node that got there first.

        Second and later providers of one key are rejected by the registry, so
        the plan has to know which ones those are before it can predict a
        lookup.
        """
        owners: dict[tuple[str, object], int] = {}
        for node in self.nodes:
            for name in node.provides:
                owners.setdefault((name, self.realm_id(node.index, name)), node.index)
        return owners

    def expected(self, observer: int, name: str) -> int | None:
        """Which node's value ``observer`` should see for ``name``."""
        return self.bindings().get((name, self.realm_id(observer, name)))


@st.composite
def plans(draw: st.DrawFn, size: int = 6) -> Plan:
    count = draw(st.integers(min_value=2, max_value=size))
    nodes = [Node(index=0, parent=None)]
    for index in range(1, count):
        parent = draw(st.integers(min_value=0, max_value=index - 1))
        isolates = draw(
            st.lists(
                st.tuples(st.sampled_from(NAMES), st.sampled_from(LABELS)),
                max_size=2,
                unique_by=lambda pair: pair[0],
            )
        )
        provides = draw(st.lists(st.sampled_from(NAMES), max_size=2, unique=True))
        nodes.append(
            Node(
                index=index,
                parent=parent,
                isolates=tuple(isolates),
                provides=tuple(provides),
            )
        )
    # The root provides too, or most trees have nothing to see.
    root_provides = draw(st.lists(st.sampled_from(NAMES), max_size=3, unique=True))
    nodes[0] = Node(index=0, parent=None, provides=tuple(root_provides))
    return Plan(tuple(nodes))


@dataclass
class World:
    """A plan, built."""

    registry: ServiceRegistry
    scope: EffectScope
    contexts: list[Context] = field(default_factory=list)
    values: dict[tuple[int, str], object] = field(default_factory=dict)
    rejected: list[tuple[int, str]] = field(default_factory=list)


def build(plan: Plan) -> World:
    registry = ServiceRegistry()
    scope = EffectScope("test")
    world = World(registry=registry, scope=scope)
    for node in plan.nodes:
        if node.parent is None:
            ctx = Context(resolver=registry, label="n0")
        else:
            ctx = world.contexts[node.parent].extend(node=node.index)
            if node.isolates:
                ctx = isolate(ctx, dict(node.isolates))
        world.contexts.append(ctx)
    for node in plan.nodes:
        for name in node.provides:
            value = f"{name}@{node.index}"
            world.values[(node.index, name)] = value
            ctx = world.contexts[node.index]
            try:
                registry.provide(name, value, scope=scope, ctx=ctx)
            except ServiceConflictError:
                # Expected whenever the plan puts two providers of one name in
                # one realm; the plan predicts exactly which, and `expected`
                # answers with the first.
                world.rejected.append((node.index, name))
    return world


# --------------------------------------------------------------------------
# PROP-ISO-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(plan=plans())
@settings(deadline=None)
def test_a_provider_is_seen_by_exactly_the_realm_it_bound_in(plan: Plan) -> None:
    """Failure value: isolating by shadowing the binding at lookup time rather
    than by keying the binding, so a provider inside an isolated group is also
    registered under the outer realm and a sibling group silently picks it
    up."""
    world = build(plan)
    for observer in range(len(plan.nodes)):
        for name in NAMES:
            owner = plan.expected(observer, name)
            found = world.registry.lookup(name, ctx=world.contexts[observer])
            if owner is None:
                assert found is None, f"n{observer} sees {name} and should not"
            else:
                assert found == world.values[(owner, name)]
    # The plan's conflict prediction is part of the oracle, not a by-product:
    # if the runtime rejected a different set, the realms it computed differ
    # from the ones the plan computed and every assertion above was luck.
    predicted = {
        (node.index, name)
        for node in plan.nodes
        for name in node.provides
        if plan.bindings().get((name, plan.realm_id(node.index, name))) != node.index
    }
    assert set(world.rejected) == predicted


# --------------------------------------------------------------------------
# PROP-ISO-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    left=st.sampled_from(LABELS),
    right=st.sampled_from(LABELS),
    name=st.sampled_from(NAMES),
)
@settings(deadline=None)
def test_a_label_is_what_makes_two_isolations_one(
    left: str | None, right: str | None, name: str
) -> None:
    """Failure value: treating `label=None` as a label, so every unlabelled
    isolation joins one shared "None" realm and isolation silently stops
    isolating."""
    registry = ServiceRegistry()
    scope = EffectScope("test")
    root = Context(resolver=registry, label="root")
    first = isolate(root.extend(side="left"), {name: left})
    second = isolate(root.extend(side="right"), {name: right})

    shared = left is not None and left == right
    value = object()
    registry.provide(name, value, scope=scope, ctx=first)

    assert registry.lookup(name, ctx=first) is value
    seen = registry.lookup(name, ctx=second)
    if shared:
        assert seen is value
    else:
        assert seen is None
        # And the proof that they are different realms rather than merely
        # unpopulated: the second isolation can claim the name itself.
        other = object()
        registry.provide(name, other, scope=scope, ctx=second)
        assert registry.lookup(name, ctx=second) is other
        assert registry.lookup(name, ctx=first) is value


# --------------------------------------------------------------------------
# PROP-ISO-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    provided=st.lists(st.sampled_from(NAMES), min_size=1, max_size=3, unique=True),
    isolated=st.lists(st.sampled_from(NAMES), max_size=3, unique=True),
)
@settings(deadline=None)
def test_isolating_one_name_moves_only_that_name(
    provided: Sequence[str], isolated: Sequence[str]
) -> None:
    """Failure value: implementing isolation by giving the child a fresh
    registry rather than a fresh realm for one name, so an isolated subtree
    loses access to the logger and every unrelated service."""
    registry = ServiceRegistry()
    scope = EffectScope("test")
    parent = Context(resolver=registry, label="parent")
    for name in provided:
        registry.provide(name, object(), scope=scope, ctx=parent)

    child = isolate(parent, tuple(isolated))

    for name in NAMES:
        here = registry.lookup(name, ctx=child)
        there = registry.lookup(name, ctx=parent)
        if name in isolated:
            assert here is None, f"{name} was isolated and still resolves outward"
        else:
            assert here is there, f"{name} was not isolated and moved anyway"
    assert isolated_names(child) == frozenset(isolated)


# --------------------------------------------------------------------------
# PROP-ISO-004
# --------------------------------------------------------------------------


class Watched:
    def __init__(self) -> None:
        self.held: list[object] = []


def consumer(watched: Watched) -> Any:
    @inject("alpha")
    def apply(ctx: Context) -> None:
        watched.held.append(ctx.alpha)

    apply.__qualname__ = "consumer"
    return apply


def group(ctx: Context) -> None:
    """A plugin whose whole purpose is to hold an isolation open."""


class IsolationMachine(RuleBasedStateMachine):
    """PROP-ISO-004: a consumer inside an isolation follows the inner name.

    Failure value: dependency evaluation using the un-isolated name while
    lookup uses the isolated one, so the consumer activates because an outer
    shell exists and then calls the inner shell that has not been provided yet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host = PluginHost()
        self.watched = Watched()
        self.inner: object | None = None
        self.outer: object | None = None
        self.inner_handle: object = None
        self.outer_handle: object = None
        self.group = self.host.root.plugin(group, isolate=("alpha",))
        self.fiber = self.group.plugin(consumer(self.watched))

    def _run(self, work: Any) -> None:
        if self.loop is None:
            # Hypothesis builds one machine just to collect its rules and never
            # runs it; creating a loop in __init__ would leak that one.
            self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(work)

    async def _settle(self) -> None:
        """Let every consequence land.

        Awaiting the fiber is not decoration: work provoked while no loop was
        running is held until the fiber's next awaited boundary, and a settle
        that only slept would be asserting on a system that had not been given
        the chance to react.
        """
        for _ in range(6):
            before = self.fiber.state
            with suppress(Exception):
                await asyncio.wait_for(self.fiber, timeout=5)
            await self.host.runtime.quiesce()
            if self.fiber.state is before:
                break

    async def _provide_inner(self) -> None:
        self.inner = object()
        self.inner_handle = self.host.registry.provide(
            "alpha", self.inner, scope=self.group.scope, ctx=self.group.context
        )
        await self._settle()

    async def _provide_outer(self) -> None:
        self.outer = object()
        self.outer_handle = self.host.registry.provide(
            "alpha", self.outer, scope=self.host.root.scope, ctx=self.host.root.context
        )
        await self._settle()

    async def _withdraw(self, handle: Any) -> None:
        handle()
        await self._settle()

    @rule()
    def provide_inner(self) -> None:
        if self.inner is not None:
            return
        self._run(self._provide_inner())

    @rule()
    def remove_inner(self) -> None:
        if self.inner is None:
            return
        handle, self.inner = self.inner_handle, None
        self._run(self._withdraw(handle))

    @rule()
    def provide_outer(self) -> None:
        if self.outer is not None:
            return
        self._run(self._provide_outer())

    @rule()
    def remove_outer(self) -> None:
        if self.outer is None:
            return
        handle, self.outer = self.outer_handle, None
        self._run(self._withdraw(handle))

    @invariant()
    def the_consumer_follows_the_inner_name(self) -> None:
        if self.loop is None:
            return
        expected = FiberState.ACTIVE if self.inner is not None else FiberState.PENDING
        assert self.fiber.state is expected, (
            f"inner={self.inner is not None} outer={self.outer is not None} "
            f"state={self.fiber.state.name}"
        )
        for held in self.watched.held:
            assert held is not self.outer, "the consumer was handed the outer value"

    def teardown(self) -> None:
        if self.loop is None:
            return
        self.loop.run_until_complete(self.host.dispose())
        self.loop.close()


TestIsolationMachine = pytest.mark.tier_pr(IsolationMachine.TestCase)


# --------------------------------------------------------------------------
# PROP-ISO-005
# --------------------------------------------------------------------------


@pytest.mark.tier_nightly
@given(
    cycles=st.integers(min_value=1, max_value=6),
    labels=st.lists(st.sampled_from(LABELS), min_size=1, max_size=3),
)
@settings(deadline=None, max_examples=25)
def test_isolated_subtrees_leave_no_realms_behind(
    cycles: int, labels: Sequence[str | None]
) -> None:
    """Failure value: interning labelled realms in a strong-keyed dict, so
    every reload of a config file that isolates by label leaks one realm and
    its bindings -- unbounded growth on a long-running process with hot reload
    enabled."""

    # Labels are stamped per example. A labelled realm is interned globally by
    # (name, label), so two examples using the label "two" would be observing
    # one object, and a reference surviving anywhere in the previous example --
    # a retained frame, a traceback -- would read as a leak in this one. The
    # property does not depend on how a label is spelled; the isolation between
    # examples does.
    stamp = next(_EXAMPLE)
    labels = [None if label is None else f"{label}-{stamp}" for label in labels]

    # One cycle per call, so every reference it takes dies with its frame. A
    # disposed instance still owns the context that carries its realm, and a
    # loop variable left bound after the loop is enough to keep one alive --
    # the test would be measuring itself rather than the runtime.
    async def once(host: PluginHost, cycle: int, seen: list[weakref.ref[Any]]) -> None:
        handles: list[Any] = []
        for index, label in enumerate(labels):
            held = host.root.plugin(
                group, isolate={"alpha": label} if label else ("alpha",)
            )
            with suppress(ServiceConflictError):
                # Two subtrees sharing a label share a realm, so the second
                # provider collides. The binding is here to give the realm
                # something to be retained by; one of them is enough, and the
                # collision is the sharing working.
                host.registry.provide(
                    "alpha", f"impl-{cycle}-{index}", scope=held.scope, ctx=held.context
                )
            seen.append(weakref.ref(realm_for(held.context, "alpha")))
            handles.append(held)
        while handles:
            await handles.pop().dispose()

    async def run() -> None:
        host = PluginHost()
        seen: list[weakref.ref[Any]] = []
        for cycle in range(cycles):
            await once(host, cycle, seen)
        await host.dispose()
        gc.collect()
        alive = [ref for ref in seen if ref() is not None]
        assert alive == [], f"{len(alive)} of {len(seen)} realms outlived their subtree"

    asyncio.run(run())


# --------------------------------------------------------------------------
# The seams themselves
# --------------------------------------------------------------------------


def test_an_unisolated_context_answers_the_default_realm() -> None:
    registry = ServiceRegistry()
    ctx = Context(resolver=registry, label="root")
    assert realm_for(ctx, "alpha") is DEFAULT_REALM
    assert isolated_names(ctx) == frozenset()


def test_isolating_the_same_name_twice_nests_rather_than_merges() -> None:
    """An inner isolation shadows an outer one, and the outer keeps its own."""
    registry = ServiceRegistry()
    scope = EffectScope("test")
    root = Context(resolver=registry, label="root")
    outer = isolate(root, ("alpha",))
    inner = isolate(outer, ("alpha",))
    assert realm_for(inner, "alpha") is not realm_for(outer, "alpha")

    registry.provide("alpha", "outer", scope=scope, ctx=outer)
    registry.provide("alpha", "inner", scope=scope, ctx=inner)
    assert registry.lookup("alpha", ctx=outer) == "outer"
    assert registry.lookup("alpha", ctx=inner) == "inner"


def test_a_service_class_isolates_under_its_name() -> None:
    """`isolate` reads a Service subclass the way `inject` does."""
    from cordis.registry import Service

    class Shell(Service):
        name = "shell"

    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    assert isolated_names(isolate(root, (Shell,))) == frozenset({"shell"})


def test_a_mounted_instance_keeps_its_isolation_across_a_restart() -> None:
    """The isolation belongs to the instance, not to the context object.

    An instance rebuilds its context on every reload; an isolation held only by
    the caller's context would quietly stop applying the first time the
    instance restarted, which is the case nobody tests by hand.
    """

    async def run() -> None:
        host = PluginHost()
        held = host.root.plugin(group, isolate=("alpha",))
        before = realm_for(held.context, "alpha")
        await held.restart()
        after = realm_for(held.context, "alpha")
        assert after is not DEFAULT_REALM
        assert after is not before, "an unlabelled realm is minted per load"
        assert isolated_names(held.context) == frozenset({"alpha"})
        await host.dispose()

    asyncio.run(run())


def test_a_labelled_isolation_survives_a_restart_as_the_same_realm() -> None:
    async def run() -> None:
        host = PluginHost()
        held = host.root.plugin(group, isolate={"alpha": "shared"})
        before = realm_for(held.context, "alpha")
        await held.restart()
        assert realm_for(held.context, "alpha") is before
        await host.dispose()

    asyncio.run(run())


def test_providing_without_a_context_still_means_the_default_realm() -> None:
    """The pre-isolation call shape keeps working, unchanged."""
    registry = ServiceRegistry()
    scope = EffectScope("test")
    registry.provide("alpha", "value", scope=scope)
    ctx = Context(resolver=registry, label="root")
    assert registry.lookup("alpha", ctx=ctx) == "value"


def test_an_empty_isolation_is_the_same_context() -> None:
    """Nothing to isolate, nothing to extend: no realm, no metadata frame."""
    registry = ServiceRegistry()
    ctx = Context(resolver=registry, label="root")
    assert isolate(ctx, ()) is ctx


@pytest.mark.tier_local
@given(plan=plans())
@settings(deadline=None, max_examples=25)
def test_the_plan_agrees_with_itself(plan: Plan) -> None:
    """The oracle's own check.

    `realm_id` is the whole model; if two nodes with the same isolation
    ancestry disagreed about their realm, every card built on it would be
    comparing noise.
    """
    assume(len(plan.nodes) > 1)
    for node in plan.nodes:
        if node.parent is None:
            continue
        declared = dict(node.isolates)
        for name in NAMES:
            here = plan.realm_id(node.index, name)
            if name not in declared:
                assert here == plan.realm_id(node.parent, name)
            elif declared[name] is None:
                assert here == ("node", node.index, name)
            else:
                assert here == ("label", name, declared[name])
