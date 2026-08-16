"""The service registry: named capabilities with owned lifetime.

Implements ``spec/capabilities/02-service-registry.yaml``.

A binding is not a dict entry that someone remembers to remove. It is an
effect on the provider's scope, so "who unregisters this" has exactly one
answer -- whoever disposes the scope -- and it cannot drift out of sync with
the provider's own resources (SEM-001).

Three rules shape the rest:

**Absence is loud, conflict is louder.** A name with no binding raises rather
than resolving to ``None`` (SEM-002), and a second provider for a live
``(name, realm)`` is rejected *before* anything is written, so the incumbent
survives the attempt intact (SEM-003).

**Nothing half-built is visible.** A provider with a readiness gate reserves
its key immediately -- so a competing provider still collides -- but publishes
only once the gate completes. A gate that raises releases the reservation and
publishes nothing (SEM-005).

**Mutation is synchronous.** Every change to the binding set happens in a
single ``await``-free stretch: check, mutate, notify. That is what makes
teardown atomic with respect to resolution without a lock (SEM-006), and what
lets the notification precede any dependent re-evaluation (SEM-004).
"""

from __future__ import annotations

import enum
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypeAlias, TypeVar, overload

from cordis.errors import (
    InvalidPluginError,
    ServiceConflictError,
    ServiceNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

    from cordis.context import Context, Token
    from cordis.effect import EffectHandle, EffectScope

__all__ = [
    "DEFAULT_REALM",
    "REALM_KEY",
    "BindingChange",
    "BindingInfo",
    "ChangeKind",
    "Gate",
    "Realm",
    "Service",
    "ServiceRegistry",
    "enter_realm",
    "realm_for",
    "realm_key",
    "realm_of",
    "service_name",
    "token_label",
]

T = TypeVar("T")

#: A readiness gate: called with no arguments, returning either an awaitable
#: (initialise, then publish) or an async iterator that yields exactly once
#: (initialise, publish at the yield, tear down when the binding is disposed).
#: The second form exists so a service whose startup and shutdown are two
#: halves of one story can be written as one function.
Gate: TypeAlias = "Callable[[], Awaitable[None] | AsyncGenerator[None, None]]"

#: The scoped-metadata key carrying a context's realm.
#:
#: A dunder name, so it is unreachable through attribute access and can never
#: be confused with a service (context-tree SEM-003). Realm membership is
#: scope, and scope is what the context tree already models -- giving Context
#: its own realm field would be a second, divergent inheritance rule.
REALM_KEY: Final = "__realm__"


class Realm:
    """An isolation boundary for bindings.

    Realms nest: a lookup that finds nothing in its own realm continues into
    the enclosing one, so an isolated subtree overrides what it wants and
    inherits the rest. Identity is the object, never the label -- two realms
    both called ``"test"`` are two realms.
    """

    # ``__weakref__`` is in the slots because realm lifetime is the observable
    # service-isolation SEM-006 is about: labelled realms are interned weakly,
    # and a test that cannot take a weak reference to one cannot tell a
    # released realm from a leaked one.
    __slots__ = ("__weakref__", "_parent", "label")

    def __init__(self, label: str = "default", *, parent: Realm | None = None) -> None:
        self.label = label
        self._parent = parent

    @property
    def parent(self) -> Realm | None:
        return self._parent

    def child(self, label: str) -> Realm:
        """A realm enclosed by this one."""
        return Realm(label, parent=self)

    def lineage(self) -> Iterator[Realm]:
        """This realm, then the realms enclosing it, outward."""
        realm: Realm | None = self
        while realm is not None:
            yield realm
            realm = realm.parent

    def __repr__(self) -> str:
        return f"<Realm {self.label}>"


#: The realm a context belongs to unless something says otherwise.
DEFAULT_REALM: Final = Realm("default")


def realm_of(ctx: Context) -> Realm:
    """The realm this context belongs to.

    Walks the lineage directly rather than through ``ctx.get``: resolution
    consults the registry, the registry consults the realm, and the realm must
    not consult resolution.
    """
    for node in ctx.lineage():
        realm = node.own_meta.get(REALM_KEY)
        if isinstance(realm, Realm):
            return realm
    return DEFAULT_REALM


def enter_realm(ctx: Context, realm: Realm) -> Context:
    """A child context whose subtree resolves inside ``realm``."""
    return ctx.extend(**{REALM_KEY: realm})


def realm_key(name: str) -> str:
    """The scoped-metadata key carrying a context's realm for one name.

    Per-name isolation and whole-context membership share one prefix and one
    lineage walk. The reader lives here, next to :data:`REALM_KEY`, because a
    key whose format is decided in one module and parsed in another is a format
    that drifts; :mod:`cordis.realm` writes it through this function.
    """
    return f"{REALM_KEY}:{name}"


def realm_for(ctx: Context, name: str) -> Realm:
    """The realm ``ctx`` resolves ``name`` in.

    Three levels of specificity in one walk, nearest first: an isolation of
    this particular name, then the realm the context belongs to for every
    name, then the default. A name nobody isolated resolves exactly where it
    did before isolation existed, which is service-isolation SEM-003.
    """
    key = realm_key(name)
    for node in ctx.lineage():
        meta = node.own_meta
        found = meta.get(key, meta.get(REALM_KEY))
        if isinstance(found, Realm):
            return found
    return DEFAULT_REALM


class ChangeKind(enum.Enum):
    """What happened to the binding set.

    There is no ``REPLACED``: a replacement is a removal and an addition, and
    reporting it as one event would hide the window in which the name was
    unbound from exactly the consumers who need to know about it.
    """

    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class BindingChange:
    """One transition of the binding set."""

    kind: ChangeKind
    name: str
    realm: Realm


@dataclass(frozen=True)
class BindingInfo:
    """A read-only description of one binding."""

    name: str
    realm: Realm
    value: object
    provider: str
    published: bool


Listener: TypeAlias = "Callable[[BindingChange], None]"


@dataclass
class _Binding:
    """The live entry. Mutable in exactly one field: ``published``."""

    name: str
    realm: Realm
    value: object
    provider: str
    published: bool

    def info(self) -> BindingInfo:
        return BindingInfo(
            name=self.name,
            realm=self.realm,
            value=self.value,
            provider=self.provider,
            published=self.published,
        )


class Service:
    """Base class for services that want to be resolvable by their type.

    The class object is the type-checkable identity and ``name`` is the
    configuration-facing one; both must reach the same binding (SEM-007).
    Declaring the name on the class is what keeps those two halves in sync,
    and ``__init_subclass__`` checks it at class-creation time rather than at
    the first failed lookup.

    ``class D(Service, abstract=True)`` skips that check. The rule is about
    classes that stand for a binding, and an abstract base -- a capability
    seam's Definition (capability-seam SEM-001) -- stands for none. Being a
    class keyword rather than an attribute, it is not inherited: a concrete
    subclass of an abstract Definition is checked exactly as before.
    """

    name: ClassVar[str]

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if abstract:
            return
        declared = cls.__dict__.get("name", getattr(cls, "name", None))
        if not isinstance(declared, str) or not declared:
            raise InvalidPluginError(
                cls, "a Service subclass must declare a non-empty `name`"
            )

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx


def service_name(token: object, *, subject: object = None) -> str:
    """The registry name ``token`` stands for.

    One rule, in one place. A name is itself and a ``Service`` subclass is its
    declared ``name``; anything else is a mistake worth naming at the call site
    that made it. Injection, isolation and interception all ask this question,
    and three private copies of the answer is three chances for them to
    disagree about what a service is called.

    ``subject`` is what the error should blame -- the plugin whose declaration
    carried the bad token, when the caller knows it.
    """
    if isinstance(token, str) and token:
        return token
    if isinstance(token, type) and issubclass(token, Service):
        return token.name
    raise InvalidPluginError(
        subject if subject is not None else token,
        f"{token_label(token)!r} is not a service name or a Service subclass",
    )


def token_label(token: object) -> str:
    """How to name ``token`` in an error, without calling ``repr`` on a class."""
    if isinstance(token, str):
        return token
    return getattr(token, "__qualname__", None) or repr(token)


class ServiceRegistry:
    """The binding set, plus the notifications that describe its changes.

    Satisfies :class:`cordis.context.ServiceResolver`, so a root context
    constructed with ``Context(resolver=registry)`` resolves service names
    through it.
    """

    __slots__ = ("_bindings", "_listeners")

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, Realm], _Binding] = {}
        self._listeners: list[Listener] = []

    # -- providing ---------------------------------------------------------

    def provide(
        self,
        name: str,
        value: object,
        *,
        scope: EffectScope,
        ctx: Context | None = None,
        realm: Realm | None = None,
        gate: Gate | None = None,
    ) -> EffectHandle:
        """Bind ``name`` to ``value`` for the lifetime of ``scope``.

        ``ctx`` is how a provider says "bind this where I would resolve it":
        the realm is derived with the same :func:`realm_for` a lookup uses, so
        a provider inside an isolated subtree binds inside it
        (service-isolation SEM-004). ``realm`` names one directly, for callers
        that hold a realm rather than a context; giving neither means the
        default realm, which is what every provider meant before isolation
        existed.

        Returns the effect handle: call it to unbind early, await it to wait
        for a gated provider to become ready. That is the same pairing
        ``EffectScope.effect`` established -- the undo and the settle are two
        views of one registration, not two objects to keep straight.

        Raises :class:`ServiceConflictError` synchronously if the name is
        already claimed in this realm, having written nothing.
        """
        if realm is None:
            realm = DEFAULT_REALM if ctx is None else realm_for(ctx, name)
        key = (name, realm)
        provider = scope.label or f"<unlabelled scope {id(scope):#x}>"

        def register() -> object:
            binding = self._reserve(key, value, provider, published=False)
            if gate is None:
                self._publish(binding)
                return lambda: self._unbind(binding)
            return self._open_gate(gate, binding)

        return scope.effect(register, label=f"provide:{name}")

    def _reserve(
        self, key: tuple[str, Realm], value: object, provider: str, *, published: bool
    ) -> _Binding:
        """Claim the key, or raise leaving the incumbent untouched.

        The check and the write are one statement apart with nothing between
        them, and the check comes first: a rejected provider must not have
        clobbered anything by the time it learns it was rejected
        (PROP-REG-003).
        """
        name, realm = key
        held = self._bindings.get(key)
        if held is not None:
            raise ServiceConflictError(name, held.provider, provider)
        binding = _Binding(
            name=name, realm=realm, value=value, provider=provider, published=published
        )
        self._bindings[key] = binding
        return binding

    async def _open_gate(self, gate: Gate, binding: _Binding) -> object:
        """Run the readiness gate, publishing only once it completes.

        The key stays reserved throughout, so a competing provider still
        collides while this one is starting up; a gate that raises drops the
        reservation on its way out, so a failed start leaves no trace.
        """
        opening: AsyncGenerator[None, None] | None = None
        try:
            started = gate()
            if isinstance(started, AsyncGenerator):
                opening = started
                await anext(opening)
            else:
                await started
        except StopAsyncIteration:
            self._drop(binding)
            raise InvalidPluginError(
                type(binding.value), "readiness gate returned before it yielded"
            ) from None
        except BaseException:
            self._drop(binding)
            raise

        self._publish(binding)

        if opening is None:
            return lambda: self._unbind(binding)

        closing = opening

        async def dispose() -> None:
            self._unbind(binding)
            await closing.aclose()

        return dispose

    def _publish(self, binding: _Binding, /) -> None:
        """Make a reserved binding visible, and say so.

        A listener that raises fails the provide: the binding is withdrawn and
        the error is raised, so the registry never ends up in a state some of
        its observers were never told about. Withdrawal is silent -- the
        addition never landed, so there is no removal to report.
        """
        binding.published = True
        try:
            self._announce(ChangeKind.ADDED, binding)
        except BaseException:
            binding.published = False
            self._drop(binding)
            raise

    def _drop(self, binding: _Binding) -> None:
        """Release a reservation that never became a binding.

        No notification: nothing was ever announced as added, and announcing a
        removal for it would tell dependents to re-evaluate a change that
        never happened (SEM-004, PROP-REG-006).
        """
        key = (binding.name, binding.realm)
        if self._bindings.get(key) is binding:
            del self._bindings[key]

    def _unbind(self, binding: _Binding) -> None:
        """Remove a binding, if it is still the one that was bound.

        The identity check is depth-in-defence, not a live guard: disposal
        arrives through an EffectHandle, which absorbs a second call before the
        registry hears about it (effect-scope PROP-EFFECT-003). It stays
        because it is what keeps "a stale disposer evicts its successor" out of
        reach if a second disposal path is ever added -- see the note on
        PROP-REG-001, which was restated once mutation testing showed the case
        unreachable from here.
        """
        key = (binding.name, binding.realm)
        if self._bindings.get(key) is not binding:
            return
        del self._bindings[key]
        if binding.published:
            self._announce(ChangeKind.REMOVED, binding)

    # -- resolving ---------------------------------------------------------

    @overload
    def lookup(self, token: type[T], /, *, ctx: Context) -> T | None: ...

    @overload
    def lookup(self, token: str, /, *, ctx: Context) -> Any: ...  # noqa: ANN401

    # The union arrives from Context's dynamic path, which cannot know which
    # of the two forms it holds.
    @overload
    def lookup(self, token: Token, /, *, ctx: Context) -> Any: ...  # noqa: ANN401

    def lookup(self, token: type[Any] | str, /, *, ctx: Context) -> Any:
        """Resolve ``token`` for ``ctx``, or ``None`` if nothing provides it."""
        name = _name_of(token)
        if name is None:
            return None
        for realm in realm_for(ctx, name).lineage():
            binding = self._bindings.get((name, realm))
            if binding is not None and binding.published:
                return binding.value
        return None

    @overload
    def resolve(self, token: type[T], /, *, ctx: Context) -> T: ...

    @overload
    def resolve(self, token: str, /, *, ctx: Context) -> Any: ...  # noqa: ANN401

    def resolve(self, token: type[Any] | str, /, *, ctx: Context) -> Any:
        """Resolve ``token`` for ``ctx`` or raise :class:`ServiceNotFoundError`."""
        found = self.lookup(token, ctx=ctx)
        if found is not None:
            return found
        name = _name_of(token)
        searched = [
            realm.label
            for realm in (
                realm_of(ctx) if name is None else realm_for(ctx, name)
            ).lineage()
        ]
        raise ServiceNotFoundError(_token_label(token), searched)

    def bindings(self) -> Mapping[tuple[str, Realm], BindingInfo]:
        """A snapshot of the published and reserved bindings.

        A new mapping each call, wrapped read-only: handing out the live dict
        would let a caller hold something that changes underneath them, which
        is precisely the bug the registry exists to prevent.
        """
        return MappingProxyType(
            {key: binding.info() for key, binding in self._bindings.items()}
        )

    # -- notifications -----------------------------------------------------

    def observe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener for binding-set transitions; returns the undo."""
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    def _announce(self, kind: ChangeKind, binding: _Binding) -> None:
        """Tell every listener, then report whatever they did wrong.

        The dict is already correct before the first listener runs, and every
        listener runs even if an earlier one raised: a broken observer must not
        be able to hide a change from the others, or leave the registry
        describing a state it is not in.
        """
        change = BindingChange(kind=kind, name=binding.name, realm=binding.realm)
        errors: list[Exception] = []
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup(f"errors while announcing {change}", errors)


def _name_of(token: type[Any] | str) -> str | None:
    """The registry key for a token, or ``None`` if the token names nothing."""
    if isinstance(token, str):
        return token
    declared = getattr(token, "name", None)
    return declared if isinstance(declared, str) and declared else None


def _token_label(token: type[Any] | str) -> str:
    """What to call the token in an error message."""
    return token if isinstance(token, str) else _name_of(token) or token.__qualname__
