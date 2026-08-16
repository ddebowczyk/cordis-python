"""Service isolation: a subtree with its own implementation of one name.

Implements ``spec/capabilities/08-service-isolation.yaml``.

``isolate(ctx, ("shell",))`` returns a context in which ``shell`` resolves in a
realm of its own. Everything else resolves exactly as it did, and a provider
mounted inside binds where the subtree can see it and nowhere else. Two
subtrees can be given one shared private instance by isolating under the same
label.

Two decisions carry the design.

**An isolated realm has no parent.** Realms nest for :func:`~cordis.registry.
enter_realm`, whose job is an overlay that falls through to what it does not
override. Isolation's job is the opposite -- a subtree that does *not* see the
outer implementation -- so its realm is a root. A parented isolation realm
would resolve the outer binding for every name it had not been given yet,
which is the failure isolation exists to prevent, arriving one turn late.

**The mapping is per name, in the ordinary scoped-metadata chain.** One key per
isolated name, read by :func:`~cordis.registry.realm_for`. There is no mapping
object to keep consistent, no second inheritance rule, and an isolation is
inherited by descendants for the same reason everything else is.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeAlias
from weakref import WeakValueDictionary

from cordis.registry import REALM_KEY, Realm, Service, realm_key, service_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cordis.context import Context

__all__ = [
    "Isolation",
    "isolate",
    "isolated_names",
    "isolated_realm",
]

#: What can be isolated: names, service classes, or a mapping of either to the
#: label that names the realm. The sequence form is the unlabelled case spelled
#: without ``{name: None}`` noise.
Isolation: TypeAlias = (
    "Iterable[str | type[Service]] | Mapping[str | type[Service], str | None]"
)

#: Labelled realms, interned so that two isolations of one name under one label
#: are one realm. Weak-valued: the last context to hold a realm is the last
#: reference to it, so a subtree that goes away takes its realm with it
#: (SEM-006) without anything having to remember to clean up.
_INTERNED: WeakValueDictionary[tuple[str, str], Realm] = WeakValueDictionary()


def isolated_realm(name: str, label: str | None = None) -> Realm:
    """The realm ``name`` should resolve in under ``label``.

    An unlabelled isolation mints a realm, every time: that is what makes it
    private, and it is why a reload gets a fresh one. A labelled isolation
    returns the interned realm for ``(name, label)``, minting it once.

    Interning is keyed by the pair rather than by the label alone. Two subtrees
    isolating ``shell`` under ``test`` want one shared shell; a third isolating
    ``logger`` under ``test`` wants nothing to do with it. The pair makes a
    label mean "the same thing", not "the same place".
    """
    if label is None:
        return Realm(f"isolate:{name}")
    key = (name, label)
    # Read and write with no suspension point between them: two isolations of
    # one label cannot interleave here and mint two realms.
    found = _INTERNED.get(key)
    if found is None:
        found = Realm(f"isolate:{name}@{label}")
        _INTERNED[key] = found
    return found


def isolate(ctx: Context, names: Isolation, /) -> Context:
    """A child context resolving each of ``names`` in a realm of its own.

    Isolating nothing returns ``ctx`` itself. An empty extension would be a
    context that differs from its parent in no way anyone can observe, and
    handing one back would make ``isolate`` a thing you cannot call
    defensively.
    """
    declared = _declared(names)
    if not declared:
        return ctx
    frame: dict[str, Any] = {
        realm_key(name): isolated_realm(name, label) for name, label in declared.items()
    }
    return ctx.extend(**frame)


def isolated_names(ctx: Context) -> frozenset[str]:
    """Every name isolated anywhere in ``ctx``'s lineage.

    For diagnostics and for tests: the runtime itself never needs the set, only
    the realm for one name at a time.
    """
    prefix = f"{REALM_KEY}:"
    return frozenset(
        key.removeprefix(prefix)
        for node in ctx.lineage()
        for key in node.own_meta
        if key.startswith(prefix)
    )


def _declared(names: Isolation) -> dict[str, str | None]:
    """Normalise every accepted spelling to ``{name: label}``.

    A ``Service`` subclass reduces to its name exactly as ``inject`` reduces
    it, so the two can never disagree about what a service is called.
    """
    pairs: Iterable[tuple[object, str | None]]
    if isinstance(names, Mapping):
        pairs = names.items()
    else:
        pairs = ((token, None) for token in names)
    declared: dict[str, str | None] = {}
    for token, label in pairs:
        declared[_name_of(token)] = label
    return declared


def _name_of(token: object) -> str:
    """The service name ``token`` stands for, by the registry's one rule.

    Typed as ``object`` on purpose: the annotation says names and service
    classes, and the check is here for the caller who was not type-checked.
    """
    return service_name(token)
