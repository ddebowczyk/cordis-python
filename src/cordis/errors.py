"""The error taxonomy, and mount-site attribution.

Every failure the specification names lands in exactly one class here. The
module imports nothing from the rest of the package, so any capability may
depend on it without creating a cycle.

Two design rules shape the hierarchy.

**Every error is also the builtin a caller would already be catching.**
``ServiceNotFoundError`` is an ``AttributeError``, so ``hasattr(ctx, "db")``
answers ``False`` instead of exploding and a debugger can still introspect a
Context. ``InvalidEffectError`` is a ``TypeError`` because returning the wrong
shape from an effect factory is a type error. Code that never heard of Cordis
keeps working; code that has can catch :class:`CordisError` and get all of it.

**Attribution annotates, it does not wrap.** When an exception escapes a plugin
body, :func:`mount_attribution` attaches a note naming the mount site and
re-raises the original object. The type, the ``__traceback__`` and any
``__cause__`` survive, so a caller's ``except MyError`` keeps working when the
plugin is mounted by the loader rather than called directly (diagnostics
SEM-004, PROP-DIAG-005).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

__all__ = [
    "MOUNT_NOTE_PREFIX",
    "AsyncValidationError",
    "ConfigValidationError",
    "CordisError",
    "DependencyCycleError",
    "EventModeError",
    "ExpressionError",
    "InactiveScopeError",
    "InvalidEffectError",
    "InvalidPluginError",
    "IssueLike",
    "NextCalledTwiceError",
    "PatchTargetError",
    "RegistryConflictError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "attribute_mount_site",
    "mount_attribution",
    "mount_sites",
]

_E = TypeVar("_E", bound="CordisError")


def _rebuild(cls: type[_E], message: str, fields: dict[str, object]) -> _E:
    """Reconstruct an error without re-running its ``__init__``.

    Unpickling must not depend on a constructor signature staying stable, and
    some fields (``AttributeError.name``) live in slots rather than ``__dict__``,
    so they are restored with ``setattr``.
    """
    error = cls.__new__(cls)
    Exception.__init__(error, message)
    for key, value in fields.items():
        setattr(error, key, value)
    return error


class CordisError(Exception):
    """Base class for every error the framework raises deliberately.

    ``code`` is a stable, machine-readable discriminator. It is what belongs in
    a log line or an error response; the message is for a human and may be
    reworded without it counting as a breaking change.
    """

    code: ClassVar[str] = "CORDIS_ERROR"

    #: Names of the attributes that carry this error's structured payload.
    #: Declared here so pickling and ``__repr__`` need no per-class support.
    fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""

    def details(self) -> dict[str, object]:
        """The structured payload, for logging and for tests."""
        return {name: getattr(self, name) for name in type(self).fields}

    def __reduce__(self) -> tuple[Callable[..., CordisError], tuple[object, ...]]:
        return (_rebuild, (type(self), self.message, self.details()))

    def __repr__(self) -> str:
        payload = "".join(f", {k}={v!r}" for k, v in self.details().items())
        return f"{type(self).__name__}({self.message!r}{payload})"


# --------------------------------------------------------------------------
# Contexts and services
# --------------------------------------------------------------------------


class ServiceNotFoundError(CordisError, AttributeError):
    """A name resolved through a Context matched no service and no scoped key.

    Also an ``AttributeError``: attribute access that finds nothing must fail
    the way Python says it fails, or ``hasattr``, ``getattr(..., default)`` and
    every ``__deepcopy__``-style protocol probe in the standard library break
    on contact with a Context (context-tree SEM-002, PROP-CTX-004).
    """

    code: ClassVar[str] = "SERVICE_NOT_FOUND"
    fields: ClassVar[tuple[str, ...]] = ("name", "searched")

    def __init__(self, name: str, searched: Sequence[str] = ()) -> None:
        trail = " -> ".join(searched)
        where = f" (searched: {trail})" if trail else ""
        super().__init__(f"no service or scoped value named {name!r}{where}")
        # AttributeError.name, so CPython's suggestion machinery sees it too.
        self.name = name
        self.searched: tuple[str, ...] = tuple(searched)


class ServiceConflictError(CordisError):
    """A second provider claimed a service name that is already bound.

    The first binding is left intact; the claimant is the one that fails
    (registry SEM-004, PROP-REG-004).
    """

    code: ClassVar[str] = "SERVICE_CONFLICT"
    fields: ClassVar[tuple[str, ...]] = ("name", "holder", "claimant")

    def __init__(self, name: str, holder: str, claimant: str) -> None:
        super().__init__(
            f"service {name!r} is already provided by {holder}; "
            f"{claimant} cannot provide it as well"
        )
        self.name = name
        self.holder = holder
        self.claimant = claimant


class RegistryConflictError(CordisError, ValueError):
    """A capability registry already holds an entry under the offered key.

    Distinct from :class:`ServiceConflictError`, which is about the service
    binding set: a duplicate tool name is not a duplicate service, and telling
    an author their tool "is already provided by" something would send them
    looking in the wrong registry (capability-seam SEM-004).

    Raised during validation, so nothing has been mutated by the time it is
    raised and the incumbent entry is untouched.
    """

    code: ClassVar[str] = "REGISTRY_CONFLICT"
    fields: ClassVar[tuple[str, ...]] = ("registry", "key")

    def __init__(self, registry: str, key: str) -> None:
        super().__init__(
            f"{registry} already holds an entry under {key!r}; "
            f"the candidate was rejected and nothing was changed"
        )
        self.registry = registry
        self.key = key


class DependencyCycleError(CordisError):
    """Declared injections form a cycle, so no member of it can ever activate."""

    code: ClassVar[str] = "DEPENDENCY_CYCLE"
    fields: ClassVar[tuple[str, ...]] = ("cycle",)

    def __init__(self, cycle: Sequence[str]) -> None:
        closed = [*cycle, cycle[0]] if cycle else []
        super().__init__("dependency cycle: " + " -> ".join(closed))
        self.cycle: tuple[str, ...] = tuple(cycle)

    @property
    def names(self) -> frozenset[str]:
        """The members, without the traversal order.

        The same cycle can be reported from any of its members, so the path is
        good for reading and useless for deciding whether two reports are the
        same cycle. This is the form to compare.
        """
        return frozenset(self.cycle)


# --------------------------------------------------------------------------
# Effects and fibers
# --------------------------------------------------------------------------


class InvalidEffectError(CordisError, TypeError):
    """An effect factory returned something that is not a disposer.

    A ``TypeError`` because that is what returning the wrong shape is. The
    scope's recorded effects are unchanged when this is raised: an effect that
    cannot be undone is never recorded as done (effect-scope SEM-003,
    PROP-EFF-008).
    """

    code: ClassVar[str] = "INVALID_EFFECT"
    fields: ClassVar[tuple[str, ...]] = ("returned",)

    def __init__(self, returned: object) -> None:
        kind = type(returned).__name__
        super().__init__(
            f"effect returned {kind}; expected a callable disposer, an "
            f"awaitable of one, an iterable of them, or None"
        )
        self.returned = kind


class InactiveScopeError(CordisError, RuntimeError):
    """Something was registered against a scope that is already disposed.

    Silently accepting the registration would leak it: nothing will ever run
    its disposer (effect-scope SEM-007, PROP-EFF-007).
    """

    code: ClassVar[str] = "INACTIVE_SCOPE"
    fields: ClassVar[tuple[str, ...]] = ("scope", "operation")

    def __init__(self, scope: str, operation: str = "register an effect") -> None:
        super().__init__(f"cannot {operation}: scope {scope} is disposed")
        self.scope = scope
        self.operation = operation


class InvalidPluginError(CordisError, TypeError):
    """An object was mounted that is not a plugin in any of the accepted forms.

    Raised before any context, scope or binding is created, so a rejected
    mount leaves nothing behind (plugin-mounting SEM-001, PROP-MOUNT-006).
    """

    code: ClassVar[str] = "INVALID_PLUGIN"
    fields: ClassVar[tuple[str, ...]] = ("plugin", "reason")

    def __init__(self, plugin: object, reason: str) -> None:
        shown = getattr(plugin, "__name__", None) or repr(plugin)
        super().__init__(f"{shown} is not a plugin: {reason}")
        self.plugin = str(shown)
        self.reason = reason


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@runtime_checkable
class IssueLike(Protocol):
    """The shape of a validation issue, as this module needs to render it.

    Structural on purpose. ``cordis.config.ConfigIssue`` satisfies it, and so
    does an adapter's own issue type, which keeps the error hierarchy free of
    an import from a higher tier.
    """

    @property
    def path(self) -> tuple[str | int, ...]: ...

    @property
    def message(self) -> str: ...


def _render_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "<root>"
    out = str(path[0])
    for part in path[1:]:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


class ConfigValidationError(CordisError, ValueError):
    """A plugin's configuration did not satisfy its declared schema.

    Carries every issue, not just the first: a config file with four mistakes
    should take one round trip to fix, not four (config-validation SEM-003).
    """

    code: ClassVar[str] = "CONFIG_VALIDATION"
    fields: ClassVar[tuple[str, ...]] = ("plugin", "issues")

    def __init__(self, plugin: str, issues: Sequence[IssueLike]) -> None:
        lines = "".join(
            f"\n  {_render_path(issue.path)}: {issue.message}" for issue in issues
        )
        count = len(issues)
        plural = "" if count == 1 else "s"
        super().__init__(f"invalid config for {plugin} ({count} issue{plural}):{lines}")
        self.plugin = plugin
        self.issues: tuple[IssueLike, ...] = tuple(issues)


class AsyncValidationError(CordisError, TypeError):
    """A schema's ``validate`` returned an awaitable.

    Validation runs on the synchronous path that decides whether a fiber may
    load at all, so it must complete without yielding to the event loop. This
    is raised where the schema is declared, not where a config eventually
    fails to validate (config-validation SEM-005).
    """

    code: ClassVar[str] = "ASYNC_VALIDATION"
    fields: ClassVar[tuple[str, ...]] = ("schema",)

    def __init__(self, schema: object) -> None:
        shown = getattr(schema, "__name__", None) or type(schema).__name__
        super().__init__(
            f"schema {shown} validates asynchronously; config validation is "
            f"synchronous so that a fiber's fate is decided without awaiting"
        )
        self.schema = str(shown)


class ExpressionError(CordisError, ValueError):
    """A configuration expression was rejected or failed to evaluate.

    Covers both halves of the restricted evaluator: syntax outside the
    permitted grammar, and a permitted expression that exceeded its step
    budget or referenced an unknown name (config-expressions SEM-002).
    """

    code: ClassVar[str] = "EXPRESSION"
    fields: ClassVar[tuple[str, ...]] = ("entry_id", "field", "source", "reason")

    def __init__(self, entry_id: str, field: str, source: str, reason: str) -> None:
        super().__init__(f"{entry_id}.{field}: {reason} in expression {source!r}")
        self.entry_id = entry_id
        self.field = field
        self.source = source
        self.reason = reason


class PatchTargetError(CordisError, ValueError):
    """A config-layer patch cannot be applied to the entry it names.

    One error for every way that happens -- no such entry (SEM-004), a name
    that does not match (SEM-005), an insert into something that is not a
    group (SEM-003), an insert whose id is already taken -- because a caller
    that could catch three of the four would eventually meet the fourth.
    Applying any of them silently would make a typo in a patch look like a
    patch that did nothing.
    """

    code: ClassVar[str] = "PATCH_TARGET"
    fields: ClassVar[tuple[str, ...]] = (
        "patch_id",
        "layer_source",
        "target",
        "reason",
    )

    def __init__(
        self, patch_id: str, layer_source: str, target: str, reason: str
    ) -> None:
        super().__init__(
            f"{patch_id} from {layer_source} cannot be applied to "
            f"entry {target!r}: {reason}"
        )
        self.patch_id = patch_id
        self.layer_source = layer_source
        self.target = target
        self.reason = reason


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class EventModeError(CordisError, TypeError):
    """An event was dispatched through a mode it was not declared with.

    Each of the five dispatch modes has different listener semantics; a
    waterfall listener invoked by ``emit`` would never receive its ``next``
    (event-bus SEM-002, PROP-EVT-009).
    """

    code: ClassVar[str] = "EVENT_MODE"
    fields: ClassVar[tuple[str, ...]] = ("event", "declared", "attempted")

    def __init__(self, event: str, declared: str, attempted: str) -> None:
        super().__init__(
            f"event {event!r} is declared {declared}; it cannot be dispatched "
            f"with {attempted}"
        )
        self.event = event
        self.declared = declared
        self.attempted = attempted


class NextCalledTwiceError(CordisError, RuntimeError):
    """A waterfall listener invoked ``next`` more than once.

    The chain would fork: work after the second call runs against a value the
    rest of the chain has already transformed (event-bus SEM-008).
    """

    code: ClassVar[str] = "NEXT_CALLED_TWICE"
    fields: ClassVar[tuple[str, ...]] = ("event", "listener")

    def __init__(self, event: str, listener: str) -> None:
        super().__init__(
            f"listener {listener} called next() twice while handling {event!r}"
        )
        self.event = event
        self.listener = listener


# --------------------------------------------------------------------------
# Mount-site attribution
# --------------------------------------------------------------------------

#: Every attribution note starts with this, so a reader -- and a test -- can
#: separate the mount trail from notes attached by other libraries.
MOUNT_NOTE_PREFIX = "cordis: while mounting "


def attribute_mount_site(exc: BaseException, site: str) -> None:
    """Record ``site`` on ``exc`` as an enclosing mount site.

    Notes accumulate in call order, which as the exception propagates outward
    means innermost first.
    """
    exc.add_note(f"{MOUNT_NOTE_PREFIX}{site}")


@contextmanager
def mount_attribution(site: str) -> Iterator[None]:
    """Annotate anything escaping this block with the mount site, then re-raise.

    The bare ``raise`` is the point: the exception object, its type and its
    traceback are the originals. Wrapping instead would silently break every
    ``except`` clause a plugin author already wrote.
    """
    try:
        yield
    except BaseException as exc:
        attribute_mount_site(exc, site)
        raise


def mount_sites(exc: BaseException) -> tuple[str, ...]:
    """The mount trail recorded on ``exc``, innermost first."""
    return tuple(
        note.removeprefix(MOUNT_NOTE_PREFIX)
        for note in getattr(exc, "__notes__", ())
        if note.startswith(MOUNT_NOTE_PREFIX)
    )
