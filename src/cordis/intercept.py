"""Service interception: per-subtree configuration of one shared instance.

Implements ``spec/capabilities/09-service-interception.yaml``.

Isolation answers "this subtree needs its own instance". Interception answers
the commoner question -- "this subtree needs the *same* instance to behave
slightly differently" -- which duplicating the instance answers badly, because
the instance usually owns a shared resource.

``intercept(ctx, "shell", {"timeout": 500})`` returns a child context carrying
one more entry in the chain for ``shell``. The service reads that chain, folds
it, and behaves accordingly; what it never does is change *which* object the
caller resolved (SEM-004). That is the line between this capability and
isolation, and it is the one thing here worth remembering.

Three decisions carry the design.

**The chain is the lineage.** One key per intercepted name
(``__intercept__:shell``) holding *this node's* entry, exactly as
:mod:`cordis.realm` holds a realm. Reading the chain is one lineage walk,
reversed so it comes out outermost-first (SEM-002); writing is one ``extend``
that copies nothing.

**Entries are frozen on the way in.** The caller keeps a reference to the dict
it passed. A chain that changed under a service because someone mutated that
dict an hour later is the least debuggable bug this capability could ship, so
each entry becomes a ``MappingProxyType`` over a copy.

**Folding is the service's business, opt in.** A service that wants a different
merge defines ``resolve_interceptions``; one that does not gets
:func:`merge_interceptions` over its ``defaults``. Nothing is added to
:class:`~cordis.registry.Service`, so a service that never heard of
interception keeps working and a service that is not a ``Service`` subclass can
still take part -- the same duck-typed shape ``ConfigSchema`` uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeAlias, runtime_checkable

from cordis.registry import Service, service_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cordis.context import Context

__all__ = [
    "INTERCEPT_KEY",
    "InterceptResolver",
    "Interception",
    "effective_config",
    "intercept",
    "intercept_all",
    "intercept_key",
    "intercepted_names",
    "interceptions",
    "merge_interceptions",
]

#: The scoped-metadata prefix carrying a context's own interception entries.
#: One key per name, so a context that intercepts ``shell`` says nothing about
#: ``http`` and a reader never has to take a chain apart.
INTERCEPT_KEY: Final = "__intercept__"

#: The attribute a service may declare to say what it does in the absence of
#: any interception (SEM-005).
DEFAULTS_ATTR: Final = "defaults"

#: What a mount may intercept: names or service classes, each mapped to the
#: entry the subtree contributes.
Interception: TypeAlias = "Mapping[str | type[Service], Mapping[str, object]]"


@runtime_checkable
class InterceptResolver(Protocol):
    """What a service defines when it wants to fold the chain itself.

    SEM-003 in one method: the framework supplies the ordered chain, the
    service decides what merging means. A service that concatenates lists,
    validates as it goes, or refuses a contradictory pair of entries writes
    that here rather than arguing with a framework-imposed merge.
    """

    def resolve_interceptions(
        self, chain: Sequence[Mapping[str, object]], /
    ) -> Mapping[str, object]: ...


def intercept_key(name: str) -> str:
    """The scoped-metadata key carrying a context's own entry for ``name``."""
    return f"{INTERCEPT_KEY}:{name}"


def intercept(
    ctx: Context, name: str | type[Service], config: Mapping[str, object], /
) -> Context:
    """A child context contributing one entry to ``name``'s chain.

    The parent is untouched (SEM-001), which is the context tree's own rule and
    not a separate promise: this is an ``extend`` like any other.
    """
    return intercept_all(ctx, {name: config})


def intercept_all(ctx: Context, entries: Interception, /) -> Context:
    """A child context contributing an entry to each of several chains.

    One ``extend`` for the whole mapping, because a mount declares all of its
    interceptions at once and two extends would be two nodes in the lineage for
    what the caller wrote as one act.

    Intercepting nothing returns ``ctx`` itself: an empty extension would be a
    context that differs from its parent in no observable way, and handing one
    back would make this unusable defensively.
    """
    if not entries:
        return ctx
    frame: dict[str, Any] = {
        intercept_key(service_name(token)): _freeze(config)
        for token, config in entries.items()
    }
    return ctx.extend(**frame)


