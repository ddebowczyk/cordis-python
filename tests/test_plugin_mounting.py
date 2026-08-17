"""PROP-PLUGIN-001..008, transcribed from spec/capabilities/03-plugin-mounting.yaml.

Three observation surfaces appear throughout, and none of them is the mount
path's own bookkeeping: a :class:`ResourceLedger` the plugin bodies write to, a
:class:`ServiceRegistry` binding view, and an :class:`EventBus` listener list.
A handle that reports "disposed" while leaking any of the three still fails.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import weakref
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cordis.errors import InactiveScopeError, InvalidPluginError
from cordis.events import Emit, EventBus
from cordis.fiber import FiberState
from cordis.plugin import PluginHost, config_of, scope_of
from cordis.registry import Service
from tests.support import FIBER_SETTLED, ResourceLedger
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from cordis.context import Context
    from cordis.effect import EffectNode
    from cordis.plugin import PluginHandle
    from tests.support import DagSpec, TreeSpec


class BodyError(RuntimeError):
    """Raised by a plugin body that the generator marked as failing."""


@dataclass
class World:
    """One application: a host, a bus, and the log its plugins write to."""

    host: PluginHost = field(default_factory=PluginHost)
    bus: EventBus = field(default_factory=EventBus)
    ledger: ResourceLedger = field(default_factory=ResourceLedger)
    seen: list[str] = field(default_factory=list)
    unwound: list[str] = field(default_factory=list)

    event: Emit[[]] = field(default_factory=lambda: Emit("probe"))

    @property
    def root(self) -> PluginHandle:
        return self.host.root

    @property
    def ctx(self) -> Context:
        return self.host.root.context

    def bindings(self) -> set[str]:
        return {name for name, _realm in self.host.registry.bindings()}

    def listeners(self) -> int:
        return len(self.bus.listeners(self.event))


def contributor(
    world: World, name: str, *, children: tuple[TreeSpec, ...] = ()
) -> Callable[[Context], None]:
    """A plugin that provides, listens, acquires, and mounts its children.

    Everything it does is visible on a surface the mount path does not own,
    which is what makes "nothing remains live" a checkable claim.
    """

    def apply(ctx: Context) -> None:
        scope = scope_of(ctx)
        world.host.registry.provide(name, f"value-{name}", scope=scope)
        world.bus.on(world.event, partial(world.seen.append, name), scope=scope)
        scope.effect(partial(world.ledger.disposer, name), label=name)
        scope.effect(lambda: partial(world.unwound.append, name), label=f"early:{name}")
        for child in children:
            ctx.plugin(contributor(world, child.label, children=child.children))
        # Registered *after* the children, so a flat reverse-order unwind would
        # run it before they are gone. That is the defect PROP-PLUGIN-003 names,
        # and the early probe above cannot witness it: LIFO already runs it last.
        scope.effect(
            lambda: partial(world.unwound.append, f"{name}!"), label=f"late:{name}"
        )

    return apply


# --------------------------------------------------------------------------
# PROP-PLUGIN-001
# --------------------------------------------------------------------------


def lazy_plugin(world: World, name: str, deps: tuple[str, ...]) -> Callable[..., None]:
    """A plugin whose dependencies are resolved when its service is *called*.

    Late resolution is what makes mount order irrelevant: the body itself
    reads nothing from the registry, so there is no moment at which "my
    dependency has not been mounted yet" can be observed.
    """

    def apply(ctx: Context) -> None:
        scope = scope_of(ctx)

        def service() -> tuple[str, ...]:
            return (name, *(dep for dep in deps if ctx.require(dep) is not None))

        world.host.registry.provide(name, service, scope=scope)
        world.bus.on(world.event, partial(world.seen.append, name), scope=scope)

    return apply


def snapshot(world: World) -> dict[str, object]:
    """Everything about a finished application that mount order must not touch."""
    return {
        "bindings": sorted(world.bindings()),
        "listeners": sorted(world.seen),
        "answers": sorted(
            tuple(world.host.registry.resolve(name, ctx=world.ctx)())
            for name, _realm in world.host.registry.bindings()
        ),
    }


async def run_permutation(spec: DagSpec, order: tuple[int, ...]) -> dict[str, object]:
    world = World()
    handles = {}
    for index in order:
        name = spec.names[index]
        handles[name] = world.ctx.plugin(
            lazy_plugin(world, name, spec.dependencies(index))
        )
    for handle in handles.values():
        await handle
    await world.bus.emit(world.event)
    return {**snapshot(world), "states": sorted(h.state.name for h in handles.values())}


@pytest.mark.tier_pr
@given(spec=gen.dag_specs(min_size=2, max_size=6), data=st.data())
async def test_mount_order_does_not_affect_the_final_state(
    spec: DagSpec, data: st.DataObject
) -> None:
    """Failure value: a plugin that reads a service in its body instead of
    declaring it as an injection, which happens to work when it is mounted
    last and fails when the config file rows are reordered."""
    size = len(spec.names)
    first = data.draw(st.permutations(range(size)))
    second = data.draw(st.permutations(range(size)))

    # Two independent executions in fresh roots; the only shared input is the
    # plugin set, so agreement cannot come from a shared cache.
    assert await run_permutation(spec, tuple(first)) == await run_permutation(
        spec, tuple(second)
    )


# --------------------------------------------------------------------------
# PROP-PLUGIN-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=gen.tree_specs(max_children=3, max_leaves=8))
async def test_disposing_a_root_leaves_nothing_live(spec: TreeSpec) -> None:
    """Failure value: a plugin that mounts children on the *parent* context (a
    one-character mistake: a captured outer ``ctx``), so its children survive
    its own unload and keep serving stale state."""
    world = World()
    planned = {node.label for _path, node in spec.walk()}

    handle = world.ctx.plugin(contributor(world, spec.label, children=spec.children))
    await handle

    assert world.bindings() == planned
    assert world.listeners() == len(planned)
    assert world.ledger.live == frozenset(planned)

    # The tree's own root, not the host root: a plugin whose children were
    # mounted on someone else's context still disappears when the host does,
    # so disposing the host cannot witness the defect this card names.
    await handle.dispose()

    assert world.ledger.balanced
    assert world.ledger.counts() == dict.fromkeys(planned, 1)
    assert world.bindings() == set()
    assert world.listeners() == 0
    assert handle.state is FiberState.DISPOSED
    assert world.root.children == ()


# --------------------------------------------------------------------------
# PROP-PLUGIN-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=gen.tree_specs(max_children=3, max_leaves=8))
async def test_disposal_runs_strictly_bottom_up(spec: TreeSpec) -> None:
    """Failure value: iterating children and parent effects in one flat reverse
    list, so a parent's database handle is closed while a child's
    flush-on-dispose is still writing through it."""
    world = World()
    await world.ctx.plugin(contributor(world, spec.label, children=spec.children))
    await world.root.dispose()

    # The tree comes from the generator's construction plan; the order comes
    # from runtime. Neither is produced by the traversal under test.
    position = {label: index for index, label in enumerate(world.unwound)}
    labels = {node.label for _path, node in spec.walk()}
    assert set(position) == labels | {f"{label}!" for label in labels}

    def descendants(node: TreeSpec) -> set[str]:
        return {
            label
            for child in node.children
            for label in ({child.label, f"{child.label}!"} | descendants(child))
        }

    for path, node in spec.walk():
        # "Before its ancestor's own effects begin unwinding": the ancestor's
        # earliest-unwinding effect is the one it registered last.
        first_ancestor_effect = min(position[node.label], position[f"{node.label}!"])
        for label in descendants(node):
            assert position[label] < first_ancestor_effect, (
                f"{label} unwound after an effect of its ancestor {node.label} "
                f"at {path}"
            )


@pytest.mark.tier_local
@given(spec=gen.tree_specs(max_children=3, max_leaves=8))
async def test_siblings_unwind_newest_first(spec: TreeSpec) -> None:
    """SEM-004 says *reverse* mount order, which bottom-up alone does not say.

    Failure value: iterating a parent's children forward, so the plugin it
    mounted first -- the one its later siblings were written against, because
    it was there when they loaded -- is torn down while they are still using
    it. Every descendant still precedes every ancestor, so PROP-PLUGIN-003 is
    satisfied by this defect.
    """
    world = World()
    await world.ctx.plugin(contributor(world, spec.label, children=spec.children))
    await world.root.dispose()

    position = {label: index for index, label in enumerate(world.unwound)}

    def subtree(node: TreeSpec) -> set[str]:
        return {node.label, f"{node.label}!"}.union(
            *(subtree(child) for child in node.children), set()
        )

    # A child is disposed whole before the next one starts, so the later
    # sibling's entire subtree comes before any of the earlier sibling's.
    for _path, node in spec.walk():
        for earlier, later in itertools.pairwise(node.children):
            latest = max(position[label] for label in subtree(later))
            earliest = min(position[label] for label in subtree(earlier))
            assert latest < earliest, (
                f"{earlier.label} was mounted before {later.label} but unwound "
                f"before it finished"
            )


# --------------------------------------------------------------------------
# PROP-PLUGIN-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    shape=st.lists(
        st.tuples(st.booleans(), st.integers(min_value=0, max_value=3)),
        min_size=1,
        max_size=6,
    )
)
async def test_a_failing_plugin_leaves_its_siblings_operational(
    shape: list[tuple[bool, int]],
) -> None:
    """Failure value: letting the exception escape the mount call so it
    propagates into the loader's task group and cancels the concurrent
    mounting of every other row -- one bad config field taking down the whole
    application."""
    world = World()
    names = [f"p{index}" for index in range(len(shape))]
    failing = {names[index] for index, (fails, _) in enumerate(shape) if fails}
    acquired: dict[str, list[str]] = {}

    def sibling(name: str, *, fails: bool, resources: int) -> Callable[[Context], None]:
        def apply(ctx: Context) -> None:
            scope = scope_of(ctx)
            acquired[name] = []
            for n in range(resources):
                resource = f"{name}.{n}"
                scope.effect(partial(world.ledger.disposer, resource), label=resource)
                acquired[name].append(resource)
            if fails:
                raise BodyError(name)
            world.host.registry.provide(name, f"value-{name}", scope=scope)

        return apply

    handles = {
        name: world.ctx.plugin(sibling(name, fails=fails, resources=resources))
        for name, (fails, resources) in zip(names, shape, strict=True)
    }

    for name, handle in handles.items():
        if name in failing:
            with pytest.raises(BodyError):
                await handle
            assert handle.state is FiberState.FAILED
        else:
            await handle
            assert handle.state is FiberState.ACTIVE

    # Sibling health is observed through the registry, not through the handles
    # the failure path also touches.
    assert world.bindings() == set(names) - failing
    for name in set(names) - failing:
        assert world.host.registry.resolve(name, ctx=world.ctx) == f"value-{name}"

    counts = world.ledger.counts()
    for name in failing:
        for resource in acquired[name]:
            assert counts[resource] == 1
    for name in set(names) - failing:
        for resource in acquired[name]:
            assert resource in world.ledger.live


# --------------------------------------------------------------------------
# PROP-PLUGIN-005
# --------------------------------------------------------------------------


def count_nodes(node: EffectNode) -> int:
    """Every scope and every registered effect below (and including) a node."""
    return 1 + sum(count_nodes(child) for child in node.children)


class Bare:
    """A class that is neither a Service subclass nor exposes ``apply``."""


class Opaque:
    """An object whose ``apply`` is not callable."""

    apply = 42


def no_parameters() -> None: ...


def too_many(ctx: object, config: object, extra: object) -> None: ...


def yielding(ctx: object) -> Iterator[None]:
    """Right arity, wrong kind: calling it runs none of its body."""
    yield


async def async_yielding(ctx: object) -> AsyncIterator[None]:
    yield


class YieldingModule:
    apply = staticmethod(yielding)


NON_PLUGINS: tuple[object, ...] = (
    0,
    "plugin",
    b"plugin",
    3.5,
    {"apply": lambda ctx: None},
    [lambda ctx: None],
    None,
    Bare,
    Bare(),
    Opaque(),
    no_parameters,
    too_many,
    yielding,
    async_yielding,
    YieldingModule(),
)


@pytest.mark.tier_local
@given(target=st.sampled_from(NON_PLUGINS))
async def test_a_rejected_target_creates_nothing(target: object) -> None:
    """Failure value: creating the child context and scope before validating
    the target, so every rejected mount leaks a scope that nothing will ever
    dispose -- invisible until a config typo is retried in a reload loop."""
    world = World()

    # A context's label carries the number of children its parent has derived,
    # so two probes taken around the rejected mount must be consecutive.
    before = world.ctx.extend().label
    scopes_before = count_nodes(world.root.scope.tree())
    bindings_before = world.bindings()

    with pytest.raises(InvalidPluginError) as caught:
        world.ctx.plugin(target)

    assert caught.value.plugin
    after = world.ctx.extend().label
    assert (before, after) == (f"{world.ctx.label}.0", f"{world.ctx.label}.1")
    assert count_nodes(world.root.scope.tree()) == scopes_before
    assert world.bindings() == bindings_before
    assert world.root.children == ()


# --------------------------------------------------------------------------
# PROP-PLUGIN-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(configs=gen.unique_names(min_size=2, max_size=6))
async def test_the_same_target_mounts_as_independent_instances(
    configs: tuple[str, ...],
) -> None:
    """Failure value: caching normalised plugin metadata on the target
    *including* the resolved config, so the second mount of the same module
    silently runs with the first mount's configuration."""
    world = World()
    observed: list[object] = []

    def apply(ctx: Context, config: object) -> None:
        observed.append(config_of(ctx))
        assert config_of(ctx) is config

    handles = [world.ctx.plugin(apply, config) for config in configs]
    for handle in handles:
        await handle

    assert observed == list(configs)
    contexts = [handle.context for handle in handles]
    scopes = [handle.scope for handle in handles]
    assert len({id(ctx) for ctx in contexts}) == len(configs)
    assert len({id(scope) for scope in scopes}) == len(configs)


