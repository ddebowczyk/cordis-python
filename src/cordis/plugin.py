"""Plugin mounting: the unit of composition.

Implements ``spec/capabilities/03-plugin-mounting.yaml``.

A *plugin* is any of four ordinary Python things -- a function taking
``(ctx)`` or ``(ctx, config)``, an object or module exposing ``apply``, or a
:class:`~cordis.registry.Service` subclass. Nothing has to import this module
to be one, which is the point: a unit of an application should be readable as
plain Python and testable by calling it.

Mounting a target creates exactly one child context and one child scope
(SEM-002) and returns a :class:`PluginHandle`. The handle is the owner of
everything that mount produced, and disposing it unwinds all of it --
including every instance the plugin mounted in turn, and those first (SEM-004).

Three things are worth knowing before reading the code.

**Validation happens before construction.** :func:`normalise` runs first and
raises :class:`~cordis.errors.InvalidPluginError` before a context, a scope or
a binding exists, so a rejected mount leaves nothing behind (SEM-001).

**A plugin body's failure belongs to that plugin.** An exception from a body
never reaches the mount call: it marks the instance FAILED, unwinds what the
body had already acquired, and is re-raised only to a caller that awaits the
handle (SEM-005, SEM-007). One broken plugin cannot cancel its siblings.

**The plugin body's own context carries what it needs.** The child context is
extended with the instance's scope, its config, and a bound ``plugin``
callable, so a body reaches all three through the ``ctx`` it was handed:
``scope_of(ctx)``, ``ctx.config``, ``ctx.plugin(...)``. This reuses the one
inheritance rule the context tree already has instead of adding a second.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, TypeAlias
from weakref import WeakKeyDictionary

from cordis.config import resolve_config, schema_of
from cordis.context import Context
from cordis.effect import EffectScope
from cordis.errors import ConfigValidationError, InactiveScopeError, InvalidPluginError
from cordis.fiber import LIVE, Fiber, FiberRuntime, FiberState, _equal
from cordis.inject import dependencies_of, provisions_of
from cordis.intercept import intercept_all
from cordis.realm import isolate
from cordis.registry import Service, ServiceRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis.config import ConfigSchema
    from cordis.intercept import Interception
    from cordis.realm import Isolation

__all__ = [
    "CONFIG_KEY",
    "FIBER_KEY",
    "MOUNT_KEY",
    "SCOPE_KEY",
    "ConfigPreparer",
    "PluginForm",
    "PluginHandle",
    "PluginHost",
    "PluginTarget",
    "config_of",
    "fiber_of",
    "normalise",
    "scope_of",
]

#: Where the mounting machinery stores an instance's own scope. Dunder-shaped
#: so ``ctx.__scope__`` is a passthrough name that never reaches the resolver;
#: :func:`scope_of` is how it is read.
SCOPE_KEY: Final = "__scope__"

#: The instance's config, under a plain name so ``ctx.config`` reads naturally.
CONFIG_KEY: Final = "config"

#: The bound mount callable, so ``ctx.plugin(target, config)`` mounts a child
#: of *this* instance rather than of whatever context happens to be in scope.
MOUNT_KEY: Final = "plugin"

#: The instance itself, for anything that has to *name* it -- a log record, a
#: diagnostic. The mount callable above already keeps the handle alive from its
#: own context, so this is a dict entry and not a new reference; reading the
#: handle off that callable's ``__self__`` instead would make attribution
#: depend on how the mount frame happens to be spelled.
FIBER_KEY: Final = "__fiber__"

PluginTarget: TypeAlias = object

#: Turns the config an instance was mounted with into the config it runs with,
#: given the context it will run in. Called once per load, while the instance's
#: environment is being built: after its injections are bound and its isolation
#: and interception are applied, and before the config reaches either the
#: schema or the body. That moment is the only one at which "compute this in
#: the environment the instance will actually run in" is expressible -- the
#: context does not exist before the mount, and the config is part of it.
ConfigPreparer: TypeAlias = "Callable[[Context, object], object]"

_ARITY_CACHE: WeakKeyDictionary[Any, PluginForm] = WeakKeyDictionary()


# --------------------------------------------------------------------------
# Reading what a body needs off its own context
# --------------------------------------------------------------------------


def _nearest(ctx: Context, key: str) -> object:
    """The nearest value for ``key`` along the lineage, or ``None``.

    Walks the tree directly rather than going through ``ctx.get``: these keys
    are the mounting machinery's own, and resolving them as services would
    make a plugin able to shadow the scope it is being torn down by.
    """
    for node in ctx.lineage():
        meta = node.own_meta
        if key in meta:
            return meta[key]
    return None


def scope_of(ctx: Context) -> EffectScope:
    """The effect scope owned by the instance ``ctx`` belongs to."""
    found = _nearest(ctx, SCOPE_KEY)
    if not isinstance(found, EffectScope):
        msg = "context does not belong to a mounted plugin: no scope in its lineage"
        raise LookupError(msg)
    return found


def config_of(ctx: Context) -> object:
    """The config the instance ``ctx`` belongs to was mounted with."""
    return _nearest(ctx, CONFIG_KEY)


def fiber_of(ctx: Context) -> Fiber | None:
    """The instance ``ctx`` belongs to, or ``None`` outside any mount.

    Absence is an answer here rather than an error: a context made directly --
    a test's, a library's -- has no instance, and the callers that want a name
    for it have a better one to fall back on than an exception.
    """
    found = _nearest(ctx, FIBER_KEY)
    return found if isinstance(found, Fiber) else None


# --------------------------------------------------------------------------
# Normalisation (SEM-001)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginForm:
    """One target, reduced to "what to call and with how many arguments".

    Produced once per target and cached, so the arity inspection SEM-001 needs
    is paid at the first mount rather than at every mount. The config is
    deliberately *not* part of it: caching a resolved config on the target is
    exactly how the second mount of a module ends up running with the first
    one's settings (PROP-PLUGIN-006).
    """

    label: str
    call: Callable[..., object]
    arity: int
    service: type[Service] | None = None


def _describe(target: object) -> str:
    for attribute in ("__qualname__", "__name__"):
        name = getattr(target, attribute, None)
        if isinstance(name, str):
            return name
    return type(target).__name__


def _arity_bounds(fn: Callable[..., object]) -> tuple[int, float]:
    """How many positional arguments ``fn`` requires, and how many it accepts."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins without introspectable signatures
        return (0, 0)

    required = 0
    accepted: float = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            accepted = float("inf")
            continue
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            continue
        accepted += 1
        if parameter.default is inspect.Parameter.empty:
            required += 1
    return (required, accepted)


