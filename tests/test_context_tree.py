"""PROP-CTX-001..006, transcribed from spec/capabilities/00-context-tree.yaml.

The trees here are built from a generated :class:`TreeSpec`, and every expected
answer is computed from that spec -- with a stdlib ``ChainMap`` where the card
asks for a model. Nothing reads the Context's own view to decide what the
Context's view should be.
"""

from __future__ import annotations

import copy
import inspect
from collections import ChainMap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cordis.context import RESERVED_NAMES, Context
from cordis.errors import ServiceNotFoundError
from tests.support import strategies as gen

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tests.support import TreeSpec


# --------------------------------------------------------------------------
# Building a real tree from a generated description
# --------------------------------------------------------------------------


@dataclass
class CountingResolver:
    """A service registry stand-in that only records that it was asked.

    PROP-CTX-004's oracle needs to observe *entry* into resolution, which is a
    different fact than the value resolution returns. Passing this in through
    the documented seam keeps the observation out of the implementation's
    internals.
    """

    calls: list[str] = field(default_factory=list)
    bindings: dict[str, object] = field(default_factory=dict)

    def lookup(self, token: type[Any] | str, /, *, ctx: Context) -> Any | None:
        del ctx
        self.calls.append(token if isinstance(token, str) else token.__qualname__)
        return self.bindings.get(token) if isinstance(token, str) else None


@dataclass(frozen=True)
class Tree:
    """A realised context tree, plus the bare context it was grown from.

    ``origin`` is kept separate because the spec's root node carries metadata
    and a bare ``Context()`` carries none; growing the spec root by ``extend``
    keeps derivation the only way a scope ever acquires metadata, which is the
    behaviour under test.
    """

    origin: Context
    nodes: dict[tuple[int, ...], Context]


def build_tree(spec: TreeSpec, *, resolver: CountingResolver | None = None) -> Tree:
    """Realise a TreeSpec as contexts, keyed by the same paths the spec uses."""
    origin = Context(resolver=resolver)
    nodes: dict[tuple[int, ...], Context] = {(): origin.extend(**spec.meta)}
    for path, node in spec.walk():
        if not path:
            continue
        nodes[path] = nodes[path[:-1]].extend(**node.meta)
    return Tree(origin=origin, nodes=nodes)


def model_view(spec: TreeSpec, path: tuple[int, ...]) -> ChainMap[str, int]:
    """The card's oracle: a ChainMap over the node's lineage, nearest first."""
    return ChainMap(*[dict(node.meta) for node in reversed(spec.chain(path))])


def all_keys(spec: TreeSpec) -> tuple[str, ...]:
    """Every key used anywhere in the tree."""
    return tuple({key for _, node in spec.walk() for key in node.meta})


def snapshot(
    spec: TreeSpec, nodes: Mapping[tuple[int, ...], Context]
) -> dict[tuple[int, ...], dict[str, object]]:
    """Every node's resolved view, read key by key and deep-copied.

    Deep-copied because the failure this witnesses is aliasing: a snapshot
    that shared the implementation's dict would compare equal to itself after
    the mutation and prove nothing.
    """
    keys = all_keys(spec)
    return {
        path: copy.deepcopy({key: ctx.get(key) for key in keys})
        for path, ctx in nodes.items()
    }


trees = gen.tree_specs()


# --------------------------------------------------------------------------
# PROP-CTX-001
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=trees, data=st.data())
def test_deriving_a_child_leaves_every_ancestor_untouched(
    spec: TreeSpec, data: st.DataObject
) -> None:
    """Failure value: deriving by ``dict(parent_meta)`` and handing the same
    dict to the child, so a later child write is visible to the parent and to
    every sibling."""
    nodes = build_tree(spec).nodes
    before = snapshot(spec, nodes)

    at = data.draw(gen.tree_paths(spec))
    extra = data.draw(gen.metadata())
    nodes[at].extend(**extra)

    assert snapshot(spec, nodes) == before


# --------------------------------------------------------------------------
# PROP-CTX-002
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=trees, data=st.data())
def test_resolution_agrees_with_a_chainmap_of_the_lineage(
    spec: TreeSpec, data: st.DataObject
) -> None:
    """Failure value: a parent-walk that stops at the first node carrying
    *any* metadata rather than the first node carrying *this key*, silently
    shadowing grandparent values whenever an intermediate context adds an
    unrelated key."""
    nodes = build_tree(spec).nodes
    path = data.draw(gen.tree_paths(spec))
    key = data.draw(st.sampled_from([*all_keys(spec), "unused", "absent"]))

    model = model_view(spec, path)
    ctx = nodes[path]

    if key in model:
        assert ctx.get(key) == model[key]
        assert ctx.require(key) == model[key]
        assert getattr(ctx, key) == model[key]
    else:
        assert ctx.get(key) is None
        with pytest.raises(ServiceNotFoundError) as caught:
            ctx.require(key)
        assert caught.value.name == key
        # SEM-002: the trail names every context walked. One entry per spec
        # node in the lineage, plus the bare root the tree was grown from.
        assert len(caught.value.searched) == len(spec.chain(path)) + 1


