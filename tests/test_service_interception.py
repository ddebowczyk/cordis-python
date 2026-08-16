"""PROP-INTC-001..004, from spec/capabilities/09-service-interception.yaml.

The model these tests compare against is written once, in `Plan`: the chain a
node sees for a name is the entries contributed along the path from the root to
that node, in that order, and nothing else. `merge_interceptions` is checked
against `collections.ChainMap`, which is stdlib and could not have inherited a
merge bug from the implementation -- the implementation is a left fold and does
not use it.
"""

from __future__ import annotations

import asyncio
from collections import ChainMap
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cordis.context import Context
from cordis.effect import EffectScope
from cordis.errors import InvalidPluginError
from cordis.intercept import (
    effective_config,
    intercept,
    intercept_all,
    intercepted_names,
    interceptions,
    merge_interceptions,
)
from cordis.plugin import PluginHost
from cordis.registry import Service, ServiceRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

NAMES = ("shell", "http", "store")
KEYS = ("timeout", "base", "tag", "retries")

#: Values include None on purpose: an explicit null is how a subtree clears an
#: inherited setting, and it is what naive merges drop.
VALUES = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=3),
    st.sampled_from(("a", "b")),
)

configs: st.SearchStrategy[dict[str, object]] = st.dictionaries(
    st.sampled_from(KEYS), VALUES, max_size=4
)


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One context in a generated tree."""

    index: int
    parent: int | None
    #: name -> the entry this node contributes for it
    entries: tuple[tuple[str, dict[str, object]], ...] = ()


@dataclass
class Plan:
    """A generated tree, and the chain arithmetic the cards are checked against.

    Deliberately ignorant of `cordis.intercept`: it knows only what the
    generator decided, which is what makes it an oracle rather than a second
    copy of the implementation.
    """

    nodes: tuple[Node, ...]

    def path(self, index: int) -> list[int]:
        """The root, then down to ``index``."""
        walk = [index]
        node = self.nodes[index]
        while node.parent is not None:
            walk.append(node.parent)
            node = self.nodes[node.parent]
        walk.reverse()
        return walk

    def chain(self, index: int, name: str) -> list[dict[str, object]]:
        """The entries ``index`` sees for ``name``, outermost first."""
        return [
            dict(entry)
            for step in self.path(index)
            for intercepted, entry in self.nodes[step].entries
            if intercepted == name
        ]


@st.composite
def plans(draw: st.DrawFn, size: int = 6) -> Plan:
    count = draw(st.integers(min_value=2, max_value=size))
    nodes = [Node(index=0, parent=None)]
    for index in range(1, count):
        parent = draw(st.integers(min_value=0, max_value=index - 1))
        entries = draw(
            st.lists(
                st.tuples(st.sampled_from(NAMES), configs),
                max_size=2,
                unique_by=lambda pair: pair[0],
            )
        )
        nodes.append(Node(index=index, parent=parent, entries=tuple(entries)))
    return Plan(tuple(nodes))


@dataclass
class World:
    """A plan, built."""

    registry: ServiceRegistry
    scope: EffectScope
    contexts: list[Context] = field(default_factory=list)


def build(plan: Plan) -> World:
    registry = ServiceRegistry()
    scope = EffectScope("test")
    world = World(registry=registry, scope=scope)
    for node in plan.nodes:
        if node.parent is None:
            ctx = Context(resolver=registry, label="n0")
        else:
            ctx = world.contexts[node.parent].extend(node=node.index)
            if node.entries:
                ctx = intercept_all(ctx, dict(node.entries))
        world.contexts.append(ctx)
    return world


# --------------------------------------------------------------------------
# PROP-INTC-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200)
@given(plan=plans())
def test_the_chain_reads_from_the_root_down(plan: Plan) -> None:
    """PROP-INTC-001: the chain is the path's entries, in the path's order.

    Failure value: building the chain child-to-root and forgetting to reverse,
    so an outer subtree's default overrides the inner subtree's explicit one.
    """
    world = build(plan)
    for node in plan.nodes:
        for name in NAMES:
            observed = interceptions(world.contexts[node.index], name)
            assert [dict(entry) for entry in observed] == plan.chain(node.index, name)


@pytest.mark.tier_local
@settings(max_examples=200)
@given(plan=plans())
def test_intercepting_a_child_leaves_the_parent_alone(plan: Plan) -> None:
    """PROP-INTC-001 (SEM-001): the parent's chain is what it was."""
    world = build(plan)
    before = {
        (node.index, name): interceptions(world.contexts[node.index], name)
        for node in plan.nodes
        for name in NAMES
    }
    for node in plan.nodes:
        intercept(world.contexts[node.index], NAMES[0], {"tag": "late"})
    for key, chain in before.items():
        index, name = key
        assert interceptions(world.contexts[index], name) == chain


