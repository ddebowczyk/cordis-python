"""The generator library.

One rule governs everything here: **construct valid values, never filter for
them**. A strategy that generates broadly and rejects with ``assume`` or
``.filter`` looks equivalent but is not -- it spends the example budget on
discards, and when the valid region is narrow it silently stops testing the
interesting part while continuing to report a full run.

So acyclicity comes from only ever drawing edges backwards, uniqueness comes
from index suffixes rather than a uniqueness filter, and a mutated entry list
is built by applying generated operations to a real list rather than by
generating two lists and hoping they overlap.

``tests/test_support.py`` enforces the rule mechanically: it parses this module
and fails if a ``.filter(`` or ``assume(`` appears without an explicit
``# discard-ok:`` justification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import strategies as st

from tests.support.specs import (
    COMPOSITE_SHAPES,
    VALID_SHAPES,
    DagSpec,
    DisposeOp,
    EffectShape,
    EffectSpec,
    EntrySpec,
    ProvideOp,
    RegistryOp,
    TreeSpec,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

_LOWER = "abcdefghijklmnopqrstuvwxyz"
_TAIL = _LOWER + "0123456789_"

#: A Python-ish identifier, assembled from a leading letter and a tail rather
#: than drawn from a regex. `from_regex` verifies its output with a filter, so
#: it is the one construction-shaped API that still discards.
identifiers = st.tuples(
    st.sampled_from(_LOWER),
    st.text(alphabet=_TAIL, max_size=7),
).map(lambda parts: parts[0] + parts[1])

service_names = identifiers
#: Slash-separated, the shape upstream uses for namespaced events.
event_names = st.lists(
    st.text(alphabet=_LOWER, min_size=1, max_size=6), min_size=1, max_size=3
).map("/".join)
labels = identifiers


def unique_names(
    min_size: int = 1, max_size: int = 6
) -> st.SearchStrategy[tuple[str, ...]]:
    """Distinct names. Uniqueness comes from the index, not from a filter."""
    return st.lists(identifiers, min_size=min_size, max_size=max_size).map(
        lambda stems: tuple(f"{stem}{index}" for index, stem in enumerate(stems))
    )


def distinct_delays(
    min_size: int = 1, max_size: int = 6, min_gap: float = 0.5, max_gap: float = 8.0
) -> st.SearchStrategy[tuple[float, ...]]:
    """Strictly increasing delays, for scheduling and timer properties.

    Built as a running sum of positive gaps. Distinctness is arithmetic, not a
    ``unique=True`` constraint -- which is a filter, and the measurably
    discard-heaviest thing that had been in this library.
    """
    return st.lists(
        st.floats(min_value=min_gap, max_value=max_gap),
        min_size=min_size,
        max_size=max_size,
    ).map(lambda gaps: tuple(sum(gaps[: i + 1]) for i in range(len(gaps))))


def metadata(max_size: int = 3) -> st.SearchStrategy[dict[str, int]]:
    """Scoped metadata. Values are ints so `0` exercises the falsy-shadowing case."""
    return st.dictionaries(
        st.sampled_from(["alpha", "beta", "gamma", "delta"]),
        st.integers(min_value=0, max_value=8),
        max_size=max_size,
    )


# --------------------------------------------------------------------------
# Context trees
# --------------------------------------------------------------------------


def _relabel(node: TreeSpec, path: tuple[int, ...] = ()) -> TreeSpec:
    """Give every node a label derived from its path.

    Labels must be unique for a ledger keyed by them, and deriving the label
    from the position makes a shrunk counterexample readable: `root.0.1` says
    where the node is without cross-referencing anything.
    """
    label = "root" if not path else "root." + ".".join(str(i) for i in path)
    return TreeSpec(
        label=label,
        meta=node.meta,
        children=tuple(
            _relabel(child, (*path, index)) for index, child in enumerate(node.children)
        ),
    )


def tree_specs(
    *, max_children: int = 3, max_leaves: int = 8
) -> st.SearchStrategy[TreeSpec]:
    """Context trees of arbitrary shape, with unique path-derived labels."""
    leaf = st.builds(
        TreeSpec, label=st.just("x"), meta=metadata(), children=st.just(())
    )
    tree = st.recursive(
        leaf,
        lambda children: st.builds(
            TreeSpec,
            label=st.just("x"),
            meta=metadata(),
            children=st.lists(children, max_size=max_children).map(tuple),
        ),
        max_leaves=max_leaves,
    )
    return tree.map(_relabel)


def tree_paths(tree: TreeSpec) -> st.SearchStrategy[tuple[int, ...]]:
    """A path into a tree that already exists, so it always addresses a node."""
    return st.sampled_from([path for path, _ in tree.walk()])


# --------------------------------------------------------------------------
# Dependency graphs
# --------------------------------------------------------------------------


@st.composite
def dag_specs(draw: st.DrawFn, min_size: int = 1, max_size: int = 6) -> DagSpec:
    """Plugin graphs that are acyclic because no other kind can be built.

    Node ``i`` may depend only on nodes before it, and the dependency set is
    decoded from an integer bitmask rather than drawn as a list with a
    uniqueness constraint -- no discards, and the mask shrinks toward zero,
    which is the empty dependency set.
    """
    names = draw(unique_names(min_size=min_size, max_size=max_size))
    edges: list[tuple[int, ...]] = []
    for i in range(len(names)):
        mask = draw(st.integers(min_value=0, max_value=(1 << i) - 1)) if i else 0
        edges.append(tuple(j for j in range(i) if mask >> j & 1))
    return DagSpec(names=names, edges=tuple(edges))


# --------------------------------------------------------------------------
# Loader entries
# --------------------------------------------------------------------------


@st.composite
def entry_lists(
    draw: st.DrawFn, min_size: int = 0, max_size: int = 6
) -> tuple[EntrySpec, ...]:
    """Configuration rows with distinct ids."""
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    plugin_names = draw(unique_names(min_size=1, max_size=3))
    return tuple(
        EntrySpec(
            id=f"e{index}",
            name=draw(st.sampled_from(plugin_names)),
            config=draw(metadata()),
            disabled=draw(st.booleans()),
        )
        for index in range(count)
    )


@st.composite
def reconfigurations(
    draw: st.DrawFn, entries: Sequence[EntrySpec]
) -> tuple[EntrySpec, ...]:
    """A second version of an entry list, built by editing the first.

    Generating two independent lists would almost always produce a diff that
    is "remove everything, add everything" -- technically valid and useless for
    testing reconciliation. Editing guarantees overlap, so the interesting
    cases (a row that moved, a row whose config changed) actually occur.
    """
    kept = [
        entry
        for entry in entries
        if draw(st.integers(min_value=0, max_value=3))  # 1-in-4 removed
    ]
    edited = [
        EntrySpec(
            id=entry.id,
            name=entry.name,
            config=draw(metadata()) if draw(st.booleans()) else entry.config,
            disabled=draw(st.booleans()) if draw(st.booleans()) else entry.disabled,
        )
        for entry in kept
    ]
    added = [
        EntrySpec(
            id=f"n{index}",
            name=draw(identifiers),
            config=draw(metadata()),
            disabled=False,
        )
        for index in range(draw(st.integers(min_value=0, max_value=2)))
    ]
    combined = edited + added
    order = draw(st.permutations(range(len(combined))))
    return tuple(combined[i] for i in order)


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------


@st.composite
def effect_specs(
    draw: st.DrawFn,
    *,
    prefix: str = "r",
    allow_invalid: bool = False,
    allow_failure: bool = True,
) -> EffectSpec:
    shapes = tuple(EffectShape) if allow_invalid else VALID_SHAPES
    shape = draw(st.sampled_from(shapes))
    # Only the composite shapes hold more than one resource; generating three
    # resources for a single sync disposer would describe a scenario the
    # implementation cannot produce.
    if shape in {EffectShape.NONE, EffectShape.INVALID}:
        count = 0
    elif shape in COMPOSITE_SHAPES:
        count = draw(st.integers(min_value=1, max_value=3))
    else:
        count = 1
    return EffectSpec(
        shape=shape,
        resources=tuple(f"{prefix}{i}" for i in range(count)),
        raises_on_setup=draw(st.booleans()) if allow_failure else False,
        raises_on_dispose=draw(st.booleans()) if allow_failure else False,
    )


@st.composite
def effect_plans(
    draw: st.DrawFn, *, max_size: int = 5, allow_invalid: bool = False
) -> tuple[EffectSpec, ...]:
    """Several effects whose resource ids do not collide across effects."""
    count = draw(st.integers(min_value=0, max_value=max_size))
    return tuple(
        draw(effect_specs(prefix=f"e{index}r", allow_invalid=allow_invalid))
        for index in range(count)
    )


# --------------------------------------------------------------------------
# Registry programs
# --------------------------------------------------------------------------


@st.composite
def registry_programs(
    draw: st.DrawFn, *, max_ops: int = 40, names: int = 3, realms: int = 2
) -> tuple[RegistryOp, ...]:
    """Interleaved provide and dispose operations over a small key space.

    The alphabet is deliberately tiny so conflicts and replacements happen by
    construction rather than by luck. Dispose targets are drawn from the
    provides already issued, so every target names a real registration --
    including ones already disposed, which is a case the contract has an
    answer for, not a case to discard.
    """
    count = draw(st.integers(min_value=0, max_value=max_ops))
    ops: list[RegistryOp] = []
    issued = 0
    for _ in range(count):
        if issued and draw(st.booleans()):
            ops.append(DisposeOp(target=draw(st.integers(0, issued - 1))))
        else:
            ops.append(
                ProvideOp(
                    seq=issued,
                    name=f"svc{draw(st.integers(0, names - 1))}",
                    realm=draw(st.integers(0, realms - 1)),
                    value=f"value{issued}",
                )
            )
            issued += 1
    return tuple(ops)
