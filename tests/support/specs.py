"""Value types the generators produce, and the models that read them.

These are plain data. Nothing here imports the framework, which is the point:
a property's expected answer is computed from the *description* of the scenario
the test generated, never from the runtime that is supposed to realise it.
When a model method here duplicates logic the implementation also has, that
duplication is the property.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# Context trees
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeSpec:
    """A context tree: a label, the metadata contributed here, and children."""

    label: str
    meta: Mapping[str, int]
    children: tuple[TreeSpec, ...] = ()

    def walk(
        self, path: tuple[int, ...] = ()
    ) -> Iterator[tuple[tuple[int, ...], TreeSpec]]:
        """Every node, with the child-index path that reaches it."""
        yield path, self
        for index, child in enumerate(self.children):
            yield from child.walk((*path, index))

    def node_at(self, path: Sequence[int]) -> TreeSpec:
        node = self
        for index in path:
            node = node.children[index]
        return node

    def chain(self, path: Sequence[int]) -> tuple[TreeSpec, ...]:
        """Root-to-node lineage, root first."""
        nodes = [self]
        node = self
        for index in path:
            node = node.children[index]
            nodes.append(node)
        return tuple(nodes)

    def resolve(self, path: Sequence[int], key: str) -> int | None:
        """What a context at ``path`` should see for ``key``.

        The model for scoped-metadata inheritance: nearest definition wins,
        searching from the node upward. ``None`` means no ancestor defines it,
        which is the case that must raise rather than return a default.
        """
        for node in reversed(self.chain(path)):
            if key in node.meta:
                return node.meta[key]
        return None

    def size(self) -> int:
        return sum(1 for _ in self.walk())

    def depth(self) -> int:
        return max(len(path) for path, _ in self.walk()) + 1


# --------------------------------------------------------------------------
# Plugin dependency graphs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DagSpec:
    """Plugins and their required injections.

    ``edges[i]`` holds the indices plugin ``i`` depends on, and every one of
    them is strictly less than ``i``. Acyclicity is a property of how the
    value is built, not something a filter has to rediscover.
    """

    names: tuple[str, ...]
    edges: tuple[tuple[int, ...], ...]

    def dependencies(self, index: int) -> tuple[str, ...]:
        return tuple(self.names[dep] for dep in self.edges[index])

    def transitive(self, index: int) -> frozenset[str]:
        seen: set[int] = set()
        stack = list(self.edges[index])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self.edges[node])
        return frozenset(self.names[i] for i in seen)

    def order(self) -> tuple[str, ...]:
        """One valid activation order: index order is already topological."""
        return self.names

    def dependents(self, index: int) -> frozenset[str]:
        """Everything that must tear down when ``index`` goes away."""
        return frozenset(
            self.names[i]
            for i in range(len(self.names))
            if self.names[index] in self.transitive(i)
        )


# --------------------------------------------------------------------------
# Loader entries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntrySpec:
    """One row of a declarative configuration file."""

    id: str
    name: str
    config: Mapping[str, int]
    disabled: bool = False


@dataclass(frozen=True)
class EntryDiff:
    """What reconciling one entry list into another should do."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    reconfigured: tuple[str, ...]
    untouched: tuple[str, ...]


def diff_entries(old: Sequence[EntrySpec], new: Sequence[EntrySpec]) -> EntryDiff:
    """The model for loader reconciliation, keyed by entry id.

    Order is deliberately not part of the answer: an entry that only moved
    within the file must not remount, and a model that compared positionally
    would agree with an implementation that wrongly remounted it.
    """
    before = {e.id: e for e in old}
    after = {e.id: e for e in new}
    changed = tuple(
        eid
        for eid in after
        if eid in before
        and (
            dict(before[eid].config) != dict(after[eid].config)
            or before[eid].name != after[eid].name
            or before[eid].disabled != after[eid].disabled
        )
    )
    return EntryDiff(
        added=tuple(eid for eid in after if eid not in before),
        removed=tuple(eid for eid in before if eid not in after),
        reconfigured=changed,
        untouched=tuple(
            eid for eid in after if eid in before and eid not in set(changed)
        ),
    )


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------


class EffectShape(enum.Enum):
    """The return shapes an effect factory may legally produce.

    ``INVALID`` is included because the rejection path is a property too: an
    effect that returns something undisposable must raise *and* leave the
    scope's records untouched (effect-scope SEM-003).
    """

    NONE = "none"
    SYNC_DISPOSER = "sync-disposer"
    ASYNC_DISPOSER = "async-disposer"
    SYNC_CONTEXT = "sync-context"
    ASYNC_CONTEXT = "async-context"
    ITERABLE = "iterable"
    GENERATOR = "generator"
    ASYNC_GENERATOR = "async-generator"
    INVALID = "invalid"


#: Shapes that can contribute more than one disposer from a single effect.
COMPOSITE_SHAPES = frozenset(
    {EffectShape.ITERABLE, EffectShape.GENERATOR, EffectShape.ASYNC_GENERATOR}
)


VALID_SHAPES = tuple(s for s in EffectShape if s is not EffectShape.INVALID)


@dataclass(frozen=True)
class EffectSpec:
    """A description of one effect: its shape, its resources, its failure mode."""

    shape: EffectShape
    resources: tuple[str, ...]
    raises_on_setup: bool = False
    raises_on_dispose: bool = False

    @property
    def acquires(self) -> tuple[str, ...]:
        """Resources this effect holds once registered.

        Nothing is held if setup raises or the shape is rejected: an effect
        that cannot be undone is never recorded as done.
        """
        if self.raises_on_setup or self.shape is EffectShape.INVALID:
            return ()
        if self.shape is EffectShape.NONE:
            return ()
        return self.resources


# --------------------------------------------------------------------------
# Registry programs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvideOp:
    """Bind ``value`` to ``name`` in realm ``realm`` (an index, not a Realm).

    Realms are referred to by index so this description stays free of the
    framework: the test maps indices onto real Realm objects when it runs the
    program.
    """

    seq: int
    name: str
    realm: int
    value: str


@dataclass(frozen=True)
class DisposeOp:
    """Dispose the binding created by the provide with ``target`` as its seq.

    The target may already have been disposed, or may have been replaced by a
    later provider for the same key. Both are in the domain: an idempotent
    dispose and a stale disposer are the two ways this goes wrong in practice.
    """

    target: int


RegistryOp = ProvideOp | DisposeOp
