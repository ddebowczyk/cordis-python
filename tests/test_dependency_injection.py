"""PROP-INJECT-001..006, from spec/capabilities/05-dependency-injection.yaml.

Two things in here are deliberately awkward, and both are the point.

Providers are *plugins*, not registry pokes. A test that binds a name by
calling the registry directly cannot exercise cascade or cycles: the
interesting cases are the ones where a provider is itself a consumer, and only
a mounted provider can be that.

Nothing asserts on a state until the tree has settled. A reload is scheduled,
so sampling a fiber the turn after a provider changed reads a state that is
about to be replaced -- a flake in the test, not a defect in the code.
"""

from __future__ import annotations

import asyncio
import graphlib
import itertools
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from cordis.errors import DependencyCycleError
from cordis.fiber import FiberState
from cordis.inject import dependencies_of, inject, provisions_of
from cordis.plugin import PluginHost
from cordis.registry import Service

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from cordis.context import Context
    from cordis.fiber import Fiber

#: Four names is enough for a two-cycle plus an acyclic remainder.
POOL = ("s0", "s1", "s2", "s3")


# --------------------------------------------------------------------------
# Targets the tests mount
# --------------------------------------------------------------------------


def _same_kind(self: object, other: object) -> bool:
    return type(self) is type(other)


def _kind_hash(self: object) -> int:
    return hash(type(self))


def service(name: str, *, needs: Sequence[str] = ()) -> type[Service]:
    """A provider of ``name`` that itself requires ``needs``.

    A class rather than a function because a provider has to *declare* what it
    will bind: a cycle must be visible while every member of it is still
    PENDING, and a body that would call `provide` when it ran declares nothing.

    Two instances of it compare equal, which is not decoration. A service that
    carries no distinguishing state -- a client wrapping a connection pool, a
    formatter, a clock -- is the ordinary case, and it is exactly the case
    where an equality comparison would fail to notice a replacement. A test
    whose services all compared unequal could not tell the two rules apart.
    """
    return type(
        f"Svc_{name}",
        (Service,),
        {
            "name": name,
            "inject": tuple(needs),
            "__module__": __name__,
            "__eq__": _same_kind,
            "__hash__": _kind_hash,
        },
    )