# --------------------------------------------------------------------------
# PROP-INTC-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=200)
@given(plan=plans(), entry=configs)
def test_interception_never_changes_which_object_is_resolved(
    plan: Plan, entry: dict[str, object]
) -> None:
    """PROP-INTC-002: interception configures a service, it does not swap it.

    Failure value: implementing interception by binding a configured wrapper
    into the child's realm, which turns every interception into an isolation.
    """
    world = build(plan)
    values = {name: object() for name in NAMES}
    for name, value in values.items():
        world.registry.provide(name, value, scope=world.scope, ctx=world.contexts[0])
    for node in plan.nodes:
        parent = world.contexts[node.index]
        for name in NAMES:
            child = intercept(parent, name, entry)
            assert child.require(name) is parent.require(name)
            assert child.require(name) is values[name]


# --------------------------------------------------------------------------
# PROP-INTC-003
# --------------------------------------------------------------------------


class Configured:
    """A service that takes no part in interception beyond declaring defaults."""

    def __init__(self, name: str, defaults: Mapping[str, object]) -> None:
        self.name = name
        self.defaults = defaults


class Folding:
    """A service that folds the chain itself, and starts from its defaults."""

    def __init__(self, name: str, defaults: Mapping[str, object]) -> None:
        self.name = name
        self.defaults = defaults

    def resolve_interceptions(
        self, chain: Sequence[Mapping[str, object]], /
    ) -> Mapping[str, object]:
        return merge_interceptions((self.defaults, *chain))


@pytest.mark.tier_local
@settings(max_examples=200)
@given(defaults=configs, name=st.sampled_from(NAMES), folds=st.booleans())
def test_an_unintercepted_caller_sees_the_declared_default(
    defaults: dict[str, object], name: str, folds: bool
) -> None:
    """PROP-INTC-003: the un-intercepted path is free of surprises.

    Failure value: a resolver that answers with an empty mapping rather than
    the service's default when the chain is empty.
    """
    registry = ServiceRegistry()
    ctx = Context(resolver=registry, label="root")
    service = Folding(name, defaults) if folds else Configured(name, defaults)
    assert effective_config(ctx, service) == defaults
    # And a context intercepting some *other* name is still un-intercepted here.
    other = next(candidate for candidate in NAMES if candidate != name)
    assert effective_config(intercept(ctx, other, {"tag": "x"}), service) == defaults


# --------------------------------------------------------------------------
# PROP-INTC-004
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@settings(max_examples=400)
@given(chain=st.lists(configs, max_size=6))
def test_the_default_resolver_is_shallow_last_wins(
    chain: list[dict[str, object]],
) -> None:
    """PROP-INTC-004: last entry wins per key, and None is a value.

    Failure value: a merge that skips entries whose value is None, so a subtree
    cannot explicitly clear an inherited setting.

    The oracle is `ChainMap`; the implementation is a left fold, so the two are
    genuinely different pieces of code.
    """
    expected = dict(ChainMap(*reversed(chain))) if chain else {}
    assert merge_interceptions(chain) == expected


@pytest.mark.tier_local
@settings(max_examples=200)
@given(plan=plans(), name=st.sampled_from(NAMES), defaults=configs)
def test_the_effective_config_is_the_chain_folded_over_the_defaults(
    plan: Plan, name: str, defaults: dict[str, object]
) -> None:
    """PROP-INTC-004 over a real tree: defaults first, then the chain."""
    world = build(plan)
    service = Configured(name, defaults)
    for node in plan.nodes:
        observed = effective_config(world.contexts[node.index], service)
        expected = dict(ChainMap(*reversed([defaults, *plan.chain(node.index, name)])))
        assert observed == expected


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