@pytest.mark.tier_local
async def test_a_target_that_cannot_be_a_weak_key_still_mounts() -> None:
    """Failure value: deciding cacheability by reading
    `type(target).__weakrefoffset__`, which answers only half the question --
    an unhashable target raises `TypeError` out of the cache lookup -- and is a
    CPython detail besides, so on PyPy, where the attribute does not exist,
    every mount raises `AttributeError` before a plugin ever runs."""
    world = World()

    class Slotted:
        """No `__weakref__` slot, so instances hold no weak references."""

        __slots__ = ("seen",)

        def __init__(self) -> None:
            self.seen: list[str] = []

        def apply(self, ctx: Context) -> None:
            self.seen.append("up")

    class Unhashable:
        """Defining `__eq__` sets `__hash__` to None: no dict may key on it."""

        def __init__(self) -> None:
            self.seen: list[str] = []

        def __eq__(self, other: object) -> bool:
            return self is other

        def apply(self, ctx: Context) -> None:
            self.seen.append("up")

    slotted, unhashable = Slotted(), Unhashable()

    # An unhashable target cannot be a key in any dict, on any interpreter.
    with pytest.raises(TypeError):
        hash(unhashable)
    # `slotted` is the other half -- on CPython there is no weak reference to
    # take. PyPy allows one regardless, which is precisely why the question is
    # asked of the cache rather than of the target's type.
    if sys.implementation.name == "cpython":
        with pytest.raises(TypeError):
            weakref.ref(slotted)

    # Mounted twice over, so the second pass reaches the path that would have
    # read a cache entry the first pass could not have written.
    for _ in range(2):
        await world.ctx.plugin(slotted)
        await world.ctx.plugin(unhashable)

    assert slotted.seen == ["up", "up"]
    assert unhashable.seen == ["up", "up"]


