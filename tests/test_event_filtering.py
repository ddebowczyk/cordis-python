"""PROP-FILTER-001..005, from spec/capabilities/10-event-filtering.yaml.

The model is written once, in `Plan`: a registration is admitted when it is
global, when no ancestor of its registration context installed a filter, or when
the nearest installed filter names the carrier's tag. The predicates the runtime
calls are the test's own closures over that same generated data, so agreement is
evidence rather than a restatement.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.events import Bail, Emit, ErrorReport, EventBus, Parallel, Serial, Waterfall
from cordis.filter import filter_of, with_filter
from cordis.registry import ServiceRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cordis.events import Next

TAGS = ("a", "b", "c")


def tag_of(ctx: Context) -> str | None:
    """The carrier's tag, read the way any plugin would read scoped metadata."""
    for node in ctx.lineage():
        found = node.own_meta.get("tag")
        if isinstance(found, str):
            return found
    return None


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One registration context in a generated tree."""

    index: int
    parent: int | None
    #: The tags this node's filter admits, or None when it installs no filter.
    allow: frozenset[str] | None = None
    #: Whether this node's filter raises instead of answering.
    raises: bool = False


@dataclass(frozen=True)
class Reg:
    """One registration: which context it was made under, and how."""

    node: int
    global_: bool


@dataclass
class Plan:
    nodes: tuple[Node, ...]
    regs: tuple[Reg, ...]

    def path(self, index: int) -> list[int]:
        walk = [index]
        node = self.nodes[index]
        while node.parent is not None:
            walk.append(node.parent)
            node = self.nodes[node.parent]
        return walk

    def filtering(self, index: int) -> Node | None:
        """The nearest node up the path that installed a filter."""
        for step in self.path(index):
            if self.nodes[step].allow is not None:
                return self.nodes[step]
        return None

    def admits(self, reg: Reg, tag: str | None) -> bool:
        """Whether ``reg``'s listener runs for a carrier tagged ``tag``."""
        if reg.global_:
            return True
        owner = self.filtering(reg.node)
        if owner is None:
            return True
        if owner.raises:
            # A predicate that raises denies its own listeners (SEM-005).
            return False
        assert owner.allow is not None
        return tag in owner.allow

    def admitted(self, tag: str | None) -> list[int]:
        """The indices of the registrations that should run, in order."""
        return [i for i, reg in enumerate(self.regs) if self.admits(reg, tag)]


@st.composite
def plans(draw: st.DrawFn, *, raising: bool = False, size: int = 5) -> Plan:
    count = draw(st.integers(min_value=1, max_value=size))
    nodes = [Node(index=0, parent=None)]
    for index in range(1, count):
        parent = draw(st.integers(min_value=0, max_value=index - 1))
        allow = draw(
            st.one_of(
                st.none(),
                st.frozensets(st.sampled_from(TAGS), max_size=3),
            )
        )
        raises = allow is not None and raising and draw(st.booleans())
        nodes.append(Node(index=index, parent=parent, allow=allow, raises=raises))
    regs = draw(
        st.lists(
            st.builds(
                Reg,
                node=st.integers(min_value=0, max_value=count - 1),
                global_=st.booleans(),
            ),
            min_size=1,
            max_size=8,
        )
    )
    return Plan(tuple(nodes), tuple(regs))


@dataclass
class World:
    """A plan, built: contexts, a bus, and what the run recorded."""

    bus: EventBus
    scope: EffectScope
    root: Context
    registry: ServiceRegistry
    contexts: list[Context] = field(default_factory=list)
    calls: list[int] = field(default_factory=list)
    evaluations: list[int] = field(default_factory=list)
    reports: list[ErrorReport] = field(default_factory=list)


def build_contexts(plan: Plan) -> World:
    """The contexts and the bus, with nothing registered yet."""
    registry = ServiceRegistry()
    root = Context(resolver=registry, label="root")
    world = World(
        bus=EventBus(), scope=EffectScope("test"), root=root, registry=registry
    )
    world.bus.on_error(world.reports.append)
    for node in plan.nodes:
        if node.parent is None:
            ctx = root
        else:
            ctx = world.contexts[node.parent].extend(node=node.index)
        if node.allow is not None:
            ctx = with_filter(ctx, predicate(world, node))
        world.contexts.append(ctx)
    return world