def _fit(target: object, fn: Callable[..., object], *, offset: int = 0) -> int:
    """Choose between the ``(ctx)`` and ``(ctx, config)`` forms, or reject.

    Config-taking wins whenever the callable can accept it, so a plugin
    written as ``def apply(ctx, config=None)`` is handed its config rather
    than silently running without one.
    """
    if inspect.isasyncgenfunction(fn) or inspect.isgeneratorfunction(fn):
        # Calling one of these returns a generator and runs no body at all, so
        # accepting it would mount an instance that silently did nothing. The
        # yield-once form is a plausible fourth form (see the capability's open
        # questions); until it exists, saying so beats a mystery no-op.
        raise InvalidPluginError(
            target,
            "a generator body is not a plugin form; register cleanup with "
            "`scope_of(ctx).effect(...)` instead of yielding",
        )

    required, accepted = _arity_bounds(fn)
    required = max(required - offset, 0)
    accepted -= offset
    if required <= 2 <= accepted:
        return 2
    if required <= 1 <= accepted:
        return 1
    wanted = "no arguments" if accepted <= 0 else f"{required} required arguments"
    raise InvalidPluginError(
        target, f"a plugin takes (ctx) or (ctx, config); this takes {wanted}"
    )


def normalise(target: object) -> PluginForm:
    """Reduce ``target`` to a :class:`PluginForm`, or reject it.

    Order matters: a ``Service`` subclass is a service before it is a class,
    and an object exposing ``apply`` is a plugin module before it is a
    callable. A plain class is neither, and saying so is the difference
    between a clear rejection and an instance constructed by accident.
    """
    try:
        cached = _ARITY_CACHE.get(target)
    except TypeError:
        # The target cannot be a weak key -- it is unhashable, or its type
        # holds no weak references. Both mean the same thing: inspect it again
        # rather than pin it in memory. Asking the cache is also the only
        # portable way to ask; `type(target).__weakrefoffset__` is a CPython
        # detail that PyPy does not define at all.
        return _classify(target)

    if cached is not None:
        return cached

    form = _classify(target)
    _ARITY_CACHE[target] = form
    return form