class Observed:
    """What each plugin body saw at the moment it ran."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, frozenset[str], tuple[object, ...]]] = []

    def record(self, who: str, ctx: Context, names: Sequence[str]) -> None:
        bound = frozenset(name for name in POOL if ctx.get(name) is not None)
        # The objects, not their ids: an id is unique only among live objects,
        # and the one this test wants to tell apart is the one just replaced.
        held = tuple(ctx.get(name) for name in names)
        self.entries.append((who, bound, held))

    def loads(self, who: str) -> int:
        return sum(1 for entry in self.entries if entry[0] == who)

    def held_at(self, who: str) -> list[tuple[object, ...]]:
        return [entry[2] for entry in self.entries if entry[0] == who]


def consumer(
    seen: Observed, who: str, needs: Sequence[str], *, optional: Sequence[str] = ()
) -> Any:
    """A plugin that declares ``needs`` and merely looks ``optional`` up."""

    def apply(ctx: Context) -> None:
        for name in optional:
            ctx.get(name)  # a probe, not a declaration (SEM-005)
        seen.record(who, ctx, needs)

    # The label a mount derives comes from the qualname, and a closure's is
    # `consumer.<locals>.apply` for every one of them.
    apply.__name__ = who
    apply.__qualname__ = who
    return inject(*needs)(apply)


async def quiet(host: PluginHost) -> None:
    """Let the tree finish reacting to whatever was just done to it.

    Awaiting each fiber is not redundant with quiescing the runtime: work
    provoked by a mount made outside the loop is held on the fiber until its
    next awaited boundary (04 SEM-009), and the runtime has no task to wait for
    until then. Repeated because a load mounts children, whose loads settle a
    pass later.
    """

    async def settle() -> None:
        for _ in range(8):
            before = states(host)
            for fiber in walk(host.root):
                await fiber
            await host.runtime.quiesce()
            if states(host) == before:
                return
        raise AssertionError("the tree never stopped changing")

    await asyncio.wait_for(settle(), timeout=10)


def walk(fiber: Fiber) -> Iterator[Fiber]:
    for child in fiber.children:
        yield child
        yield from walk(child)


def states(host: PluginHost) -> dict[str, str]:
    """A canonical (label -> state) snapshot, comparable across runs."""
    return {fiber.label: fiber.state.name for fiber in walk(host.root)}


def state_of(fiber: Fiber) -> str:
    """The state, named. Read through a function because a fiber's state is a
    live reading: asserting on it twice is two questions, not a narrowing."""
    return fiber.state.name


def binding_keys(host: PluginHost) -> frozenset[str]:
    return frozenset(name for name, _realm in host.registry.bindings())


# --------------------------------------------------------------------------
# PROP-INJECT-001
# --------------------------------------------------------------------------


class InjectionMachine(RuleBasedStateMachine):
    """Mount and unmount consumers and providers against one live host."""

    consumers = Bundle("consumers")
    providers = Bundle("providers")

    def __init__(self) -> None:
        super().__init__()
        # Opened on first use, not here: Hypothesis constructs one machine to
        # collect the rules off and never runs it, and a loop allocated in
        # `__init__` is one nothing tears down.
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host = PluginHost()
        self.seen = Observed()
        self.declared: dict[str, tuple[str, ...]] = {}
        self.minted = 0

    def _run(self, coro: Any) -> Any:
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        return self.loop.run_until_complete(coro)

    @rule(target=consumers, needs=st.frozensets(st.sampled_from(POOL), max_size=3))
    def mount_consumer(self, needs: frozenset[str]) -> Fiber:
        self.minted += 1
        who = f"c{self.minted}"
        names = tuple(sorted(needs))
        self.declared[who] = names
        fiber = self.host.root.plugin(consumer(self.seen, who, names))
        self._run(quiet(self.host))
        return fiber

    @rule(target=providers, name=st.sampled_from(POOL))
    def mount_provider(self, name: str) -> Fiber | None:
        # A name already bound would be a conflict, which is the registry's
        # business and not this property's.
        if name in binding_keys(self.host):
            return None
        fiber = self.host.root.plugin(service(name))
        self._run(quiet(self.host))
        return fiber

    @rule(fiber=consumes(providers))
    def withdraw(self, fiber: Fiber | None) -> None:
        if fiber is None:
            return
        self._run(fiber.dispose())
        self._run(quiet(self.host))

    @rule(fiber=consumes(consumers))
    def unmount(self, fiber: Fiber) -> None:
        self._run(fiber.dispose())
        self._run(quiet(self.host))

    @invariant()
    def every_body_ran_with_what_it_declared(self) -> None:
        for who, bound, _held in self.seen.entries:
            names = set(self.declared[who])
            assert names <= bound, f"{who} ran with {sorted(names - bound)} unbound"

    def teardown(self) -> None:
        if self.loop is None:  # nothing ran, so there is nothing to unwind
            return
        try:
            # Bounded: disposing an application must terminate, so a hang here
            # is a defect to report rather than a reason for the suite to stop.
            self._run(asyncio.wait_for(self.host.dispose(), timeout=10))
        finally:
            self.loop.close()


@pytest.mark.tier_pr
def test_a_body_runs_only_with_every_declared_dependency_bound() -> None:
    """Failure value: a body that runs before its dependency is bound, because
    the declaration was read as advisory -- the plugin then resolves `None` and
    fails somewhere unrelated to the missing service."""
    InjectionMachine.TestCase.settings = settings(max_examples=40, deadline=None)
    InjectionMachine().TestCase().runTest()


# --------------------------------------------------------------------------
# PROP-INJECT-002
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    wants=st.lists(st.frozensets(st.sampled_from(POOL), max_size=2), max_size=4),
    data=st.data(),
)
@settings(deadline=None)
def test_the_order_dependencies_arrive_does_not_change_the_result(
    wants: list[frozenset[str]], data: st.DataObject
) -> None:
    """Failure value: readiness decided once, when the mount happens -- so a
    consumer mounted before its provider waits forever while an identical one
    mounted afterwards runs, and which of the two you get depends on the order
    the mounts happen to be written in."""
    # Providers and consumers are permuted *together*: a plan that mounts every
    # consumer before every provider is broken uniformly by an implementation
    # that never re-evaluates, and comparing two uniformly broken runs proves
    # nothing. What must not matter is whether a given consumer was mounted
    # before or after the service it needs.
    plan = [("provider", name) for name in POOL]
    plan += [("consumer", f"c{index}") for index in range(len(wants))]
    needs = {f"c{index}": tuple(sorted(want)) for index, want in enumerate(wants)}
    order = data.draw(st.permutations(plan))
    other = data.draw(st.permutations(plan))

    def run(
        sequence: Sequence[tuple[str, str]],
    ) -> tuple[dict[str, str], frozenset[str]]:
        host = PluginHost()
        seen = Observed()
        # Keyed by what the test called each plugin, not by the label the mount
        # derived: a label carries its mount ordinal, which is precisely the
        # thing being permuted.
        handles: dict[str, Fiber] = {}

        async def drive() -> tuple[dict[str, str], frozenset[str]]:
            for kind, key in sequence:
                target = (
                    service(key)
                    if kind == "provider"
                    else consumer(seen, key, needs[key])
                )
                handles[key] = host.root.plugin(target)
                await quiet(host)
            snapshot = (
                {key: fiber.state.name for key, fiber in handles.items()},
                binding_keys(host),
            )
            await host.dispose()
            return snapshot

        return asyncio.run(drive())

    assert run(order) == run(other)


# --------------------------------------------------------------------------
# PROP-INJECT-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    consumers=st.integers(min_value=1, max_value=6),
    replacements=st.integers(min_value=1, max_value=4),
)
@settings(deadline=None)
def test_replacing_an_implementation_reloads_every_consumer_once(
    consumers: int, replacements: int
) -> None:
    """Failure value: comparing implementations by equality rather than
    identity, so swapping in a fresh instance of a stateless-looking service
    does not reload anyone, and every consumer goes on calling the instance
    that was disposed."""

    async def drive() -> None:
        host = PluginHost()
        seen = Observed()
        provider = host.root.plugin(service("s0"), 0)
        fibers = [
            host.root.plugin(consumer(seen, f"c{index}", ("s0",)))
            for index in range(consumers)
        ]
        await quiet(host)
        assert all(fiber.state is FiberState.ACTIVE for fiber in fibers)

        for generation in range(1, replacements + 1):
            # A config change restarts the provider, which unbinds the old
            # instance and binds a fresh one. Restart is the realistic shape of
            # a replacement, and the one where the name never looks missing.
            await provider.update(generation)
            await quiet(host)

        for index in range(consumers):
            who = f"c{index}"
            # One load, plus one per replacement -- from the plan, not from
            # anything the runtime reports about itself.
            assert seen.loads(who) == replacements + 1, f"{who} reloaded wrongly"
            held = [entry[0] for entry in seen.held_at(who)]
            for earlier, later in itertools.pairwise(held):
                assert earlier is not later, f"{who} kept a replaced implementation"
            assert held[-1] is host.registry.lookup("s0", ctx=host.root.context)
        await host.dispose()

    asyncio.run(drive())
    asyncio.run(_swap_under_a_loading_consumer())


async def _swap_under_a_loading_consumer() -> None:
    """The same rule, at the moment where identity and equality differ.

    In the generated part above, the consumer sees its dependency *go missing*
    while the provider restarts, and would reload for that reason alone. The
    case that only an identity comparison catches is the one where it never
    sees the gap: a consumer still running its own body when the swap happens
    is told about it once, afterwards, when the name it needs is bound and
    always was -- by a different object than the one it was handed.
    """
    host = PluginHost()
    seen = Observed()
    gate = asyncio.Event()

    @inject("s0")
    async def slow(ctx: Context) -> None:
        seen.record("slow", ctx, ("s0",))
        await gate.wait()

    slow.__qualname__ = "slow"
    provider = host.root.plugin(service("s0"), 0)
    fiber = host.root.plugin(slow)
    await asyncio.sleep(0)
    assert state_of(fiber) == "LOADING", "the arrangement did not arrange"
    first = host.registry.lookup("s0", ctx=host.root.context)

    await provider.update(1)  # unbind, rebind: the consumer is busy throughout
    gate.set()
    await quiet(host)

    current = host.registry.lookup("s0", ctx=host.root.context)
    assert current is not first, "the provider did not actually replace anything"
    assert current == first, "equality and identity must disagree here"
    held = [entry[0] for entry in seen.held_at("slow")]
    assert held == [first, current], f"held {held}"
    assert state_of(fiber) == "ACTIVE"
    await host.dispose()


# --------------------------------------------------------------------------
# PROP-INJECT-004
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    plan=st.lists(st.sampled_from(POOL), min_size=1, max_size=5),
    needs=st.frozensets(st.sampled_from(POOL), min_size=1, max_size=3),
)
@settings(deadline=None)
def test_a_balanced_sequence_of_changes_leaves_the_system_as_it_was(
    plan: list[str], needs: frozenset[str]
) -> None:
    """Failure value: each unload/reload cycle leaving one subscription behind,
    so a provider that flaps during a config reload multiplies event handling
    by the number of flaps -- duplicated side effects, and no error at all."""

    async def drive() -> None:
        host = PluginHost()
        seen = Observed()
        live = {name: host.root.plugin(service(name)) for name in POOL}
        fiber = host.root.plugin(consumer(seen, "c0", tuple(sorted(needs))))
        await quiet(host)
        assert fiber.state is FiberState.ACTIVE
        # Bindings, live fibers and the consumer's effect tree: three things
        # that a flap can quietly accumulate, and one that it must.
        before = (
            binding_keys(host),
            fiber.state,
            len(host.runtime.fibers),
            len(fiber.effects().children),
        )

        # Every provider taken down is put back before the end: balanced by
        # construction rather than by filtering random sequences.
        for name in plan:
            await live.pop(name).dispose()
            await quiet(host)
            # Mid-sequence, not only at the end: a consumer that kept running
            # through the gap and was put right by the remount would balance
            # perfectly and still have spent the gap calling a disposed object.
            if name in needs:
                assert state_of(fiber) == "PENDING", f"c0 outlived {name}"
            live[name] = host.root.plugin(service(name))
            await quiet(host)

        after = (
            binding_keys(host),
            fiber.state,
            len(host.runtime.fibers),
            len(fiber.effects().children),
        )
        assert after == before
        assert set(states(host).values()) == {"ACTIVE"}
        # One reload per plan entry that took away something c0 declared, and
        # not one more: counted from the plan, not from the runtime.
        touched = sum(1 for name in plan if name in needs)
        assert seen.loads("c0") == touched + 1
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-INJECT-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    needs=st.frozensets(st.sampled_from(POOL[:2]), max_size=2),
    optional=st.frozensets(st.sampled_from(POOL[2:]), max_size=2),
    present=st.frozensets(st.sampled_from(POOL[2:]), max_size=2),
)
@settings(deadline=None)
def test_an_optional_lookup_never_changes_a_fiber_state(
    needs: frozenset[str], optional: frozenset[str], present: frozenset[str]
) -> None:
    """Failure value: treating a `ctx.get()` call as an implicit declaration,
    so a plugin that merely probes for an approval service is held PENDING on a
    deployment that deliberately has none."""

    def run(*, probes: frozenset[str]) -> dict[str, str]:
        host = PluginHost()
        seen = Observed()
        host.root.plugin(
            consumer(seen, "c0", tuple(sorted(needs)), optional=tuple(sorted(probes)))
        )
        for name in sorted(needs | present):
            host.root.plugin(service(name))

        async def drive() -> dict[str, str]:
            await quiet(host)
            snapshot = states(host)
            await host.dispose()
            return snapshot

        return asyncio.run(drive())

    # The comparison run is the same plugin set with the probes removed. Only
    # the optional lookups differ, so any difference in state is caused by one.
    assert run(probes=optional) == run(probes=frozenset())


# --------------------------------------------------------------------------
# PROP-INJECT-006
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    edges=st.lists(
        st.tuples(st.integers(0, len(POOL) - 1), st.integers(0, len(POOL) - 1)),
        max_size=6,
    ),
    spare=st.integers(min_value=0, max_value=2),
)
@settings(deadline=None)
def test_a_dependency_cycle_pends_its_members_and_is_reported_once(
    edges: list[tuple[int, int]], spare: int
) -> None:
    """Failure value: a cycle causing unbounded reload churn -- each member
    loading and unloading as the other's binding appears and vanishes --
    pinning a core with no error and no way to tell what is wrong."""
    graph: dict[str, set[str]] = {name: set() for name in POOL}
    for source, target in edges:
        if source != target:  # a self-edge is a different defect
            graph[POOL[source]].add(POOL[target])
    # By construction, so there is always something to find.
    graph[POOL[0]].add(POOL[1])
    graph[POOL[1]].add(POOL[0])

    async def drive() -> None:
        host = PluginHost()
        seen = Observed()
        fibers = {
            name: host.root.plugin(service(name, needs=sorted(graph[name])))
            for name in POOL
        }
        free = [host.root.plugin(consumer(seen, f"f{i}", ())) for i in range(spare)]
        await quiet(host)

        # The oracle is the generated graph, ordered by hand: it shares no code
        # with the runtime's readiness evaluation or with graphlib.
        stuck = unorderable(graph)
        for name, fiber in fibers.items():
            expected = FiberState.PENDING if name in stuck else FiberState.ACTIVE
            assert fiber.state is expected, f"{name}: {fiber.state.name}"
        assert all(fiber.state is FiberState.ACTIVE for fiber in free)

        # Asked again, with nothing changed. A standing condition that is
        # re-reported every time somebody looks buries the rest of the log, and
        # how many times the runtime happens to look is not something the test
        # should have to know.
        await quiet(host)
        host.runtime.audit()

        reported = [
            problem
            for problem in host.runtime.problems
            if isinstance(problem, DependencyCycleError)
        ]
        assert reported, "a cycle exists and nothing was reported"
        for problem in reported:
            # Sound: every reported cycle is a cycle in the generated graph.
            members = list(problem.cycle)
            for one, other in zip(members, members[1:] + members[:1], strict=True):
                assert one in graph[other] or other in graph[one], f"{members} is not"
            assert problem.names <= stuck, "a member that is not even blocked"
        # Reported once: a standing condition, not a per-turn complaint.
        assert len(reported) == len({problem.names for problem in reported})
        await host.dispose()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# PROP-INJECT-007
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    declared=st.frozensets(st.sampled_from(POOL), max_size=3),
    override=st.frozensets(st.sampled_from(POOL), max_size=3),
    present=st.frozensets(st.sampled_from(POOL), max_size=4),
)
@settings(deadline=None)
def test_an_explicit_requirement_replaces_the_declaration(
    declared: frozenset[str], override: frozenset[str], present: frozenset[str]
) -> None:
    """Failure value: an explicit `requires` that is merged with the target's
    own declaration instead of replacing it, so mounting a third-party plugin
    against a renamed service leaves it waiting for the name it was written
    for -- a plugin that can be pointed at nothing but its original wiring."""

    def run(*, needs: frozenset[str], requires: tuple[str, ...] | None) -> str:
        host = PluginHost()
        seen = Observed()
        target = consumer(seen, "c0", tuple(sorted(needs)))
        # `None` and `()` are different answers: read the declaration, versus
        # there is no declaration to read.
        fiber = host.root.plugin(target, requires=requires)
        for name in sorted(present):
            host.root.plugin(service(name))

        async def drive() -> str:
            await quiet(host)
            answer = state_of(fiber)
            expected = tuple(sorted(needs)) if requires is None else requires
            assert fiber.requires == expected
            await host.dispose()
            return answer

        return asyncio.run(drive())

    # Mounting with an override must be indistinguishable from mounting a
    # target that declared the override in the first place.
    overridden = run(needs=declared, requires=tuple(sorted(override)))
    native = run(needs=override, requires=None)
    assert overridden == native


def unorderable(graph: Mapping[str, Iterable[str]]) -> set[str]:
    """Every node that cannot be ordered: the cycles, plus what waits on them."""
    remaining = {node: set(deps) for node, deps in graph.items()}
    ordered: set[str] = set()
    while True:
        ready = {node for node, deps in remaining.items() if not (deps - ordered)}
        if not ready:
            return set(remaining)
        ordered |= ready
        for node in ready:
            remaining.pop(node)


# --------------------------------------------------------------------------
# The oracles' own checks
# --------------------------------------------------------------------------


def test_the_ordering_helper_agrees_with_graphlib() -> None:
    """graphlib must call the same graph unorderable -- and `c`, which only
    waits on a cycle, must be unorderable without being in one."""
    graph: dict[str, set[str]] = {"a": {"b"}, "b": {"a"}, "c": {"a"}, "d": set()}
    sorter = graphlib.TopologicalSorter({k: tuple(v) for k, v in graph.items()})
    with pytest.raises(graphlib.CycleError):
        sorter.prepare()
    assert unorderable(graph) == {"a", "b", "c"}
    assert unorderable({"a": (), "b": ("a",)}) == set()


def test_every_declaration_spelling_normalises_the_same() -> None:
    """The decorator, the class attribute and the module attribute agree."""

    class Db(Service):
        name = "db"

    @inject("shell", Db)
    def decorated(ctx: Context) -> None: ...

    class Declared:
        inject = ("shell", Db)

        def __init__(self, ctx: Context) -> None: ...

    module = type(asyncio)("plugin_module")
    module.inject = ("shell", Db)  # type: ignore[attr-defined]

    assert dependencies_of(decorated) == ("shell", "db")
    assert dependencies_of(Declared) == ("shell", "db")
    assert dependencies_of(module) == ("shell", "db")
    assert dependencies_of(lambda ctx: None) == ()
    assert provisions_of(service("s0")) == ("s0",)
    assert provisions_of(decorated) == ()


def test_importing_the_decorator_is_not_a_declaration() -> None:
    """`from cordis import inject` shadows the attribute a module declares with.

    Reading it as a declaration would turn an ordinary import into a mount
    failure, which is a poor trade for a spelling nobody chose.
    """
    module = type(asyncio)("plugin_module")
    module.inject = inject  # type: ignore[attr-defined]
    assert dependencies_of(module) == ()