# --------------------------------------------------------------------------
# PROP-PLUGIN-007
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    plan=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.booleans()),
        min_size=1,
        max_size=5,
    )
)
async def test_awaiting_a_mount_waits_for_it_to_settle(
    plan: list[tuple[int, bool]],
) -> None:
    """Failure value: an awaited mount resolving as soon as the body's
    coroutine is scheduled, so a startup script that awaits its plugins
    proceeds before they have provided anything and every subsequent lookup
    fails intermittently."""
    world = World()
    raised: dict[str, BodyError] = {}

    def slow(name: str, *, turns: int, fails: bool) -> Callable[[Context], Any]:
        async def apply(ctx: Context) -> None:
            for _ in range(turns):
                await asyncio.sleep(0)
            if fails:
                error = BodyError(name)
                raised[name] = error
                raise error
            world.host.registry.provide(name, f"value-{name}", scope=scope_of(ctx))

        return apply

    handles = {}
    for index, (turns, fails) in enumerate(plan):
        name = f"p{index}"
        handles[name] = world.ctx.plugin(slow(name, turns=turns, fails=fails))
        # The mount call returned without waiting: an async body has not run.
        assert handles[name].state is FiberState.LOADING

    for index, (_turns, fails) in enumerate(plan):
        name = f"p{index}"
        handle = handles[name]
        if fails:
            with pytest.raises(BodyError) as caught:
                await handle
            # Identity, not type: no re-wrapping can satisfy this.
            assert caught.value is raised[name]
            assert handle.state is FiberState.FAILED
        else:
            assert await handle is handle
            assert handle.state is FiberState.ACTIVE
            assert world.host.registry.resolve(name, ctx=world.ctx) == f"value-{name}"
        assert handle.state.name in FIBER_SETTLED


