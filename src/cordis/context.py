"""The context tree: hierarchical scope, container handle, and identity.

Implements ``spec/capabilities/00-context-tree.yaml``.

Three facts decide the shape of everything here.

**Derivation, not mutation.** ``extend`` returns a new Context holding its own
small dict and a pointer to its parent. Nothing is copied, nothing upstream is
touched, and two siblings being built concurrently cannot see each other
(SEM-001, SEM-005).

**Resolution is a walk, and a miss is loud.** A key not defined here is looked
for in the nearest ancestor that defines *that key*, then in the services; if
nothing has it, resolution raises rather than returning ``None`` (SEM-002).

**Attribute access is a narrow door.** Only ``__getattr__`` routes to
resolution, and only for names that are neither underscore-prefixed nor
reserved. Everything the interpreter and the standard library probe for --
``__deepcopy__``, ``__await__``, ``_ipython_canary_method_should_not_exist_``
-- fails as an ordinary ``AttributeError`` without the registry ever hearing
about it (SEM-003). That rule is also what keeps ``__getattr__`` from
recursing during ``__init__``: every internal slot is underscore-prefixed, so
reading one before it is assigned raises instead of re-entering resolution.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Protocol,
    TypeAlias,
    TypeGuard,
    TypeVar,
    overload,
)

from cordis.errors import ServiceNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "CONTEXT_BRAND",
    "RESERVED_NAMES",
    "Context",
    "ServiceResolver",
    "Token",
    "is_passthrough",
]

T = TypeVar("T")

#: What may be resolved: a service class (the typed path) or a name (the
#: dynamic path). Spelled once, because it appears in every resolution
#: signature and the two halves are not interchangeable for the type checker --
#: only the overloads distinguish them.
Token: TypeAlias = "type[object] | str"

#: Absence, distinguished from a scoped value that happens to be ``None``.
_MISSING: Final = object()

#: The nominal brand, mirroring upstream's ``Symbol.for('cordis.is')``.
#:
#: Two copies of this package -- an editable install shadowed by a vendored
#: one, a module reloaded by a test runner -- produce Contexts that fail
#: ``isinstance`` against each other while being the same thing in every way
#: that matters. Identity is therefore nominal: a well-known attribute name,
#: not a class object (SEM-004).
CONTEXT_BRAND: Final = "__cordis_context__"

#: Names that never reach service resolution even though they carry no
#: underscore.
#:
#: ``keys`` is here because it is how Python and most of the ecosystem asks
#: "are you a mapping?" -- ``dict(**obj)`` probes it, and so does anything that
#: wants to duck-type a config object. A Context that answered that probe by
#: walking the registry would either raise from inside unrelated library code
#: or, worse, succeed and be silently treated as a mapping.
RESERVED_NAMES: Final = frozenset({"keys"})


def is_passthrough(name: str) -> bool:
    """Whether ``name`` resolves as a plain attribute, never as a service.

    Covers dunders and single-underscore names with one test: both start with
    an underscore, and neither is a service name anyone would choose.
    """
    return name.startswith("_") or name in RESERVED_NAMES


class ServiceResolver(Protocol):
    """The seam between a Context and the service registry.

    Declared here rather than imported, because the registry depends on the
    context tree and not the other way round. ``cordis.registry.ServiceRegistry``
    satisfies this shape structurally; tests substitute a counting stub.
    """

    def lookup(self, token: Token, /, *, ctx: Context) -> Any | None:  # noqa: ANN401
        """Return the bound implementation, or ``None`` if nothing is bound."""
        ...


class Context:
    """A handle onto a scope: some metadata, a parent, and a view of services.

    Cheap to make and safe to keep. A Context is allocated per mounted plugin
    and per scoped dispatch, so it holds four slots and derives in constant
    time; it is also stored in registries as a dict key, so equality is
    identity and the hash never changes (SEM-006).
    """

    __slots__ = ("_derived", "_label", "_meta", "_parent", "_resolver", "_root")

    #: See :data:`CONTEXT_BRAND`. Present on the class, so subclasses and
    #: foreign copies of this module answer the same way.
    __cordis_context__: Final = True

    def __init__(
        self,
        *,
        resolver: ServiceResolver | None = None,
        label: str = "ctx",
    ) -> None:
        self._parent: Context | None = None
        self._meta: dict[str, object] = {}
        self._resolver = resolver
        self._label = label
        self._root = self
        self._derived = 0

    # -- derivation --------------------------------------------------------

    def extend(self, **meta: object) -> Context:
        """Derive a child carrying ``meta`` on top of this context's view.

        O(1): the child stores only what it was given. Ancestor metadata is
        reached by walking, never by copying, which is what keeps a deep tree
        linear instead of quadratic (SEM-005).
        """
        child = Context.__new__(Context)
        child._parent = self
        child._meta = dict(meta)  # the caller's kwargs dict is already fresh;
        child._resolver = self._resolver  # copying makes that explicit anyway
        child._label = f"{self._label}.{self._derived}"
        child._root = self._root
        child._derived = 0
        self._derived += 1
        return child

    # -- identity ----------------------------------------------------------

    @property
    def root(self) -> Context:
        """The context this one was ultimately derived from."""
        return self._root

    @property
    def label(self) -> str:
        """A stable path-shaped name, used when reporting what was searched.

        SEM-002 requires a failed resolution to list the contexts it walked,
        which requires contexts to be nameable. The label is derived from the
        parent's label and a per-parent counter, so it costs one integer and
        reads like a position in the tree: ``ctx.0.2``.
        """
        return self._label

    @property
    def own_meta(self) -> Mapping[str, object]:
        """Exactly what was passed to the ``extend`` that made this context.

        A read-only view for diagnostics. Deliberately *not* the resolved
        view: anything that wants inheritance should resolve, so the walk is
        exercised rather than bypassed.
        """
        return MappingProxyType(self._meta)

    @staticmethod
    def is_context(obj: object) -> TypeGuard[Context]:
        """Whether ``obj`` is a Context, including one from another import."""
        return getattr(obj, CONTEXT_BRAND, False) is True

    def lineage(self) -> Iterator[Context]:
        """This context, then its parent, up to the root."""
        node: Context | None = self
        while node is not None:
            yield node
            node = node._parent

    # -- resolution --------------------------------------------------------

    @overload
    def get(self, token: type[T], /) -> T | None: ...

    @overload
    def get(self, token: str, /) -> Any: ...  # noqa: ANN401 -- dynamic path

    def get(self, token: Token, /) -> Any:
        """Resolve ``token``, or ``None`` if nothing provides it.

        The tolerant form, for code that has a real answer for absence. Code
        that does not should use :meth:`require` and let the failure name
        itself.
        """
        found = self._resolve(token)
        return None if found is _MISSING else found

    @overload
    def require(self, token: type[T], /) -> T: ...

    @overload
    def require(self, token: str, /) -> Any: ...  # noqa: ANN401 -- dynamic path

    def require(self, token: Token, /) -> Any:
        """Resolve ``token`` or raise :class:`ServiceNotFoundError`.

        Scoped metadata is consulted before services, so a subtree can shadow
        a provided capability with a fixture or a tenant-specific value. The
        error names the token and lists every context walked, because "which
        scope did you expect this in" is the only question worth answering
        when wiring is wrong (SEM-002).
        """
        found = self._resolve(token)
        if found is not _MISSING:
            return found
        raise ServiceNotFoundError(
            _token_name(token), tuple(node._label for node in self.lineage())
        )

    def _resolve(self, token: Token, /) -> Any:  # noqa: ANN401
        """The single resolution path, returning ``_MISSING`` for absence.

        Scoped keys are matched by presence rather than by truthiness, so a
        scoped ``0``, ``""`` or ``None`` shadows an inherited value instead of
        falling through to it -- the difference between "this subtree sets it
        to zero" and "this subtree says nothing".
        """
        if isinstance(token, str):
            for node in self.lineage():
                meta = node._meta
                if token in meta:
                    return meta[token]
        if self._resolver is None:
            return _MISSING
        found = self._resolver.lookup(token, ctx=self)
        return _MISSING if found is None else found

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Route a missing attribute to resolution -- unless it is spoken for.

        Reached only after normal lookup fails, so real attributes, methods
        and descriptors keep their ordinary cost and semantics.
        """
        if is_passthrough(name):
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        return self.require(name)

    # -- presentation ------------------------------------------------------

    def __repr__(self) -> str:
        keys = ", ".join(sorted(self._meta))
        return f"<Context {self._label} [{keys}]>"


def _token_name(token: Token) -> str:
    """The name to report for a failed resolution.

    A class token reports the registry name its author declared, falling back
    to the qualified name -- so the error says what the caller wrote, not an
    internal key.
    """
    if isinstance(token, str):
        return token
    declared = getattr(token, "name", None)
    if isinstance(declared, str):
        return declared
    return token.__qualname__