# --------------------------------------------------------------------------
# PROP-CTX-003
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(base=gen.metadata(), a=gen.metadata(), b=gen.metadata())
def test_chained_extends_equal_a_single_merged_extend(
    base: dict[str, int], a: dict[str, int], b: dict[str, int]
) -> None:
    """Failure value: a merge that treats falsy values as absent, so
    ``extend(x=0)`` fails to shadow an inherited ``x=1`` -- whether that
    happens on one construction path or on both.

    The dict comparison is not decoration. Mutation testing showed the
    metamorphic half alone accepts a merge that drops falsy values on *every*
    path, because then both sides drop the same key and still agree; the card
    was amended to require the model comparison as well.
    """
    root = Context().extend(**base)
    chained = root.extend(**a).extend(**b)
    merged = root.extend(**{**a, **b})

    keys = {*base, *a, *b, "unused"}
    model = {**base, **a, **b}
    chained_view = {k: chained.get(k) for k in keys}

    assert chained_view == {k: merged.get(k) for k in keys}
    assert chained_view == {k: model.get(k) for k in keys}


# --------------------------------------------------------------------------
# PROP-CTX-004
# --------------------------------------------------------------------------

#: Dunders the interpreter and the standard library probe for on arbitrary
#: objects. Every one of these is a real crash if it reaches the registry.
PROTOCOL_DUNDERS = (
    "__deepcopy__",
    "__copy__",
    "__await__",
    "__iter__",
    "__len__",
    "__getstate__",
    "__setstate__",
    "__wrapped__",
    "__bases__",
    "__test__",
    "__isabstractmethod__",
    "__fields__",
)

passthrough_names = st.one_of(
    st.sampled_from(PROTOCOL_DUNDERS),
    st.sampled_from(sorted(RESERVED_NAMES)),
    gen.identifiers.map(lambda name: f"_{name}"),
    gen.identifiers.map(lambda name: f"__{name}__"),
)


@pytest.mark.tier_local
@given(name=passthrough_names, spec=trees, data=st.data())
def test_reserved_names_never_reach_the_resolver(
    name: str, spec: TreeSpec, data: st.DataObject
) -> None:
    """Failure value: ``copy.deepcopy(ctx)`` or ``inspect.getmembers(ctx)``
    triggering a registry walk for ``__deepcopy__``, raising
    ServiceNotFoundError from inside stdlib and making Contexts
    un-inspectable in a debugger."""
    resolver = CountingResolver()
    nodes = build_tree(spec, resolver=resolver).nodes
    ctx = nodes[data.draw(gen.tree_paths(spec))]

    try:
        getattr(ctx, name)
    except ServiceNotFoundError as exc:  # pragma: no cover - the failure path
        pytest.fail(f"{name!r} was routed through service resolution: {exc}")
    except AttributeError:
        pass

    assert resolver.calls == []


@pytest.mark.tier_local
@given(spec=trees)
def test_contexts_survive_the_standard_library_probing_them(spec: TreeSpec) -> None:
    """The same rule, stated as the thing it protects: the operations a
    debugger, a test runner and ``copy`` perform must all work."""
    resolver = CountingResolver()
    ctx = build_tree(spec, resolver=resolver).nodes[()]

    assert inspect.getmembers(ctx)
    assert repr(ctx)
    assert not hasattr(ctx, "__deepcopy__")
    assert resolver.calls == []


# --------------------------------------------------------------------------
# PROP-CTX-005
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=trees, data=st.data())
def test_root_and_dict_membership_survive_further_derivation(
    spec: TreeSpec, data: st.DataObject
) -> None:
    """Failure value: defining ``__eq__`` for structural comparison without
    ``__hash__``, making Contexts unhashable -- or worse, making two distinct
    scopes collide as one registry key."""
    tree = build_tree(spec)
    root, nodes = tree.origin, tree.nodes
    markers = {ctx: path for path, ctx in nodes.items()}
    assert len(markers) == len(nodes)  # distinct scopes are distinct keys

    for _ in range(data.draw(st.integers(min_value=0, max_value=6))):
        parent = nodes[data.draw(gen.tree_paths(spec))]
        derived = parent.extend(**data.draw(gen.metadata()))
        assert derived.root is root
        assert derived not in markers

    for path, ctx in nodes.items():
        assert markers[ctx] == path
        assert ctx.root is root

    assert root.root is root


@pytest.mark.tier_local
@given(spec=trees, data=st.data())
def test_identity_is_nominal_not_class_based(
    spec: TreeSpec, data: st.DataObject
) -> None:
    """SEM-004: a Context from a second copy of the module still answers yes.

    Simulated by a class that carries the brand without sharing the class
    object, which is exactly what a vendored or reloaded import produces.
    """
    ctx = build_tree(spec).nodes[data.draw(gen.tree_paths(spec))]
    assert Context.is_context(ctx)

    class ForeignContext:
        __cordis_context__ = True

    assert Context.is_context(ForeignContext())
    assert not Context.is_context(object())
    assert not Context.is_context(None)


# --------------------------------------------------------------------------
# PROP-CTX-006
# --------------------------------------------------------------------------


@pytest.mark.tier_local
@given(spec=trees)
def test_a_child_stores_only_its_own_metadata(spec: TreeSpec) -> None:
    """Failure value: deriving by ``{**parent_view, **meta}``, which answers
    every lookup correctly while making derivation O(n) in the inherited view
    and a deep tree quadratic to build.

    No resolution-based assertion can see this defect -- a flattened child
    resolves everything correctly. Only the stored size can.
    """
    nodes = build_tree(spec).nodes

    planned = sum(len(node.meta) for _, node in spec.walk())
    stored = sum(len(ctx.own_meta) for ctx in nodes.values())
    assert stored == planned

    for path, node in spec.walk():
        assert dict(nodes[path].own_meta) == dict(node.meta)