def _classify(target: object) -> PluginForm:
    label = _describe(target)

    if isinstance(target, type) and issubclass(target, Service):
        # `self` is not part of the plugin's signature.
        arity = _fit(target, target.__init__, offset=1)
        return PluginForm(label=label, call=target, arity=arity, service=target)

    apply = getattr(target, "apply", None)
    if apply is not None:
        if not callable(apply):
            raise InvalidPluginError(target, "its `apply` attribute is not callable")
        return PluginForm(label=label, call=apply, arity=_fit(target, apply))

    if isinstance(target, type):
        raise InvalidPluginError(
            target, "a class must subclass Service or expose `apply` to be a plugin"
        )

    if not callable(target):
        raise InvalidPluginError(
            target, "expected a callable, an object with `apply`, or a Service subclass"
        )

    return PluginForm(label=label, call=target, arity=_fit(target, target))


# --------------------------------------------------------------------------
# The mounted instance
# --------------------------------------------------------------------------


class PluginHandle(Fiber):
    """One mounted instance: its context, its scope, its state, its children.

    Returned by every mount and awaitable (SEM-005): the same call serves a
    caller that wants fire-and-forget composition and one that wants to assert
    that startup succeeded.

    A mounted instance *is* a fiber -- one instance has one state machine, and
    two objects sharing it would need a synchronisation rule nothing else in
    the system has. :class:`~cordis.fiber.Fiber` owns the states, the settle
    protocol and disposal ordering; what is added here is the part only this
    capability knows: how to turn a target into a body, and what environment
    that body runs in.
    """

    __slots__ = (
        "_form",
        "_held",
        "_host",
        "_intercept",
        "_isolate",
        "_minted",
        "_origin",
        "_prepare",
        "_raw",
        "_schema",
    )

    def __init__(
        self,
        host: PluginHost,
        parent: PluginHandle | None,
        label: str,
        form: PluginForm | None,
        config: object = None,
        requires: tuple[str, ...] = (),
        provides: tuple[str, ...] = (),
        schema: ConfigSchema[Any] | None = None,
        isolation: Isolation = (),
        interception: Interception | None = None,
        prepare: ConfigPreparer | None = None,
    ) -> None:
        self._host = host
        self._form = form
        self._minted = 0
        self._schema = schema
        self._isolate = isolation
        self._intercept = interception or {}
        self._raw = config
        self._prepare = prepare
        self._held: Exception | None
        # Resolved before anything is constructed, so an invalid config never
        # produces a context, a scope or a binding -- the same order SEM-001
        # imposes on normalisation. A preparer moves that to the one moment it
        # can happen at instead: a config that has not been computed yet is not
        # a config that can be checked.
        if prepare is None:
            self._held, resolved = self._resolve(config, label)
        else:
            self._held, resolved = None, config
        super().__init__(
            runtime=host.runtime,
            registry=host.registry,
            parent=parent,
            label=label,
            config=resolved,
            requires=requires,
            provides=provides,
        )

    def _resolve(
        self, config: object, label: str
    ) -> tuple[ConfigValidationError | None, object]:
        """Resolve ``config``, holding a validation failure rather than raising.

        A mount whose config does not validate still returns a handle: the
        failure belongs to the fiber, not to the caller (plugin-mounting
        SEM-005), and holding the error until the load reaches it means FAILED
        is arrived at through the one path the system already has. Every other
        rejection -- an async validator, a malformed declaration -- is a defect
        in the *code* rather than in the *data* and is raised where it is
        written.
        """
        try:
            return None, resolve_config(self._schema, config, plugin=label)
        except ConfigValidationError as exc:
            return exc, config

    @property
    def form(self) -> PluginForm | None:
        """What this instance was built from, normalised. ``None`` at the root.

        Read-only, because changing what an instance *is* means remounting it.
        Public because "what is mounted here" is a question the tree should be
        able to answer: hot reload asks it for the module an instance came
        from, and a diagnostic that wants the target's identity rather than its
        label asks it for the same thing.
        """
        return self._form

    @property
    def raw_config(self) -> object:
        """The config as submitted, before the schema saw it.

        Kept alongside the resolved value because it is what an operator wrote:
        a diagnostic that echoed the resolved config would show fields nobody
        typed.
        """
        return self._raw

    # -- the environment a load runs in ------------------------------------

    def _create_scope(self) -> EffectScope:
        parent = self._parent
        if parent is None:
            return EffectScope(self._label)
        return parent._nursery.child(self._label)

    def _create_context(self, scope: EffectScope) -> Context:
        parent = self._parent
        origin = (
            Context(resolver=self._host.registry, label=self._label)
            if parent is None
            else parent._context
        )
        # Isolation is applied before the instance's own frame, so a name this
        # instance isolates resolves in its realm for the body and for
        # everything the body mounts. Re-applied on every load rather than
        # captured once: an unlabelled realm is private to one load, which is
        # what makes a restart a fresh start (service-isolation SEM-006).
        origin = isolate(origin, self._isolate)
        # Interception is applied in the same place and for the same reason:
        # a subtree configured at its mount stays configured across a reload,
        # which a chain held only by the caller's context would not
        # (service-interception SEM-001).
        origin = intercept_all(origin, self._intercept)
        # Kept for the length of this load: an unlabelled isolation mints a
        # realm on every call, so a computed config that rebuilt the origin
        # would be evaluated in a realm the body never sees.
        self._origin = origin
        return self._frame(scope)

    def _frame(self, scope: EffectScope) -> Context:
        """The instance's own context: one frame over the load's origin.

        Scope, config and mount callable ride the context tree's one
        inheritance rule as scoped metadata, so `scope_of(ctx)`,
        `config_of(ctx)` and `ctx.plugin(...)` all resolve to the nearest
        enclosing instance. Built twice for a computed config -- once without
        it, once with -- but only ever one frame deep, because the second is
        built over the same origin rather than over the first.
        """
        return self._origin.extend(
            **{
                SCOPE_KEY: scope,
                MOUNT_KEY: self.plugin,
                FIBER_KEY: self,
                CONFIG_KEY: self._config,
            }
        )

    def _prepare_config(self) -> None:
        """Compute the config, then validate it, holding either failure.

        Called at the top of the load rather than while the environment is
        being built, because the environment is built at construction and
        again after every unload -- moments at which this instance's declared
        injections are not yet bound. The load is the first moment they are,
        which is what config-expressions SEM-004 asks for.

        Both halves are held rather than raised for the same reason the
        uncomputed case holds a validation failure: an expression that cannot
        be evaluated is this instance's problem, and the fiber has exactly one
        way of expressing that.
        """
        prepare = self._prepare
        if prepare is None:  # pragma: no cover -- guarded by the caller
            return
        # The one context an instance without a computed config never sees:
        # everything the body's own context resolves, minus the config that is
        # still being computed. Nothing registers on it and no body receives
        # it -- it exists so that "computed in the environment the instance
        # runs in" can be true of isolation and interception too.
        try:
            prepared = prepare(self._context, self._raw)
        except Exception as exc:
            self._held, self._config = exc, self._raw
        else:
            self._held, self._config = self._resolve(prepared, self._label)
        self._context = self._frame(self._scope)

    def _run_body(self) -> object:
        if self._prepare is not None:
            self._prepare_config()
        if self._held is not None:
            # SEM-001: the body never runs on a config that did not validate.
            # Raised here rather than at the mount so the fiber fails the way
            # every other failed body does.
            raise self._held
        form = self._form
        if form is None:  # the root: an anchor, not a mounted plugin
            return None
        arguments = (
            (self._context, self._config) if form.arity == 2 else (self._context,)
        )
        produced = form.call(*arguments)
        if form.service is None:
            return produced
        # A Service subclass is a plugin whose whole body is "provide me". It
        # binds where its own context resolves the name, so a service mounted
        # inside an isolated subtree stays inside it (service-isolation
        # SEM-004).
        self._host.registry.provide(
            form.service.name, produced, scope=self._scope, ctx=self._context
        )
        return None

    # -- mounting ----------------------------------------------------------

    def plugin(
        self,
        target: PluginTarget,
        config: object = None,
        /,
        *,
        requires: tuple[str, ...] | None = None,
        isolate: Isolation = (),
        intercept: Interception | None = None,
        prepare: ConfigPreparer | None = None,
    ) -> PluginHandle:
        """Mount ``target`` as a child of this instance.

        Reached as ``ctx.plugin(...)`` from a plugin body, which is the same
        call: the child context carries this method under :data:`MOUNT_KEY`.

        ``requires`` names the services the instance cannot run without; it
        stays PENDING until every one of them is bound (fiber-lifecycle
        SEM-004). Normally it is left alone: the names are read off the
        target's own ``@inject`` declaration (capability 05). Passing it
        *replaces* the declaration rather than adding to it, which is what
        makes it useful -- mounting a third-party plugin against a renamed
        service is otherwise impossible without editing it. ``None`` and
        ``()`` are therefore different answers: the first means "read the
        declaration", the second means "it has none".

        ``isolate`` names the services this instance and its descendants
        resolve privately: ``ctx.plugin(shell_group, isolate=("shell",))``
        gives that subtree its own shell, and ``isolate={"shell": "test"}``
        gives it one shared with every other subtree isolating ``shell`` under
        that label. Declared at the mount rather than by mounting into a forked
        context, because an instance rebuilds its context on every reload and
        an isolation held elsewhere would quietly stop applying.

        ``intercept`` configures services this subtree shares with everyone
        else: ``ctx.plugin(worker, intercept={"shell": {"timeout": 500}})``
        gives the subtree a stricter shell without giving it a second shell.
        The two keywords are the two axes -- ``isolate`` changes *which*
        instance the subtree resolves, ``intercept`` changes how the one it
        already resolves behaves for it (service-interception SEM-004).

        ``prepare`` computes the config in the environment the instance will
        run in, and is what the loader passes for an entry whose config holds
        expressions (config-expressions SEM-004). Giving one moves config
        validation from here to the load, because a value that has not been
        computed cannot be checked; giving none changes nothing at all.
        """
        form = normalise(target)  # before anything is created (SEM-001)
        schema = schema_of(target)  # and before that, that the schema is usable
        self._require_live()
        declared = dependencies_of(target) if requires is None else requires

        # The ordinal counts mounts, not living children: a disposed child
        # leaves the list, and numbering from the list length would hand its
        # label to the next mount. Two distinct instances sharing a label make
        # every label-keyed observer -- logs, metrics, the tests' own recorder
        # -- attribute one instance's transitions to another.
        label = f"{self._label}/{form.label}#{self._minted}"
        self._minted += 1
        child = PluginHandle(
            self._host,
            self,
            label,
            form,
            config,
            declared,
            provisions_of(target),
            schema,
            isolate,
            intercept,
            prepare,
        )
        self._children.append(child)
        child._arm()
        return child

    async def update(self, config: object, /) -> None:
        """Adopt ``config``, comparing *resolved* values (SEM-002).

        Two raw inputs that resolve to the same value -- a re-read file with
        reordered keys, an omitted field that defaults to what was written --
        are one config, and the instance does not restart. That falls out of
        resolving before the comparison rather than being a rule of its own.

        A computed config is compared as written instead, for the same reason
        it is validated late: what it resolves to is not known until the
        instance's next environment exists. Two identical expressions are one
        config; anything else restarts and is computed again there.
        """
        if self._prepare is not None:
            if _equal(config, self._raw):
                return
            self._raw = config
            self._held = None
            await super().update(config)
            return
        held, resolved = self._resolve(config, self._label)
        self._raw = config
        self._held = held
        await super().update(resolved)

    def _require_live(self) -> None:
        """Refuse to mount into an instance that is on its way out.

        A FAILED instance\'s rollback is scheduled, not finished, so its scope
        may still accept a child for a turn or two. Deciding on the state
        rather than on the scope makes the refusal deterministic: whether a
        mount is rejected must not depend on how many event-loop turns have
        passed since the failure.
        """
        if self._state in LIVE:
            return
        raise InactiveScopeError(
            f"{self._label} ({self._state.name.lower()})", "mount a plugin"
        )

    @property
    def children(self) -> tuple[PluginHandle, ...]:
        """The instances this one mounted, in mount order."""
        return tuple(
            child for child in self._children if isinstance(child, PluginHandle)
        )


class PluginHost:
    """A root instance and the registry its plugin tree binds services in.

    An application is one host. Everything else is reached from
    ``host.root.context``, which is the context a top-level mount extends, and
    from ``host.runtime``, which is where transitions are announced and where
    "is anything still starting?" has an answer.
    """

    __slots__ = ("_root", "registry", "runtime")

    def __init__(
        self, registry: ServiceRegistry | None = None, *, label: str = "root"
    ) -> None:
        self.registry = registry if registry is not None else ServiceRegistry()
        self.runtime = FiberRuntime()
        self._root = PluginHandle(self, None, label, None)
        # The root is the tree\'s anchor, not a mounted plugin: it has no body
        # to run and no dependencies to wait for, so it is ACTIVE by
        # construction. Assigning rather than transitioning is deliberate --
        # PENDING -> ACTIVE is not an edge, and announcing a transition nobody
        # could have subscribed to yet would be noise in every status log.
        self._root._state = FiberState.ACTIVE

    @property
    def root(self) -> PluginHandle:
        """The instance every top-level plugin is mounted under."""
        return self._root

    async def dispose(self) -> None:
        """Unload the whole application."""
        await self._root.dispose()
