"""The capability seam: Definition, Provider, Consumer -- and nothing across.

Implements ``spec/capabilities/19-capability-seam.yaml``.

Runtime substitution is worthless if consumers import providers. The registry
can rebind a name in a microsecond, but if the consumer's module imported the
provider's module to get a type, the two are welded together at import time and
no amount of rebinding separates them. The seam is the packaging discipline
that keeps the runtime's substitution machinery usable:

* a **Definition** is an abstract :class:`~cordis.registry.Service` subclass
  owning the registry name *and* the request and result types -- one artefact,
  so the two halves of the contract cannot drift apart (SEM-001);
* a **Provider** implements it, a **Consumer** depends on it, and neither
  imports the other (SEM-002). This is the seam; everything else here is in
  service of it.

The rest of the module is what a Definition package hands its Providers so
they do not each reinvent the registration discipline. :class:`Registry`
implements validate-then-commit once (SEM-004): a subclass supplies only pure
questions -- what is this thing called, and is it acceptable -- and cannot
reach the shared state before the answers are in. Removal belongs to the
registering scope, so there is no unregister to forget (SEM-005), and a
listener that raises is contained rather than allowed to derail a mutation
(SEM-006).

:func:`resolve_spec` is the other half of the discipline: defaults live in one
place and are applied by one call, rather than as ``or DEFAULT`` at each use
site, where two of them eventually disagree (SEM-003).
"""

from __future__ import annotations

import abc
import dataclasses
import sys
import traceback
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Generic, Self, TypeVar, cast

from cordis.config import ConfigIssue, from_dataclass, resolve_config
from cordis.errors import ConfigValidationError, RegistryConflictError
from cordis.plugin import scope_of
from cordis.registry import ChangeKind, Service

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cordis.context import Context
    from cordis.effect import EffectHandle

__all__ = [
    "UNSET",
    "Candidate",
    "Definition",
    "Registry",
    "RegistryChange",
    "RegistryFailure",
    "resolve_spec",
]

T = TypeVar("T")


class Definition(Service, abc.ABC, abstract=True):
    """Base class for a capability contract.

    A Definition declares the registry name once, as ``name``, and the request
    and result types alongside it, so a Consumer that imports the Definition
    has the whole contract in one import and a Provider that drifts from it
    fails at class definition rather than at first call.

    This class itself declares no name -- it stands for no binding -- which is
    what ``abstract=True`` says to :class:`~cordis.registry.Service`. Every
    concrete subclass still has to declare or inherit one.
    """

    __slots__ = ()

    @classmethod
    def of(cls, ctx: Context, /) -> Self:
        """The provider bound for this capability in ``ctx``, typed as this.

        The Consumer's spelling. ``ctx.require(SomeDefinition)`` is what a
        reader reaches for and what a type checker rejects: ``require`` takes
        ``type[T]``, which asserts the class can be instantiated, and SEM-001
        makes a Definition abstract. Resolving by ``cls.name`` -- the same
        binding, reached by the string half of the contract -- carries no such
        constraint, so the one cast the seam needs lives here rather than at
        every use site.

        Raises :class:`~cordis.errors.ServiceNotFoundError` when nothing is
        bound, which is the same failure ``ctx.require`` reports: a Consumer
        that declared the dependency never runs without it, so a ``None`` to
        check would be a branch that cannot be taken.
        """
        return cast("Self", ctx.require(cls.name))


# --------------------------------------------------------------------------
# The registry a Definition package owns
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate(Generic[T]):
    """An item that has passed validation, with the key it will be filed under.

    Produced by :meth:`Registry._validate` and consumed by ``_commit`` and
    ``_release``, so the key is computed once: a key derived again at removal
    time is a key that can differ from the one used at insertion.
    """

    key: str
    item: T


@dataclass(frozen=True, slots=True)
class RegistryChange(Generic[T]):
    """One transition of a registry's entries.

    There is no ``REPLACED`` here for the same reason there is none in the
    service registry: a replacement is a removal and an addition, and reporting
    it as one hides the window in which the key held nothing.
    """

    kind: ChangeKind
    key: str
    item: T


