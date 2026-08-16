"""Scoped registration: many live subjects, one plugin tree.

Implements ``spec/capabilities/18-scoped-registration.yaml``.

Isolation realms (``cordis.realm``) are decided when the tree is built. Live
subjects are not: sessions start and end, agents spawn sub-agents, and each one
needs its own entries in shared registries without a plugin tree of its own. A
:class:`Scope` is that lifetime -- a mounted instance whose context every
contribution for the subject is registered through, so ending the subject is an
ordinary unload rather than a sweep of every registry in the process.

Two directions, deliberately opposite, and one relation:

* **Visibility inherits down.** A contribution registered at scope S is visible
  to S and to every scope created from it (SEM-002). A sub-agent sees the tools
  its parent contributed; a sibling does not.
* **Events travel up.** A dispatch carried by scope S reaches listeners
  registered at S and at every *ancestor* of S (SEM-003). A supervisor hears
  its sub-agent; the sub-agent does not hear its siblings.

Both are :func:`admits`, asked with the two arguments in different roles. There
is one implementation of the relation, which is why the sign error that would
make a supervisor deaf and every sibling a subscriber cannot be made in one
direction and not the other.

Scoping is a routing mechanism and not a trust boundary (SEM-005). Nothing here
withholds anything from a caller who holds the registry: :meth:`ScopedRegistry.
all` returns everything, on purpose, because diagnostics and session recorders
need it and hiding it would be theatre rather than security.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Generic, TypeVar

from cordis.filter import FILTER_KEY, filter_of
from cordis.plugin import scope_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cordis.context import Context
    from cordis.effect import EffectHandle
    from cordis.filter import Filter
    from cordis.plugin import PluginHandle

__all__ = [
    "SUBJECT_KEY",
    "Scope",
    "ScopedRegistry",
    "admits",
    "create_scope",
    "scope_target",
    "subject_of",
]

#: The scoped-metadata key carrying a context's subject scope. Present and
#: ``None`` means "explicitly nobody's", which is not the same as absent.
SUBJECT_KEY: Final = "__subject__"

T = TypeVar("T")


class Scope:
    """One subject's lifetime, and the context its contributions go through.

    A scope is created from a context and owns a mounted instance; disposing it
    unwinds everything registered through :attr:`ctx`, descendants first, and
    the disposal is complete when it returns (SEM-001, SEM-006).
    """

    # `__weakref__` is explicit because PROP-SCOPE-005 measures liveness with
    # weak references: a scope nothing can point at weakly cannot be shown to
    # have been collected.
    __slots__ = ("__weakref__", "_ctx", "_handle", "_key", "_label", "_parent")

    def __init__(self, key: object, parent: Scope | None, label: str) -> None:
        self._key = key
        self._parent = parent
        self._label = label
        self._ctx: Context | None = None
        self._handle: PluginHandle | None = None

    @property
    def key(self) -> object:
        """The subject itself, compared by identity and never interpreted."""
        return self._key

    @property
    def label(self) -> str:
        """What this lifetime is called in the fiber and effect trees."""
        return self._label

    @property
    def parent(self) -> Scope | None:
        """The scope this one was created from, or ``None`` at the top."""
        return self._parent

    @property
    def ctx(self) -> Context:
        """The context to register this subject's contributions through."""
        if self._ctx is None:  # pragma: no cover - unreachable by construction
            msg = "scope context requested before the scope was mounted"
            raise RuntimeError(msg)
        return self._ctx

    def lineage(self) -> Iterator[Scope]:
        """This scope, then its ancestors, nearest first."""
        node: Scope | None = self
        while node is not None:
            yield node
            node = node._parent  # noqa: SLF001 -- the same class

    def covers(self, other: Scope | None, /) -> bool:
        """Whether this scope is ``other`` or an ancestor of it."""
        return other is not None and any(node is self for node in other.lineage())

    async def dispose(self) -> None:
        """End the subject: descendant scopes first, then everything here."""
        if self._handle is not None:
            await self._handle.dispose()

    def __repr__(self) -> str:
        return f"<Scope {self._label} key={type(self._key).__name__}>"

    def _bind(self, handle: PluginHandle, ctx: Context) -> None:
        self._handle = handle
        self._ctx = ctx


def create_scope(ctx: Context, key: object, /, *, label: str | None = None) -> Scope:
    """Start a subject under ``ctx``, and return its scope.

    The lifetime owner is a mounted instance rather than a bare context: a
    context has no disposal hook, and subject teardown has to be the same
    machinery as every other unload or it will diverge from it (SEM-001).

    The new scope's parent is whatever scope ``ctx`` is already in, so the
    scope tree and the fiber tree are the same tree -- which is what makes
    SEM-006's "descendants first, and the disposal completes only after all of
    them have" inherited rather than reimplemented.
    """
    name = label or _describe(key)
    scope = Scope(key=key, parent=subject_of(ctx), label=name)
    handle: PluginHandle = ctx.plugin(_owner(name))
    scope._bind(handle, _scoped(handle.context, scope, filter_of(ctx)))  # noqa: SLF001
    return scope


