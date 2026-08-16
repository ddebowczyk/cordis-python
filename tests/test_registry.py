"""PROP-REG-001..006, transcribed from spec/capabilities/02-service-registry.yaml.

The oracle for the binding set is a plain dict maintained by the test, written
from the semantics rather than from the registry. Where a card asks for
identity, the test captures the object before the operation and compares with
``is`` afterwards -- nothing the implementation recomputes can satisfy that.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.errors import ServiceConflictError, ServiceNotFoundError
from cordis.registry import (
    DEFAULT_REALM,
    BindingChange,
    ChangeKind,
    Realm,
    Service,
    ServiceRegistry,
    enter_realm,
    realm_of,
)
from tests.support import ProvideOp
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from cordis.effect import EffectHandle
    from tests.support import RegistryOp, TreeSpec


class GateError(RuntimeError):
    """Raised by a gate that is generated to fail."""


def realm_table(count: int = 2) -> list[Realm]:
    """Sibling realms, so a program's realm index selects an isolated space.

    Siblings rather than nested: PROP-REG-001 and -006 are about the binding
    set, and nesting would let a lookup in one realm answer from another,
    which is a resolution question and belongs to PROP-REG-002.
    """
    return [DEFAULT_REALM, *(Realm(f"r{i}") for i in range(1, count))]


programs = gen.registry_programs()


# --------------------------------------------------------------------------
# PROP-REG-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(program=programs)
async def test_the_binding_set_matches_a_plain_dict_model(
    program: tuple[RegistryOp, ...],
) -> None:
    """Failure value: a disposer that removes the key unconditionally rather
    than only when it still points at its own binding, so disposing an old
    provider silently unregisters the replacement that took its place."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    realms = realm_table()

    model: dict[tuple[str, int], str] = {}
    issued: dict[int, ProvideOp] = {}
    handles: dict[int, EffectHandle] = {}

    for op in program:
        if isinstance(op, ProvideOp):
            key = (op.name, op.realm)
            issued[op.seq] = op
            if key in model:
                with pytest.raises(ServiceConflictError):
                    registry.provide(
                        op.name, op.value, scope=scope, realm=realms[op.realm]
                    )
            else:
                handles[op.seq] = registry.provide(
                    op.name, op.value, scope=scope, realm=realms[op.realm]
                )
                model[key] = op.value
        else:
            target = issued[op.target]
            key = (target.name, target.realm)
            handle = handles.get(op.target)
            if handle is not None:
                handle()
            # The model removes the key only while it still holds *this*
            # provider's value; a stale disposer must not evict a successor.
            if model.get(key) == target.value:
                del model[key]

    live = {
        (name, realms.index(realm)): info.value
        for (name, realm), info in registry.bindings().items()
    }
    assert live == model
    assert all(info.published for info in registry.bindings().values())


# --------------------------------------------------------------------------
# PROP-REG-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(names=gen.unique_names(min_size=1, max_size=6), data=st.data())
async def test_a_disposed_provider_stops_resolving(
    names: tuple[str, ...], data: st.DataObject
) -> None:
    """Failure value: holding the provider in a module-level cache for speed,
    so resolution keeps returning an object whose connections were already
    closed -- a "connection already closed" error surfacing in an unrelated
    plugin."""
    registry = ServiceRegistry()
    root = EffectScope("root")
    ctx = Context(resolver=registry)

    scopes = [root.child(f"provider{index}") for index in range(len(names))]
    values = {name: object() for name in names}
    for name, scope in zip(names, scopes, strict=True):
        registry.provide(name, values[name], scope=scope)

    order = data.draw(st.permutations(range(len(names))))
    gone: set[str] = set()

    for index in order:
        await scopes[index].dispose()
        gone.add(names[index])

        for name in names:
            if name in gone:
                assert registry.lookup(name, ctx=ctx) is None
                with pytest.raises(ServiceNotFoundError):
                    registry.resolve(name, ctx=ctx)
            else:
                assert registry.resolve(name, ctx=ctx) is values[name]


# --------------------------------------------------------------------------
# PROP-REG-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(names=gen.unique_names(min_size=1, max_size=5), data=st.data())
async def test_a_rejected_provider_leaves_the_incumbent_intact(
    names: tuple[str, ...], data: st.DataObject
) -> None:
    """Failure value: writing the new binding and then checking for conflict,
    so a rejected duplicate provider has already clobbered the incumbent by
    the time the error is raised."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    ctx = Context(resolver=registry)

    incumbents = {name: object() for name in names}
    for name, value in incumbents.items():
        registry.provide(name, value, scope=scope)

    contested = data.draw(st.sampled_from(sorted(names)))
    before = registry.bindings()

    with pytest.raises(ServiceConflictError) as caught:
        registry.provide(contested, object(), scope=scope)

    assert caught.value.name == contested
    assert registry.resolve(contested, ctx=ctx) is incumbents[contested]
    assert len(registry.bindings()) == len(before)
    for name, value in incumbents.items():
        assert registry.resolve(name, ctx=ctx) is value


# --------------------------------------------------------------------------
# PROP-REG-004
# --------------------------------------------------------------------------


@pytest.mark.tier_pr
@given(
    plan=st.lists(
        st.tuples(st.integers(min_value=0, max_value=4), st.booleans()),
        min_size=1,
        max_size=5,
    )
)
async def test_a_gated_service_is_invisible_until_its_gate_completes(
    plan: list[tuple[int, bool]],
) -> None:
    """Failure value: publishing the binding before awaiting the gate, so a
    consumer that started concurrently calls a service whose connection pool
    is still None."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    ctx = Context(resolver=registry)
    names = [f"svc{index}" for index in range(len(plan))]

    def make_gate(turns: int, *, fails: bool) -> Any:
        async def gate() -> AsyncGenerator[None, None]:
            for _ in range(turns):
                await asyncio.sleep(0)
            if fails:
                raise GateError(f"gate failed after {turns} turns")
            yield

        return gate

    handles = [
        registry.provide(
            name, object(), scope=scope, gate=make_gate(turns, fails=fails)
        )
        for name, (turns, fails) in zip(names, plan, strict=True)
    ]

    first_seen: dict[str, int] = {}
    horizon = max(turns for turns, _ in plan) + 4

    async def poll() -> None:
        for turn in range(horizon):
            for name in names:
                if name in first_seen:
                    continue
                if registry.lookup(name, ctx=ctx) is not None:
                    first_seen[name] = turn
            await asyncio.sleep(0)

    poller = asyncio.create_task(poll())
    for handle, (_, fails) in zip(handles, plan, strict=True):
        if fails:
            with pytest.raises(GateError):
                await handle
        else:
            await handle
    await poller

    for name, (turns, fails) in zip(names, plan, strict=True):
        if fails:
            assert name not in first_seen
            assert registry.lookup(name, ctx=ctx) is None
        else:
            assert first_seen[name] >= turns