def build(plan: Plan, event: Any) -> World:
    world = build_contexts(plan)
    for index, reg in enumerate(plan.regs):
        world.bus.through(world.contexts[reg.node]).on(
            event,
            listener(world, index),
            scope=world.scope,
            global_=reg.global_,
        )
    return world


def predicate(world: World, node: Node) -> Callable[[Context], bool]:
    allow = node.allow
    assert allow is not None

    def admits(carrier: Context) -> bool:
        world.evaluations.append(node.index)
        if node.raises:
            msg = f"filter for node {node.index} is broken"
            raise RuntimeError(msg)
        return tag_of(carrier) in allow

    admits.__qualname__ = f"filter#{node.index}"
    return admits


def listener(world: World, index: int) -> Callable[..., None]:
    def record(*_args: object, **_kwargs: object) -> None:
        world.calls.append(index)

    record.__qualname__ = f"listener#{index}"
    return record


# --------------------------------------------------------------------------
# PROP-FILTER-001
# --------------------------------------------------------------------------

TOPIC: Emit[[]] = Emit("topic")


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans(), tag=st.one_of(st.none(), st.sampled_from(TAGS)))
def test_exactly_the_admitted_listeners_run(plan: Plan, tag: str | None) -> None:
    """PROP-FILTER-001: admission is the registering context's business.

    Failure value: inheriting the filter from the *dispatching* context rather
    than the registering one, which inverts the mechanism -- a global recorder
    dispatching on behalf of a subject would filter out every listener.
    """
    world = build(plan, TOPIC)
    carrier = world.root.extend(tag=tag) if tag is not None else world.root
    asyncio.run(world.bus.through(carrier).emit(TOPIC))
    assert world.calls == plan.admitted(tag)


@pytest.mark.tier_local
def test_a_dispatch_with_no_carrier_admits_everyone() -> None:
    """The deviation, stated as a test: absence is open on both sides."""
    plan = Plan((Node(0, None, frozenset({"a"})),), (Reg(0, global_=False),))
    world = build(plan, TOPIC)
    asyncio.run(world.bus.emit(TOPIC))
    assert world.calls == [0]
    assert world.evaluations == [], "a carrierless dispatch asks no filter"


# --------------------------------------------------------------------------
# PROP-FILTER-002
# --------------------------------------------------------------------------

MODES = ("emit", "parallel", "serial", "bail", "waterfall")


def dispatch(bus: EventBus, mode: str, event: Any, carrier: Context | None) -> Any:
    """Run one dispatch, through ``carrier`` when there is one."""
    target: Any = bus if carrier is None else bus.through(carrier)
    if mode == "emit":
        return asyncio.run(target.emit(event))
    if mode == "parallel":
        return asyncio.run(target.parallel(event))
    if mode == "serial":
        return asyncio.run(target.serial(event))
    if mode == "bail":
        return target.bail(event)
    return asyncio.run(target.waterfall(event, lambda: "default"))


def event_for(mode: str) -> Any:
    # One name per mode: a bus keys its channels by name, so reusing "topic"
    # would leave each mode dispatching to the previous mode's listeners too.
    return {
        "emit": Emit(f"topic/{mode}"),
        "parallel": Parallel(f"topic/{mode}"),
        "serial": Serial(f"topic/{mode}"),
        "bail": Bail(f"topic/{mode}"),
        "waterfall": Waterfall(f"topic/{mode}"),
    }[mode]


def answering(world: World, index: int, answer: object) -> Callable[..., Any]:
    """A listener that records itself and then answers as the mode expects."""

    def respond(*_args: object, **_kwargs: object) -> object:
        world.calls.append(index)
        return answer

    respond.__qualname__ = f"listener#{index}"
    return respond


def wrapping(world: World, index: int, stop: bool) -> Callable[..., Any]:
    async def step(next_: Next[str], *_args: object, **_kwargs: object) -> str:
        world.calls.append(index)
        if stop:
            return f"stop#{index}"
        return f"{index}:" + await next_()

    step.__qualname__ = f"listener#{index}"
    return step


def register(
    world: World, plan: Plan, mode: str, event: Any, answers: Sequence[object]
) -> None:
    for index, reg in enumerate(plan.regs):
        answer = answers[index % len(answers)]
        made = (
            wrapping(world, index, stop=bool(answer))
            if mode == "waterfall"
            else answering(world, index, answer)
        )
        world.bus.through(world.contexts[reg.node]).on(
            event, made, scope=world.scope, global_=reg.global_
        )