def interceptions(
    ctx: Context, name: str | type[Service], /
) -> tuple[Mapping[str, object], ...]:
    """Every entry ``ctx`` sees for ``name``, outermost first.

    Outermost first is what makes a nested subtree's entry the one that wins
    under any last-wins fold (SEM-002). The lineage walks inward-out, so the
    result is reversed once, here, rather than at each of the places that fold
    it.
    """
    key = intercept_key(service_name(name))
    found: list[Mapping[str, object]] = []
    for node in ctx.lineage():
        entry = node.own_meta.get(key)
        if isinstance(entry, Mapping):
            found.append(entry)
    found.reverse()
    return tuple(found)


def intercepted_names(ctx: Context) -> frozenset[str]:
    """Every name intercepted anywhere in ``ctx``'s lineage.

    For diagnostics: the resolution path only ever wants one name at a time.
    """
    prefix = f"{INTERCEPT_KEY}:"
    return frozenset(
        key.removeprefix(prefix)
        for node in ctx.lineage()
        for key in node.own_meta
        if key.startswith(prefix)
    )


def merge_interceptions(
    chain: Sequence[Mapping[str, object]], /
) -> Mapping[str, object]:
    """The default fold: shallow, last wins, and ``None`` is a value.

    A left fold rather than ``ChainMap``, for two reasons. It is what the
    result actually is -- one dict, built once -- and PROP-INTC-004 checks this
    function against ``ChainMap``, which is only evidence while the two are
    different pieces of code.

    ``None`` being a value rather than an absence is the whole point of the
    card: a subtree clears an inherited setting by naming it explicitly, and a
    merge that skipped nulls would silently re-inherit it.
    """
    merged: dict[str, object] = {}
    for entry in chain:
        merged.update(entry)
    return merged


def effective_config(
    ctx: Context, service: object, /, *, name: str | type[Service] | None = None
) -> Mapping[str, object]:
    """What ``service`` should do for a caller resolving from ``ctx``.

    The service folds the chain if it says how (``resolve_interceptions``);
    otherwise the chain is folded over its ``defaults``, so a context that
    intercepts nothing observes exactly the declared default (SEM-005).

    ``name`` names the chain to read when the service does not carry a usable
    one -- a bare callable, a proxy, an object standing in for a service during
    a test.

    The result is a fresh mapping every call. Handing back the service's own
    defaults would let one caller's edit rewrite every other caller's
    configuration, which is the failure this capability exists to prevent
    happening at a distance instead of by design.
    """
    chain = interceptions(ctx, _name_for(service, name))
    if isinstance(service, InterceptResolver):
        # The protocol is `runtime_checkable`, so this is the duck-typed check
        # written once and type-checked at the same time -- `getattr` would
        # have answered the same question and returned `Any`.
        return service.resolve_interceptions(chain)
    return merge_interceptions((_defaults_of(service), *chain))


def _name_for(service: object, name: str | type[Service] | None) -> str:
    if name is not None:
        return service_name(name, subject=service)
    declared = getattr(service, "name", None)
    if isinstance(declared, str) and declared:
        return declared
    return service_name(service, subject=service)


def _defaults_of(service: object) -> Mapping[str, object]:
    """A service's declared default configuration, or nothing.

    Nothing is a legitimate answer: a service with no defaults declares its
    default by not having one, and inventing a value for it here would be the
    framework deciding something only the service knows.
    """
    declared = getattr(service, DEFAULTS_ATTR, None)
    return declared if isinstance(declared, Mapping) else {}


def _freeze(config: Mapping[str, object]) -> Mapping[str, object]:
    """A read-only snapshot, so the caller's later edits are their own."""
    return MappingProxyType(dict(config))