# --------------------------------------------------------------------------
# PROP-REG-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(names=gen.unique_names(min_size=1, max_size=5), spec=gen.tree_specs())
async def test_class_and_name_lookups_reach_the_same_binding(
    names: tuple[str, ...], spec: TreeSpec
) -> None:
    """Failure value: a class-keyed fast path that caches by class identity
    and so misses a rebinding performed under the string name by the loader,
    splitting the registry into two disagreeing views."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    root = Context(resolver=registry)

    classes: list[type[Service]] = [
        type(f"Service{index}", (Service,), {"name": name})
        for index, name in enumerate(names)
    ]
    for cls in classes:
        registry.provide(cls.name, object(), scope=scope)

    contexts = [root]
    for _, node in spec.walk():
        contexts.append(contexts[-1].extend(**node.meta))

    for ctx in contexts:
        for cls in classes:
            assert registry.resolve(cls, ctx=ctx) is registry.resolve(cls.name, ctx=ctx)
            assert ctx.require(cls) is ctx.require(cls.name)


# --------------------------------------------------------------------------
# PROP-REG-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(program=programs)
async def test_every_binding_change_emits_exactly_one_notification(
    program: tuple[RegistryOp, ...],
) -> None:
    """Failure value: a spurious notification on a no-op dispose, which under
    the dependency tracking capability restarts every consumer of that service
    for no reason -- an unload storm triggered by an idempotent cleanup call."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    realms = realm_table()

    log: list[BindingChange] = []
    registry.observe(log.append)

    model: dict[tuple[str, int], str] = {}
    issued: dict[int, ProvideOp] = {}
    handles: dict[int, EffectHandle] = {}

    for op in program:
        before = dict(model)
        mark = len(log)

        if isinstance(op, ProvideOp):
            key = (op.name, op.realm)
            issued[op.seq] = op
            if key in model:
                with pytest.raises(ServiceConflictError):
                    registry.provide(
                        op.name, op.value, scope=scope, realm=realms[op.realm]
                    )
            else:
                handles[op.seq] = registry.provide(
                    op.name, op.value, scope=scope, realm=realms[op.realm]
                )
                model[key] = op.value
        else:
            target = issued[op.target]
            key = (target.name, target.realm)
            handle = handles.get(op.target)
            if handle is not None:
                handle()
            if model.get(key) == target.value:
                del model[key]

        # The expected change list is a diff of two snapshots of the model --
        # state before and after -- while the log comes from the registry's
        # own emission points. A forgotten emit and a spurious one are both
        # inequalities.
        expected = [
            BindingChange(ChangeKind.REMOVED, name, realms[realm])
            for (name, realm) in before
            if (name, realm) not in model
        ] + [
            BindingChange(ChangeKind.ADDED, name, realms[realm])
            for (name, realm) in model
            if (name, realm) not in before
        ]
        assert log[mark:] == expected


# --------------------------------------------------------------------------
# Realms
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(names=gen.unique_names(min_size=2, max_size=4))
async def test_an_inner_realm_overrides_what_it_binds_and_inherits_the_rest(
    names: tuple[str, ...],
) -> None:
    """SEM-002's "nearest enclosing realm", stated as the behaviour it buys:
    an isolated subtree replaces the providers it cares about without having
    to re-provide everything else."""
    registry = ServiceRegistry()
    scope = EffectScope("root")
    inner = DEFAULT_REALM.child("isolated")

    outer_values = {name: object() for name in names}
    for name, value in outer_values.items():
        registry.provide(name, value, scope=scope)

    overridden, *inherited = names
    replacement = object()
    registry.provide(overridden, replacement, scope=scope, realm=inner)

    outer_ctx = Context(resolver=registry)
    inner_ctx = enter_realm(outer_ctx, inner)

    assert realm_of(outer_ctx) is DEFAULT_REALM
    assert realm_of(inner_ctx) is inner
    assert registry.resolve(overridden, ctx=inner_ctx) is replacement
    assert registry.resolve(overridden, ctx=outer_ctx) is outer_values[overridden]
    for name in inherited:
        assert registry.resolve(name, ctx=inner_ctx) is outer_values[name]