@pytest.mark.tier_pr
@settings(max_examples=100, deadline=None)
@given(
    plan=plans(),
    tag=st.sampled_from(TAGS),
    mode=st.sampled_from(MODES),
    answers=st.lists(st.one_of(st.none(), st.just("x")), min_size=1, max_size=3),
)
def test_filtering_only_removes_listeners(
    plan: Plan, tag: str, mode: str, answers: list[object]
) -> None:
    """PROP-FILTER-002: the admitted subset behaves like a bus of just those.

    Failure value: a waterfall whose `next` chain is built before filtering, so
    a denied listener still occupies a link and the chain returns early.
    """
    event = event_for(mode)
    filtered = build_contexts(plan)
    register(filtered, plan, mode, event, answers)
    carrier = filtered.root.extend(tag=tag)
    first = dispatch(filtered.bus, mode, event, carrier)

    admitted = plan.admitted(tag)
    plain = build_contexts(Plan((Node(0, None),), ()))
    for index in admitted:
        answer = answers[index % len(answers)]
        made = (
            wrapping(plain, index, stop=bool(answer))
            if mode == "waterfall"
            else answering(plain, index, answer)
        )
        plain.bus.on(event, made, scope=plain.scope)
    second = dispatch(plain.bus, mode, event, None)

    assert filtered.calls == plain.calls
    assert first == second


# --------------------------------------------------------------------------
# PROP-FILTER-003
# --------------------------------------------------------------------------


@pytest.mark.tier_nightly
@settings(max_examples=50, deadline=None)
@given(
    plan=plans(),
    tag=st.sampled_from(TAGS),
    listeners=st.integers(min_value=1, max_value=200),
)
def test_a_filter_is_asked_once_per_dispatch(
    plan: Plan, tag: str, listeners: int
) -> None:
    """PROP-FILTER-003: cost is linear in contexts, not in listeners.

    Failure value: dropping the memo during a refactor, turning a per-event
    cost linear in subjects into one linear in listeners.
    """
    world = build_contexts(plan)
    for index in range(listeners):
        node = plan.regs[index % len(plan.regs)].node
        world.bus.through(world.contexts[node]).on(
            TOPIC, listener(world, index), scope=world.scope
        )
    asyncio.run(world.bus.through(world.root.extend(tag=tag)).emit(TOPIC))
    seen = sorted(world.evaluations)
    assert seen == sorted(set(seen)), f"a filter was asked twice: {world.evaluations}"
    used = {plan.regs[index % len(plan.regs)].node for index in range(listeners)}
    consulted = {
        owner.index
        for owner in (plan.filtering(node) for node in used)
        if owner is not None
    }
    assert set(seen) == consulted, "asked exactly the filters in the way, once each"


# --------------------------------------------------------------------------
# PROP-FILTER-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans(raising=True), tag=st.sampled_from(TAGS))
def test_a_raising_filter_denies_only_its_own(plan: Plan, tag: str) -> None:
    """PROP-FILTER-004: one bad predicate is one subject's problem.

    Failure value: a predicate exception propagating out of the dispatch loop,
    so one subject's malformed scope tag stops delivery for every subject.
    """
    world = build(plan, TOPIC)
    asyncio.run(world.bus.through(world.root.extend(tag=tag)).emit(TOPIC))
    assert world.calls == plan.admitted(tag)
    consulted = {
        owner.index
        for owner in (plan.filtering(reg.node) for reg in plan.regs if not reg.global_)
        if owner is not None
    }
    broken = {index for index in consulted if plan.nodes[index].raises}
    assert set(world.evaluations) == consulted, "asked exactly the filters in the way"
    assert len(world.reports) == len(broken), "one report per broken evaluation"
    assert all(isinstance(r.error, RuntimeError) for r in world.reports)


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


def test_a_filter_is_inherited_until_it_is_replaced() -> None:
    root = Context(resolver=ServiceRegistry(), label="root")

    def first(_ctx: Context) -> bool:
        return True

    def second(_ctx: Context) -> bool:
        return False

    outer = with_filter(root, first)
    middle = outer.extend(step=1)
    inner = with_filter(middle, second)
    assert filter_of(root) is None
    assert filter_of(outer) is first
    assert filter_of(middle) is first, "inherited like every other scoped value"
    assert filter_of(inner) is second, "replaced, not composed"


