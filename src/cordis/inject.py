"""What a plugin needs, and what it promises -- read off the plugin itself.

Implements the declaration half of ``spec/capabilities/05-dependency-injection.yaml``.
The continuous half -- staying PENDING until every declared name is bound,
reloading when an implementation is replaced -- lives in :mod:`cordis.fiber`,
because readiness and identity change are one decision made in one place.

A declaration can be spelled three ways, because the three plugin forms are
written three ways:

    @inject("db", Cache)              # a function
    def apply(ctx): ...

    class Reports:                    # a class, or a module object
        inject = ("db", Cache)

    inject = ("db", Cache)            # a module, at top level

All three reduce to the same tuple of registry names. Tokens may be strings or
:class:`~cordis.registry.Service` subclasses; the class form is what lets a
plugin declare a dependency it can also type-check.

The attribute is read, never required. A plugin that declares nothing depends
on nothing and loads immediately, which is what the overwhelming majority of
plugins do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias, TypeVar

from cordis.errors import InvalidPluginError
from cordis.registry import Service, service_name, token_label

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "DECLARED_ATTR",
    "INJECT_ATTR",
    "PROVIDES_ATTR",
    "Token",
    "dependencies_of",
    "inject",
    "provisions_of",
]

#: The attribute :func:`inject` writes. Distinct from the public spelling so a
#: module that imports the decorator (``from cordis import inject``) cannot be
#: mistaken for a module that declares dependencies -- the name is shadowed,
#: but the declaration is not.
DECLARED_ATTR: str = "__cordis_inject__"

#: The attribute a class or module may set directly.
INJECT_ATTR: str = "inject"

#: The attribute a plugin may set to say which names it will bind. Only a
#: declared provision can take part in cycle detection: a name a body would
#: bind when it ran is invisible while the body is waiting to run.
PROVIDES_ATTR: str = "provides"

#: What a declaration may be written with.
Token: TypeAlias = "str | type[Service]"

_T = TypeVar("_T")


def inject(*tokens: str | type[Service]) -> Callable[[_T], _T]:
    """Declare what a plugin needs, as a decorator.

    Returns the target unchanged with the declaration attached, so decorating
    changes nothing about how the plugin is called -- and, because the type is
    preserved, nothing about how it type-checks either. A decorated function is
    still exactly the function that was written.
    """
    names = _names(tokens, subject=None)

    def declare(target: _T) -> _T:
        try:
            setattr(target, DECLARED_ATTR, names)
        except (AttributeError, TypeError) as exc:  # slots, builtins, C types
            raise InvalidPluginError(
                target, "cannot carry an injection declaration"
            ) from exc
        return target

    return declare


def dependencies_of(target: object) -> tuple[str, ...]:
    """The registry names ``target`` declares it cannot run without.

    The decorator's attribute wins over the plain one: a module can both import
    :func:`inject` and decorate a function with it, and only one of the two
    readings is a declaration.
    """
    declared = getattr(target, DECLARED_ATTR, None)
    if declared is not None:
        return _names(declared, subject=target)
    return _names(_readable(target, INJECT_ATTR), subject=target)


def provisions_of(target: object) -> tuple[str, ...]:
    """The registry names ``target`` promises to bind.

    A :class:`~cordis.registry.Service` subclass promises its own ``name``
    without saying so; anything else must declare it. This is what makes a
    cycle decidable while every member of it is still PENDING and nothing has
    been bound at all.
    """
    if isinstance(target, type) and issubclass(target, Service):
        return (target.name,)
    return _names(_readable(target, PROVIDES_ATTR), subject=target)


def _readable(target: object, attribute: str) -> object:
    """The attribute's value, unless it is obviously not a declaration.

    ``from cordis import inject`` puts a function under the same name a
    declaration would use. Rejecting non-sequences here means that import reads
    as "declares nothing" rather than as a malformed declaration -- a mistake
    that would otherwise turn an ordinary import into a mount failure.
    """
    value = getattr(target, attribute, None)
    if value is None or isinstance(value, (str, bytes)) or callable(value):
        return ()
    return value


def _names(tokens: object, *, subject: object) -> tuple[str, ...]:
    """Normalise a declaration to registry names, in declaration order.

    Order is kept and duplicates are dropped: the order is what error messages
    and the pending-reason list read back, and a name declared twice is one
    dependency.
    """
    if not isinstance(tokens, (list, tuple, set, frozenset)):
        raise InvalidPluginError(
            subject if subject is not None else tokens,
            "an injection declaration is a sequence of names or Service classes",
        )
    seen: dict[str, None] = {}
    for token in _ordered(tokens):
        name = _name_of(token, subject=subject)
        seen.setdefault(name, None)
    return tuple(seen)


def _ordered(tokens: object) -> Sequence[object]:
    if isinstance(tokens, (set, frozenset)):
        # A set has no order to preserve, so give it one that is at least the
        # same on every run: an unordered declaration must not make two
        # otherwise identical mounts report their missing names differently.
        return sorted(tokens, key=lambda token: _label(token))
    return tuple(tokens)  # type: ignore[arg-type]


def _name_of(token: object, *, subject: object) -> str:
    """What this token is called, by the one rule the registry publishes."""
    return service_name(token, subject=subject)


def _label(token: object) -> str:
    return token_label(token)