# --------------------------------------------------------------------------
# The supported plugin forms (SEM-001), stated as what each one buys
# --------------------------------------------------------------------------


@pytest.mark.tier_local
async def test_every_supported_plugin_form_mounts() -> None:
    world = World()
    seen: list[str] = []

    def one_parameter(ctx: Context) -> None:
        seen.append(f"callable/{ctx.label}")

    def two_parameters(ctx: Context, config: object) -> None:
        seen.append(f"configured/{config}")

    class Module:
        @staticmethod
        def apply(ctx: Context, config: object) -> None:
            seen.append(f"apply/{config}")

    class Database(Service):
        name = "db"

        def __init__(self, ctx: Context, config: object) -> None:
            self.config = config
            seen.append(f"service/{config}")

    for target, config in (
        (one_parameter, None),
        (two_parameters, "cfg"),
        (Module(), "mod"),
        (Database, "dsn"),
    ):
        await world.ctx.plugin(target, config)

    assert seen == [
        f"callable/{world.ctx.label}.0",
        "configured/cfg",
        "apply/mod",
        "service/dsn",
    ]
    # A Service subclass provides its instance under the declared name.
    resolved = world.host.registry.resolve(Database, ctx=world.ctx)
    assert isinstance(resolved, Database)
    assert resolved.config == "dsn"

    await world.root.dispose()
    assert world.bindings() == set()