def test_a_global_listener_bypasses_a_denying_filter() -> None:
    plan = Plan((Node(0, None, frozenset()),), (Reg(0, global_=True),))
    world = build(plan, TOPIC)
    asyncio.run(world.bus.through(world.root.extend(tag="a")).emit(TOPIC))
    assert world.calls == [0]
    assert world.evaluations == [], "a global listener asks no filter at all"


def test_the_raw_bus_still_registers_without_a_context() -> None:
    world = build_contexts(Plan((Node(0, None, frozenset({"a"})),), ()))
    world.bus.on(TOPIC, listener(world, 0), scope=world.scope)
    asyncio.run(world.bus.through(world.root.extend(tag="z")).emit(TOPIC))
    assert world.calls == [0], "no registration context, no filter"


def test_the_bound_bus_dispatches_every_mode() -> None:
    world = build_contexts(Plan((Node(0, None),), ()))
    carrier = world.root.extend(tag="a")
    for mode in MODES:
        event = event_for(mode)
        made = (
            wrapping(world, 0, stop=True)
            if mode == "waterfall"
            else answering(world, 0, "x")
        )
        world.bus.through(world.root).on(event, made, scope=world.scope)
        dispatch(world.bus, mode, event, carrier)
    assert world.calls == [0] * len(MODES)


def test_the_plan_agrees_with_itself() -> None:
    plan = Plan(
        (
            Node(0, None, frozenset({"a"})),
            Node(1, 0),
            Node(2, 1, frozenset({"b"})),
        ),
        (Reg(0, False), Reg(1, False), Reg(2, False), Reg(2, True)),
    )
    assert plan.admitted("a") == [0, 1, 3]
    assert plan.admitted("b") == [2, 3]
    assert plan.admitted(None) == [3]


# --------------------------------------------------------------------------
# PROP-FILTER-005
# --------------------------------------------------------------------------

REACHABLE = ("shell", "logger", "db")


def _unfiltered(plan: Plan) -> Plan:
    """The same tree with every filter taken out -- the differential baseline."""
    nodes = tuple(replace(node, allow=None, raises=False) for node in plan.nodes)
    return Plan(nodes, plan.regs)


def _stocked(plan: Plan) -> World:
    world = build(plan, TOPIC)
    for name in REACHABLE:
        world.registry.provide(name, object(), scope=world.scope)
    return world


@pytest.mark.tier_local
@settings(max_examples=200, deadline=None)
@given(plan=plans(), tag=st.one_of(st.none(), st.sampled_from(TAGS)))
def test_a_filter_routes_dispatch_and_changes_nothing_about_reach(
    plan: Plan, tag: str | None
) -> None:
    """PROP-FILTER-005 (SEM-004): a filter is not a security boundary.

    Differential against the same tree with the filters removed: whatever a
    context could resolve without them, it resolves with them, including the
    contexts whose listeners the filters just denied. Stated as a test because
    the temptation is to reach for `with_filter` when what is wanted is
    `isolate` -- and a mechanism that half-works as a boundary is worse than
    one that visibly does not work as one at all.

    Failure value: someone confining a tenant's plugins with a filter, on the
    evidence that the tenant's listeners stopped being called.
    """
    filtered = _stocked(plan)
    plain = _stocked(_unfiltered(plan))

    for index in range(len(plan.nodes)):
        for name in REACHABLE:
            here = filtered.contexts[index].get(name)
            there = plain.contexts[index].get(name)
            assert (here is None) == (there is None), (index, name)
            assert here is not None

    carrier = filtered.root.extend(tag=tag) if tag is not None else filtered.root
    asyncio.run(filtered.bus.through(carrier).emit(TOPIC))
    assert filtered.calls == plan.admitted(tag)

    # The denied listeners are exactly the ones whose reach was just shown to
    # be intact, so the claim is about them rather than about nobody.
    denied = [
        index for index in range(len(plan.regs)) if index not in set(plan.admitted(tag))
    ]
    for index in denied:
        ctx = filtered.contexts[plan.regs[index].node]
        assert all(ctx.get(name) is not None for name in REACHABLE)