def test_an_entry_is_frozen_against_the_caller() -> None:
    registry = ServiceRegistry()
    ctx = Context(resolver=registry, label="root")
    mine = {"timeout": 1}
    child = intercept(ctx, "shell", mine)
    mine["timeout"] = 999
    assert interceptions(child, "shell") == ({"timeout": 1},)
    with pytest.raises(TypeError):
        interceptions(child, "shell")[0]["timeout"] = 2  # type: ignore[index]


def test_intercepting_nothing_is_the_same_context() -> None:
    ctx = Context(resolver=ServiceRegistry(), label="root")
    assert intercept_all(ctx, {}) is ctx


def test_a_service_class_intercepts_under_its_name() -> None:
    class Shell(Service):
        name = "shell"

    ctx = Context(resolver=ServiceRegistry(), label="root")
    child = intercept(ctx, Shell, {"timeout": 1})
    assert interceptions(child, "shell") == ({"timeout": 1},)
    assert interceptions(child, Shell) == ({"timeout": 1},)


def test_intercepting_something_that_is_not_a_service_is_refused() -> None:
    ctx = Context(resolver=ServiceRegistry(), label="root")
    with pytest.raises(InvalidPluginError, match="service name"):
        intercept(ctx, 42, {"timeout": 1})  # type: ignore[arg-type]


def test_intercepted_names_reports_the_whole_lineage() -> None:
    ctx = Context(resolver=ServiceRegistry(), label="root")
    child = intercept(intercept(ctx, "shell", {}), "http", {})
    assert intercepted_names(child) == frozenset({"shell", "http"})
    assert intercepted_names(ctx) == frozenset()


def test_a_service_without_defaults_sees_an_empty_config() -> None:
    ctx = Context(resolver=ServiceRegistry(), label="root")

    class Bare:
        name = "shell"

    assert effective_config(ctx, Bare()) == {}
    intercepted = intercept(ctx, "shell", {"tag": "x"})
    assert effective_config(intercepted, Bare()) == {"tag": "x"}


def test_the_name_can_be_given_when_the_service_does_not_carry_one() -> None:
    root = Context(resolver=ServiceRegistry(), label="root")
    ctx = intercept(root, "shell", {"a": 1})
    assert effective_config(ctx, object(), name="shell") == {"a": 1}


def test_a_mounted_instance_keeps_its_interception_across_a_restart() -> None:
    """The interception belongs to the instance, not to the context object."""
    seen: list[Mapping[str, object]] = []

    class Shell(Service):
        name = "shell"
        defaults: ClassVar[Mapping[str, object]] = MappingProxyType({"timeout": 1})

    def body(ctx: Context) -> None:
        seen.append(effective_config(ctx, Shell))

    async def run() -> None:
        host = PluginHost()
        handle = host.root.plugin(body, intercept={"shell": {"timeout": 9}})
        await handle
        await handle.restart()
        await host.dispose()

    asyncio.run(run())
    assert seen == [{"timeout": 9}, {"timeout": 9}]


def test_the_plan_agrees_with_itself() -> None:
    """The generator's own arithmetic, on a shape written by hand."""
    plan = Plan(
        (
            Node(0, None, (("shell", {"timeout": 1}),)),
            Node(1, 0, (("shell", {"timeout": 2}),)),
            Node(2, 1, (("http", {"base": "x"}),)),
        )
    )
    assert plan.chain(2, "shell") == [{"timeout": 1}, {"timeout": 2}]
    assert plan.chain(0, "shell") == [{"timeout": 1}]
    assert plan.chain(2, "http") == [{"base": "x"}]


def test_the_effective_config_is_a_fresh_mapping_each_time() -> None:
    ctx = Context(resolver=ServiceRegistry(), label="root")
    service = Configured("shell", {"timeout": 1})
    first: Any = effective_config(ctx, service)
    second: Any = effective_config(ctx, service)
    assert first == second
    assert first is not second
    assert first is not service.defaults