# --------------------------------------------------------------------------
# PROP-PLUGIN-008
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(
    turns=st.integers(min_value=0, max_value=3),
    fails=st.booleans(),
    settle=st.booleans(),
)
async def test_mounting_into_an_instance_that_is_gone_is_refused(
    turns: int, fails: bool, settle: bool
) -> None:
    """Failure value: deciding on the scope's liveness instead of the
    instance's state, so mounting onto a just-failed parent is accepted for
    however many turns its rollback takes to run -- the child is built, its
    body runs, and the rollback then unwinds it, all invisibly."""
    world = World()

    def body(ctx: Context) -> None:
        if fails:
            raise BodyError("body")

    handle = world.ctx.plugin(body)
    if settle and fails:
        # Settling changes when the scope dies, and must not change the answer.
        with pytest.raises(BodyError):
            await handle
    elif settle:
        await handle
    for _ in range(turns):
        await asyncio.sleep(0)

    if not fails and not settle:
        # The live case, stated so the test cannot pass by refusing everything.
        assert handle.state is FiberState.ACTIVE
        await handle.plugin(lambda ctx: None)
    await handle.dispose()

    with pytest.raises(InactiveScopeError) as caught:
        handle.plugin(lambda ctx: None)
    assert caught.value.operation == "mount a plugin"
    assert handle.children == ()

    # A failed instance refuses just the same, and refuses immediately: the
    # answer does not depend on how many turns have passed since it failed.
    if fails:
        broken = world.ctx.plugin(body)
        assert broken.state is FiberState.FAILED
        with pytest.raises(InactiveScopeError):
            broken.plugin(lambda ctx: None)
        with pytest.raises(BodyError):
            await broken