@dataclass(frozen=True, slots=True)
class RegistryFailure(Generic[T]):
    """A listener that raised, and what it was being told at the time."""

    registry: str
    change: RegistryChange[T]
    listener: str
    error: BaseException


class Registry(Generic[T], abc.ABC):
    """Entries that are registered as effects and removed by their own scope.

    Concrete rather than a pattern to reimplement. An application has several
    of these -- tools, prompts, transports -- and the ordering SEM-004
    prescribes is exactly the thing that must not be re-derived per registry:
    validate fully, mutate in one synchronous stretch, announce, and hand back
    the undo. A subclass supplies :meth:`key_of` and optionally :meth:`check`,
    neither of which is given the chance to touch shared state.
    """

    __slots__ = ("_entries", "_handlers", "_listeners")

    def __init__(self) -> None:
        self._entries: dict[str, T] = {}
        self._listeners: list[Callable[[RegistryChange[T]], None]] = []
        self._handlers: list[Callable[[RegistryFailure[T]], None]] = []

    # -- what a subclass supplies ------------------------------------------

    @abc.abstractmethod
    def key_of(self, item: T, /) -> str:
        """What ``item`` is filed under.

        Pure: called during validation, before anything has been committed, and
        free to raise if the item has no usable key.
        """

    def check(self, item: T, /) -> None:
        """Refuse ``item`` by raising; the default accepts everything.

        Called after the duplicate-key check and before any mutation, so
        whatever it raises leaves the registry exactly as it was.
        """

    # -- registration ------------------------------------------------------

    def register(
        self, item: T, /, *, ctx: Context, label: str | None = None
    ) -> EffectHandle:
        """Add ``item`` for as long as ``ctx``'s scope lives.

        Raises whatever validation raised -- synchronously, having written
        nothing -- and otherwise returns the effect handle. Nobody has to
        remember to unregister: the entry is an effect on the caller's own
        scope, so it is unwound by whatever unloads the caller (SEM-005), and
        it shows up in the effect tree with the rest of what that plugin did.
        """

        def start() -> Callable[[], None]:
            candidate = self._validate(item)
            self._commit(candidate)
            self._announce(ChangeKind.ADDED, candidate)

            def undo() -> None:
                if self._release(candidate):
                    self._announce(ChangeKind.REMOVED, candidate)

            return undo

        return scope_of(ctx).effect(start, label or f"register:{type(item).__name__}")

    def _validate(self, item: T, /) -> Candidate[T]:
        """Everything that may refuse ``item``, before anything that changes.

        Reads the entries; writes nothing. That is the whole of SEM-004's
        first half, and it is here rather than in a subclass hook because the
        duplicate check is precisely the check an author is tempted to do
        after inserting.
        """
        key = self.key_of(item)
        if key in self._entries:
            raise RegistryConflictError(type(self).__name__, key)
        self.check(item)
        return Candidate(key=key, item=item)

    def _commit(self, candidate: Candidate[T], /) -> None:
        """Publish a validated candidate. Synchronous and total, by contract."""
        self._entries[candidate.key] = candidate.item

    def _release(self, candidate: Candidate[T], /) -> bool:
        """Remove ``candidate`` if it is still the entry, and say whether it was.

        The identity check is what keeps a stale disposer from evicting a
        successor that took the key after this one was already gone.
        """
        if self._entries.get(candidate.key) is not candidate.item:
            return False
        del self._entries[candidate.key]
        return True

    # -- reading -----------------------------------------------------------

    def get(self, key: str, /) -> T | None:
        """The entry under ``key``, or ``None``."""
        return self._entries.get(key)

    def entries(self) -> Mapping[str, T]:
        """A read-only snapshot of the entries, in registration order.

        A new mapping each call: handing out the live dict would let a caller
        hold something that changes underneath them.
        """
        return MappingProxyType(dict(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({len(self._entries)} entries)"

    # -- notification ------------------------------------------------------

    def observe(
        self, listener: Callable[[RegistryChange[T]], None], /
    ) -> Callable[[], None]:
        """Register a listener for entry transitions; returns the undo."""
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    def on_error(
        self, handler: Callable[[RegistryFailure[T]], None], /
    ) -> Callable[[], None]:
        """Route contained listener failures somewhere; returns the undo."""
        self._handlers.append(handler)

        def stop() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return stop

    def _announce(self, kind: ChangeKind, candidate: Candidate[T], /) -> None:
        """Tell every listener, containing whatever they do about it.

        The entries are already correct before the first listener runs, and
        every listener runs even if an earlier one raised (SEM-006): a registry
        mutation that half-fails because an observer threw is the worst state
        shared state can be left in.
        """
        change = RegistryChange(kind=kind, key=candidate.key, item=candidate.item)
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception as exc:
                self._report(change, listener, exc)

    def _report(
        self,
        change: RegistryChange[T],
        listener: Callable[[RegistryChange[T]], None],
        error: BaseException,
        /,
    ) -> None:
        """Deliver a failure the mutation deliberately did not raise.

        With no handler installed the traceback goes to stderr. Silence is the
        one unacceptable option: the caller is deliberately not told, so the
        channel is the only place the failure can still be seen.
        """
        failure = RegistryFailure(
            registry=type(self).__name__,
            change=change,
            listener=_describe(listener),
            error=error,
        )
        if not self._handlers:
            traceback.print_exception(error, file=sys.stderr)
            return
        for handler in list(self._handlers):
            handler(failure)


# --------------------------------------------------------------------------
# Resolved specifications
# --------------------------------------------------------------------------


class _Unset:
    """The sentinel's type, so it prints as itself in a repr or a traceback."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: "This field has no default; something must fill it before the spec is used."
#:
#: Typed ``Any`` so it can stand as the declared default of a field of any
#: type: ``region: str = UNSET`` type-checks, and :func:`resolve_spec` is what
#: refuses to hand out a spec still holding one.
UNSET: Final[Any] = _Unset()


def resolve_spec(
    spec: type[T], raw: object = None, /, *, plugin: str | None = None
) -> T:
    """Read ``raw`` as ``spec``, with every field resolved to a value.

    The defaults are the dataclass's own -- one place per field, where a reader
    looking for "what is the default timeout" finds exactly one answer instead
    of one per use site (SEM-003). This function is what makes "resolved" mean
    something: it applies them, then refuses to return a spec in which any
    field is still :data:`UNSET`, at any depth.

    Total over valid input and idempotent: ``resolve_spec(S, resolve_spec(S,
    raw))`` is the first result again, because a spec instance is accepted as
    input and validated as itself.

    Raises :class:`~cordis.errors.ConfigValidationError` carrying every issue
    at once -- unknown keys, type failures and unfilled fields alike -- so a
    config with four mistakes takes one round trip to fix.
    """
    where = plugin if plugin is not None else spec.__name__
    resolved = resolve_config(
        from_dataclass(spec), {} if raw is None else raw, plugin=where
    )
    missing = [ConfigIssue(path, "no resolved default") for path in _unfilled(resolved)]
    if missing:
        raise ConfigValidationError(where, missing)
    return resolved  # type: ignore[return-value]


def _unfilled(value: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every field still holding the sentinel, by path, depth first.

    Generic over the dataclass rather than a per-spec list of fields: a field
    added to a spec and forgotten by whatever was meant to fill it is caught
    without anyone remembering to extend this.
    """
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return []
    found: list[tuple[str, ...]] = []
    for field_ in dataclasses.fields(value):
        held = getattr(value, field_.name)
        here = (*path, field_.name)
        if held is UNSET:
            found.append(here)
        found.extend(_unfilled(held, here))
    return found


def _describe(listener: object) -> str:
    """What to call a listener in a failure report."""
    for attribute in ("__qualname__", "__name__"):
        found = getattr(listener, attribute, None)
        if isinstance(found, str):
            return found
    return repr(listener)