def subject_of(ctx: Context, /) -> Scope | None:
    """The scope ``ctx`` registers and dispatches in, or ``None`` if unscoped.

    Nearest wins, and an explicit ``None`` -- what :func:`scope_target` writes
    when handed no scope -- stops the search rather than falling through to an
    ancestor's subject.
    """
    for node in ctx.lineage():
        if SUBJECT_KEY in node.own_meta:
            found = node.own_meta[SUBJECT_KEY]
            return found if isinstance(found, Scope) else None
    return None


def scope_target(base: Context, scope: Scope | None, /) -> Context:
    """``base``, as seen from inside ``scope``.

    What a recorder or a supervisor uses to act on a subject's behalf from a
    context that is not the subject's: dispatches made through the result carry
    ``scope``, and listeners registered on it are admitted like the subject's
    own. ``None`` puts the result explicitly outside every scope.
    """
    if scope is None:
        return base.extend(**{SUBJECT_KEY: None})
    return base.extend(
        **{SUBJECT_KEY: scope, FILTER_KEY: _admission(scope, filter_of(base))}
    )


def admits(listener: Scope | None, carrier: Scope | None, /) -> bool:
    """Whether a registration at ``listener`` answers for ``carrier``.

    Asked in both directions, with the arguments meaning different things:
    for events, ``listener`` is where the listener registered and ``carrier``
    is the dispatching subject; for registry queries, ``listener`` is where the
    entry was registered and ``carrier`` is the querier. An absent ``listener``
    means "everywhere", which is why a plugin that never heard of scopes keeps
    working (SEM-004).
    """
    return listener is None or listener.covers(carrier)


# --------------------------------------------------------------------------
# Registries
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry(Generic[T]):
    item: T
    scope: Scope | None


class ScopedRegistry(Generic[T]):
    """A registry whose entries are visible down the scope chain.

    Concrete rather than a pattern to reimplement: an application has several
    of these -- tools, prompts, transports -- and SEM-002's direction is
    exactly the thing that must not be re-derived per registry.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list[_Entry[T]] = []

    def register(
        self, item: T, /, *, ctx: Context, label: str | None = None
    ) -> EffectHandle:
        """Add ``item``, visible to ``ctx``'s scope and its descendants.

        The entry is an effect on ``ctx``'s own effect scope, so it is unwound
        by whatever owns that context -- the subject's scope, when the context
        came from one -- and appears in the effect tree diagnostics render.
        """
        entry = _Entry(item=item, scope=subject_of(ctx))

        def start() -> Callable[[], None]:
            self._entries.append(entry)
            return lambda: self._forget(entry)

        return scope_of(ctx).effect(start, label or f"scoped:{type(item).__name__}")

    def visible(self, *, ctx: Context) -> tuple[T, ...]:
        """Everything registered at ``ctx``'s scope or at an ancestor of it."""
        here = subject_of(ctx)
        return tuple(entry.item for entry in self._entries if admits(entry.scope, here))

    def all(self) -> tuple[T, ...]:
        """Every live entry, whatever scope the caller is in.

        Deliberately ungated (SEM-005): diagnostics and session recorders need
        the whole picture, and a scope was never a boundary that could withhold
        it from anyone holding this object.
        """
        return tuple(entry.item for entry in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def _forget(self, entry: _Entry[T]) -> None:
        # By identity: two registrations of equal items are two entries, and
        # disposing one must not remove the other's.
        for index, other in enumerate(self._entries):
            if other is entry:
                del self._entries[index]
                return


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _owner(name: str) -> Callable[[Context], None]:
    """The plugin whose only job is to be disposable."""

    def scope(_ctx: Context) -> None:
        return None

    scope.__name__ = name
    scope.__qualname__ = name
    return scope


def _scoped(base: Context, scope: Scope, inherited: Filter | None) -> Context:
    return base.extend(**{SUBJECT_KEY: scope, FILTER_KEY: _admission(scope, inherited)})


def _admission(scope: Scope, inherited: Filter | None) -> Filter:
    """Admit dispatches from this scope's subtree -- and whatever else was asked.

    A nearer filter normally *replaces* the one above it (event-filtering
    SEM-002), but this one is installed on the author's behalf rather than by
    them: silently widening a filter a plugin installed, because a session was
    started inside it, would be a leak introduced by an unrelated call. The
    composition is an `and`, so creating a scope can only ever narrow.
    """

    def admit(carrier: Context) -> bool:
        if not admits(scope, subject_of(carrier)):
            return False
        return True if inherited is None else bool(inherited(carrier))

    admit.__qualname__ = f"scope:{scope.label}"
    return admit


def _describe(key: object) -> str:
    """A readable name for a subject that was not given one."""
    for attribute in ("__qualname__", "__name__"):
        found = getattr(key, attribute, None)
        if isinstance(found, str):
            return found
    return type(key).__name__
