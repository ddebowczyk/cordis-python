"""The lifecycle of one mounted instance: its states, and what moves between them.

Implements ``spec/capabilities/04-fiber-lifecycle.yaml``.

A *fiber* is a mounted instance seen as a state machine. It is PENDING while a
declared dependency is missing, LOADING while its body runs, ACTIVE once the
body has returned, FAILED if the body raised, UNLOADING while it gives back
what it took, and DISPOSED once for good. Those six states and the edges
between them (SEM-001, SEM-002, SEM-003) are the whole vocabulary; every
operation below is one path through them.

Four things are worth knowing before reading the code.

**PENDING is a settled state, not a waiting room.** A fiber whose dependency
may never arrive must not hang the caller that awaits it (SEM-006), so
``await fiber`` resolves at PENDING and :attr:`Fiber.missing` says why.

**Readiness is recomputed, never polled.** A fiber with declared dependencies
subscribes to the registry and re-evaluates when a name it needs appears or
disappears. The re-evaluation is *scheduled* rather than run inline, so a
provider registering a service cannot recurse into a consumer's reload while
it is still mid-registration.

**Transitions are announced synchronously, after the assignment.** A listener
reading ``fiber.state`` when it is told about a transition sees the new value
(SEM-005). That is only unambiguous for a synchronous callback, which is why
the status channel is an observer list in the shape the service registry
already uses rather than an event-bus emit.

**Restart shares nothing.** Unloading disposes the fiber's scope and every
instance it mounted; reloading opens a fresh scope and a fresh context
(SEM-007). Reusing the scope would be the classic hot-reload leak where the
tenth save handles every event ten times.

What this module deliberately does *not* know is how to turn a plugin target
into a body, or how to build the context that body runs in.
:class:`cordis.plugin.PluginHandle` supplies both by subclassing :class:`Fiber`.
The dependency runs one way -- ``cordis.plugin`` imports this, never the
reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import graphlib
import inspect
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeAlias

from cordis.errors import CordisError, DependencyCycleError, mount_attribution

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator, Mapping, Sequence

    from cordis.context import Context
    from cordis.effect import EffectNode, EffectScope
    from cordis.registry import BindingChange, ServiceRegistry

__all__ = [
    "SETTLED",
    "TRANSITIONS",
    "Fiber",
    "FiberRuntime",
    "FiberState",
    "IllegalTransitionError",
    "ProblemListener",
    "StatusChange",
    "StatusListener",
    "check_transition",
]


class FiberState(enum.Enum):
    """The six states of a mounted instance (SEM-001).

    ``PENDING`` means a declared required dependency is missing -- a settled
    condition, not a transitional one, which is why it is a state and not a
    flag on ``LOADING``.
    """

    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


#: SEM-002, verbatim, plus SEM-003's terminality expressed as an empty set.
TRANSITIONS: Final[Mapping[FiberState, frozenset[FiberState]]] = {
    FiberState.PENDING: frozenset({FiberState.LOADING, FiberState.UNLOADING}),
    FiberState.LOADING: frozenset({FiberState.ACTIVE, FiberState.FAILED}),
    FiberState.ACTIVE: frozenset({FiberState.UNLOADING}),
    FiberState.FAILED: frozenset({FiberState.UNLOADING}),
    FiberState.UNLOADING: frozenset({FiberState.PENDING, FiberState.DISPOSED}),
    FiberState.DISPOSED: frozenset(),
}

#: States at which nothing is in flight, so an invariant may be asserted --
#: and, by SEM-006, the states at which ``await fiber`` resolves.
SETTLED: Final = frozenset(
    {FiberState.PENDING, FiberState.ACTIVE, FiberState.FAILED, FiberState.DISPOSED}
)

#: The states in which an instance can still take on a child mount. LOADING is
#: among them because a body mounts its children while it is itself loading.
LIVE: Final = frozenset({FiberState.PENDING, FiberState.LOADING, FiberState.ACTIVE})


class IllegalTransitionError(RuntimeError):
    """A transition outside the permitted set was attempted.

    A programming error in the runtime rather than a condition a caller can
    provoke, which is why it is a plain ``RuntimeError`` and not part of the
    ``CordisError`` taxonomy: no application should ever catch it.
    """

    def __init__(self, subject: str, source: FiberState, target: FiberState) -> None:
        permitted = sorted(state.name for state in TRANSITIONS[source])
        super().__init__(
            f"{subject}: {source.name} -> {target.name} is not a permitted "
            f"transition (permitted: {permitted})"
        )
        self.subject = subject
        self.source = source
        self.target = target


def check_transition(subject: str, source: FiberState, target: FiberState) -> None:
    """Raise unless ``source -> target`` is an edge in the table."""
    if target not in TRANSITIONS[source]:
        raise IllegalTransitionError(subject, source, target)


@dataclass(frozen=True)
class StatusChange:
    """One transition, as its listeners see it (SEM-005)."""

    fiber: Fiber
    previous: FiberState
    new: FiberState

    def __repr__(self) -> str:
        return (
            f"StatusChange({self.fiber.label!r}, "
            f"{self.previous.name} -> {self.new.name})"
        )


StatusListener: TypeAlias = "Callable[[StatusChange], None]"
ProblemListener: TypeAlias = "Callable[[CordisError], None]"

#: Identity for instances, handed out in construction order. A diagnostic
#: snapshot outlives the fiber it describes, and `id()` is unique only among
#: live objects -- the same reason `Fiber._sample` holds objects rather than
#: their addresses. A serial cannot be recycled, so two snapshots taken across
#: a reload can never mistake one instance for another (diagnostics SEM-001).
_SERIALS = itertools.count(1)


class FiberRuntime:
    """One application's status channel and its definition of "settled".

    Every fiber in a tree shares one runtime. It exists so that two questions
    have answers that do not depend on guessing: *what just changed* and *is
    anything still in flight*.
    """

    __slots__ = (
        "_faults",
        "_fibers",
        "_idle",
        "_listeners",
        "_problems",
        "_reported",
        "_watchers",
        "_work",
    )

    def __init__(self) -> None:
        self._listeners: list[StatusListener] = []
        self._work: set[asyncio.Task[None]] = set()
        self._faults: list[Exception] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self._fibers: list[Fiber] = []
        self._problems: list[CordisError] = []
        self._reported: set[object] = set()
        self._watchers: list[ProblemListener] = []

    # -- status ------------------------------------------------------------

    def observe(self, listener: StatusListener) -> Callable[[], None]:
        """Register a listener for transitions; returns the undo."""
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return stop

    def announce(self, change: StatusChange) -> None:
        """Tell every listener, and keep whatever they did wrong for later.

        Every listener runs even if an earlier one raised, in the shape the
        service registry established: a broken observer must not be able to
        hide a transition from the others.

        Unlike the registry's, this announcement does *not* raise. A
        transition is announced from inside the transition -- often from a
        task nobody awaits -- and letting a listener's exception out there
        would abandon a fiber mid-state, which is a worse outcome than any
        listener bug. The failures are held and re-raised from
        :meth:`quiesce`, which is a defined, awaited boundary.
        """
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception as exc:  # one listener, one fault
                self._faults.append(exc)

    def drain_faults(self) -> tuple[Exception, ...]:
        """Take the listener failures collected since the last drain."""
        faults = tuple(self._faults)
        self._faults.clear()
        return faults

    # -- work tracking -----------------------------------------------------

    def spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        """Schedule fiber work, counted so :meth:`quiesce` can wait for it.

        Raises ``RuntimeError`` when no loop is running. Deliberately
        ``get_running_loop`` rather than ``ensure_future``: the latter falls
        back to the current-thread loop policy, which in 3.12+ warns and may
        hand back a loop nobody is running -- work scheduled onto it would
        never start, and the fiber would wait for it forever.
        """
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        self._work.add(task)
        self._idle.clear()
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._work.discard(task)
        if not self._work:
            self._idle.set()

    async def quiesce(self) -> None:
        """Return when no fiber has a load, unload, or re-evaluation in flight.

        A defined moment rather than a heuristic sleep: work scheduled *by*
        the work being waited on is waited for too, so a cascade of reloads
        settles completely before this returns.
        """
        while self._work:
            await asyncio.gather(*tuple(self._work), return_exceptions=True)
            # One turn for callbacks that a just-finished task queued but that
            # have not yet had the chance to register their own work.
            await asyncio.sleep(0)
        self.audit()
        faults = self.drain_faults()
        if faults:
            raise ExceptionGroup("status listeners failed", list(faults))

    @property
    def busy(self) -> bool:
        return bool(self._work)

    # -- the fibers themselves ---------------------------------------------

    def enrol(self, fiber: Fiber) -> None:
        """Take note of a live fiber.

        The runtime needs a view across the whole tree, not just one branch: a
        dependency cycle is a property of the set of mounted instances, and
        walking down from the root would miss nothing only as long as every
        fiber is reachable from it -- which is true today and is not something
        this check should depend on.
        """
        self._fibers.append(fiber)

    def retire(self, fiber: Fiber) -> None:
        """Forget a disposed fiber."""
        if fiber in self._fibers:
            self._fibers.remove(fiber)

    @property
    def fibers(self) -> tuple[Fiber, ...]:
        """Every fiber that has been mounted and not yet disposed."""
        return tuple(self._fibers)

    # -- problems ----------------------------------------------------------

    def observe_problem(self, listener: ProblemListener) -> Callable[[], None]:
        """Register a listener for reported problems; returns the undo."""
        self._watchers.append(listener)

        def stop() -> None:
            if listener in self._watchers:
                self._watchers.remove(listener)

        return stop

    @property
    def problems(self) -> tuple[CordisError, ...]:
        """Conditions the runtime found that no caller was in a position to see."""
        return tuple(self._problems)

    def report(self, problem: CordisError, *, key: object = None) -> bool:
        """Record a standing condition once, and say whether it was new.

        Not raised, because there is nobody to raise it to. A cycle is
        discovered while every fiber involved is quietly waiting; the mount
        call that completed the cycle returned successfully some time ago, and
        the loop that would have caught an exception is running other work. So
        the condition is recorded and announced, and it is the application's
        choice whether an unresolvable dependency is fatal.
        """
        token = key if key is not None else (type(problem), str(problem))
        if token in self._reported:
            return False
        self._reported.add(token)
        self._problems.append(problem)
        for watcher in list(self._watchers):
            try:
                watcher(problem)
            except Exception as exc:  # a broken watcher is not a lost problem
                self._faults.append(exc)
        return True

    def audit(self) -> None:
        """Look for dependency cycles among the fibers that are still waiting.

        Run at :meth:`quiesce`, which is the first moment the answer is stable:
        during a cascade half the graph is mid-load, and a fiber that is about
        to bind its service in three turns is indistinguishable from one that
        never will.
        """
        for cycle in _cycles(_blocked_graph(self._fibers)):
            names = tuple(_provision_label(fiber) for fiber in cycle)
            self.report(DependencyCycleError(names), key=frozenset(names))


class Fiber:
    """One mounted instance, seen as a state machine.

    Subclasses supply the two things this class deliberately does not know:
    :meth:`_create_scope` and :meth:`_create_context` build the environment a
    load runs in, and :meth:`_run_body` runs it.
    """

    __slots__ = (
        "_children",
        "_config",
        "_context",
        "_deferred",
        "_dirty",
        "_disposal",
        "_epoch",
        "_error",
        "_evaluation",
        "_gate",
        "_label",
        "_nursery",
        "_parent",
        "_provides",
        "_registry",
        "_requires",
        "_runtime",
        "_scope",
        "_settled",
        "_state",
        "_task",
        "_uid",
        "_watch",
    )

    def __init__(
        self,
        *,
        runtime: FiberRuntime,
        registry: ServiceRegistry,
        parent: Fiber | None,
        label: str,
        config: object = None,
        requires: tuple[str, ...] = (),
        provides: tuple[str, ...] = (),
    ) -> None:
        self._runtime = runtime
        self._registry = registry
        self._parent = parent
        self._uid = next(_SERIALS)
        self._label = label
        self._config = config
        self._requires = requires
        self._provides = provides
        self._epoch: tuple[object, ...] = ()
        self._children: list[Fiber] = []
        self._state = FiberState.PENDING
        self._error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None
        self._evaluation: asyncio.Task[None] | None = None
        self._disposal: asyncio.Task[None] | None = None
        self._watch: Callable[[], None] | None = None
        self._deferred: list[Coroutine[object, object, None]] = []
        self._dirty = False
        self._gate = asyncio.Lock()
        self._settled = asyncio.Event()
        self._settled.set()
        self._scope: EffectScope
        self._nursery: EffectScope
        self._context: Context
        self._open_environment()
        runtime.enrol(self)

    # -- what a caller can see ---------------------------------------------

    @property
    def uid(self) -> int:
        """This instance's identity: a serial, unique for the process's life.

        Two instances of the same plugin differ here and nowhere else, which is
        what makes a snapshot taken across a reload comparable to one taken
        before it.
        """
        return self._uid

    @property
    def label(self) -> str:
        return self._label

    @property
    def state(self) -> FiberState:
        return self._state

    @property
    def config(self) -> object:
        """The config this instance was mounted (or last updated) with."""
        return self._config

    @property
    def requires(self) -> tuple[str, ...]:
        """The names that must be bound for this instance to run."""
        return self._requires

    @property
    def provides(self) -> tuple[str, ...]:
        """The names this instance declared it would bind.

        A declaration, not an observation: it is what the instance *will* bind
        once it loads, which is the only form of the question that can be
        answered while it is still waiting.
        """
        return self._provides

    @property
    def missing(self) -> tuple[str, ...]:
        """Which required names are currently unbound -- why it is PENDING."""
        return tuple(
            name
            for name in self._requires
            if self._registry.lookup(name, ctx=self._context) is None
        )

    def _sample(self) -> tuple[object, ...]:
        """The implementations currently behind this instance's dependencies.

        Held as objects rather than as ids: an id is only unique among live
        objects, and the object this one replaced is exactly the one about to
        be collected. Comparing recycled ids is how a reload gets skipped for
        the one change that most needs it.
        """
        return tuple(
            self._registry.lookup(name, ctx=self._context) for name in self._requires
        )

    @property
    def error(self) -> BaseException | None:
        """The exception the body raised, if it did."""
        return self._error

    @property
    def context(self) -> Context:
        """The child context this instance's body was given."""
        return self._context

    @property
    def scope(self) -> EffectScope:
        """The effect scope this instance owns."""
        return self._scope

    @property
    def children(self) -> tuple[Fiber, ...]:
        """The instances this one mounted, in mount order."""
        return tuple(self._children)

    def effects(self) -> EffectNode:
        """This instance's own effect tree, as registered.

        The instances it mounted are not in here. They are fibers in their own
        right and carry their own trees, so including the nursery would report
        every effect in the application once for each of its ancestors --
        which is the one thing an effect tree exists to make impossible
        (diagnostics SEM-003).
        """
        return self._scope.tree(skip=self._nursery)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._label!r}, {self._state.name})"

    # -- hooks a subclass supplies -----------------------------------------

    def _create_scope(self) -> EffectScope:
        raise NotImplementedError

    def _create_context(self, scope: EffectScope) -> Context:
        raise NotImplementedError

    def _run_body(self) -> object:
        raise NotImplementedError

    # -- the environment one load runs in ----------------------------------

    def _open_environment(self) -> None:
        """A fresh scope, nursery and context. Nothing survives from the last.

        SEM-007 is enforced here rather than at the restart call site: a reload
        that wanted to reuse anything would have to go around this method.
        """
        scope = self._create_scope()
        self._scope = scope
        self._nursery = scope.child(f"{self._label}/children")
        self._context = self._create_context(scope)

    # -- readiness ---------------------------------------------------------

    def _arm(self) -> None:
        """Subscribe to the names this instance needs, then take a first look."""
        if self._requires:
            self._watch = self._registry.observe(self._on_binding_change)
        self._evaluate()

    def _on_binding_change(self, change: BindingChange) -> None:
        if change.name not in self._requires:
            return
        if self._state is FiberState.DISPOSED or self._evaluation is not None:
            return
        # Scheduled, never inline: a provider is mid-registration right now.
        evaluation = self._evaluate_soon()
        try:
            self._evaluation = self._runtime.spawn(evaluation)
        except RuntimeError:
            evaluation.close()  # named, so it can be closed rather than leaked
            # No loop yet. A service can be bound during a synchronous
            # bootstrap, before anything is awaited, and refusing to notice it
            # would leave the instance PENDING forever with its dependency in
            # plain sight. Reconciled at this fiber's next awaited boundary,
            # which is the earliest moment a scheduled load could have run
            # anyway.
            self._dirty = True

    async def _evaluate_soon(self) -> None:
        self._evaluation = None
        await self._quiet()
        self._evaluate()

    def _evaluate(self) -> None:
        """Reconcile the state with what is currently bound (SEM-004).

        Three answers, not two. A running instance whose dependency was
        *replaced* is as wrong as one whose dependency vanished -- it is
        holding an object that has been unloaded -- but nothing about its state
        says so, because the name it needs is bound and always was. The epoch
        is what makes that case visible: the implementations this instance's
        body was handed, compared by identity with the ones behind the same
        names now.
        """
        if self._state is FiberState.PENDING and not self.missing:
            self._load()
        elif self._state is FiberState.ACTIVE:
            if self.missing:
                self._task = self._schedule(self._unload_to(FiberState.PENDING))
            elif not _same(self._epoch, self._sample()):
                self._task = self._schedule(self._reload())

    async def _reload(self) -> None:
        """Unload and load again, without passing through a settled PENDING.

        Reloading rather than merely unloading matters for the caller that is
        awaiting this fiber: an instance whose dependency was swapped is
        expected to come back, and resolving at PENDING in between would report
        a fiber waiting for a service that is bound.
        """
        await self._unload_to(FiberState.PENDING)
        self._evaluate()

    # -- scheduling --------------------------------------------------------

    def _schedule(
        self, coro: Coroutine[object, object, None]
    ) -> asyncio.Task[None] | None:
        """Start ``coro``, or hold it until there is a loop to start it in.

        Mounting is synchronous and does not require a running loop, but the
        work a mount can provoke -- awaiting an asynchronous body, giving back
        what a failed body took -- does. Rather than make the mount call fail
        for a reason that has nothing to do with the plugin, the coroutine is
        held and started at this fiber's next awaited boundary. Until then the
        fiber is not settled, so nothing can observe the gap without waiting
        for it to close.
        """
        try:
            return self._runtime.spawn(coro)
        except RuntimeError:
            self._deferred.append(coro)
            self._settled.clear()
            return None

    # -- loading -----------------------------------------------------------

    def _load(self) -> None:
        """Run the body, and decide what "settled" will mean for this instance.

        Synchronous up to the body's first await: a synchronous body has
        already finished when the mount call returns.
        """
        if not self._scope.active:
            self._open_environment()
        self._error = None
        # Sampled before the body runs, so it records what the body was handed
        # -- not what happened to be bound by the time it finished, which for
        # an asynchronous body can already be something else.
        self._epoch = self._sample()
        self._transition(FiberState.LOADING)
        try:
            with mount_attribution(self._label):
                result = self._run_body()
        except BaseException as exc:  # contained: never reaches the mount caller
            self._fail(exc)
            return

        if inspect.isawaitable(result):
            self._task = self._schedule(self._finish(result))
            return
        self._transition(FiberState.ACTIVE)

    async def _finish(self, result: object) -> None:
        """Await an asynchronous body and record how it ended."""
        try:
            with mount_attribution(self._label):
                await result  # type: ignore[misc]  # isawaitable narrows nothing
        except BaseException as exc:
            self._error = exc
            await self._unwind()
            self._transition(FiberState.FAILED)
            return
        self._transition(FiberState.ACTIVE)

    def _fail(self, exc: BaseException) -> None:
        """Mark a synchronously failing body failed and give back what it took.

        The release cannot happen here -- scope disposal is asynchronous -- so
        it is scheduled. Awaiting the fiber waits for it and then re-raises.
        """
        self._error = exc
        self._transition(FiberState.FAILED)
        self._task = self._schedule(self._unwind())

    # -- unloading ---------------------------------------------------------

    async def _unload_to(self, target: FiberState) -> None:
        """UNLOADING, give everything back, then land on ``target``."""
        if self._state is FiberState.DISPOSED:
            return
        self._transition(FiberState.UNLOADING)
        # Dropped here rather than at the next load: the epoch is the one
        # reference an unloaded instance would otherwise keep to a service that
        # has already been disposed.
        self._epoch = ()
        await self._unwind()
        self._transition(target)
        if target is FiberState.PENDING:
            # A PENDING fiber is still part of the tree and can be reloaded, so
            # it needs a live environment to be reloaded into.
            self._open_environment()

    async def _unwind(self) -> None:
        """Descendants first, then this instance's own effects (SEM-004).

        The two-step order is why this is not simply ``scope.dispose()``: a
        scope unwinds one flat LIFO, which would interleave a child instance
        between two of its parent's effects, and a child's flush-on-dispose
        must not run after the parent closed what it flushes through.
        """
        for child in reversed(tuple(self._children)):
            await child.dispose()
        self._children.clear()
        await self._scope.dispose()

    # -- the operations a caller performs ----------------------------------

    async def restart(self) -> None:
        """A full unload followed by a fresh load (SEM-007).

        Serialised against other restarts, updates and disposals on the same
        fiber: two concurrent restarts are two restarts, one after the other,
        not two bodies running at once.
        """
        async with self._gate:
            await self._quiet()
            if self._state is FiberState.DISPOSED:
                return
            await self._unload_to(FiberState.PENDING)
            self._evaluate()
        await self._settle()

    async def update(self, config: object, /) -> None:
        """Adopt ``config``; restart only if it differs by value (SEM-008)."""
        if _equal(config, self._config):
            return
        async with self._gate:
            if self._state is FiberState.DISPOSED:
                return
            await self._quiet()
            self._config = config
            await self._unload_to(FiberState.PENDING)
            self._evaluate()
        await self._settle()

    async def dispose(self) -> None:
        """Unload this instance and everything it mounted, descendants first.

        Concurrent callers share one unwind, and a second call after it has
        finished is a no-op: DISPOSED is terminal (SEM-003).
        """
        if self._state is FiberState.DISPOSED:
            return
        if self._disposal is None:
            self._disposal = self._runtime.spawn(self._dispose_once())
        await asyncio.shield(self._disposal)

    async def _dispose_once(self) -> None:
        async with self._gate:
            if self._state is FiberState.DISPOSED:
                return
            if self._watch is not None:
                self._watch()  # no notification can start a load from here on
                self._watch = None
            self._dirty = False  # nor can one that arrived before the loop did
            self._runtime.retire(self)
            await self._quiet()
            await self._unload_to(FiberState.DISPOSED)
            if self._parent is not None and self in self._parent._children:
                self._parent._children.remove(self)

    # -- settling ----------------------------------------------------------

    def __await__(self) -> Generator[object, None, Fiber]:
        return self._settle().__await__()

    async def _settle(self) -> Fiber:
        """Wait for a settled state, then re-raise a failure (SEM-006).

        ``self._task`` never raises -- it records the failure instead -- so
        the exception a caller sees is the identical object the body raised,
        not a re-wrapping produced by task machinery.
        """
        await self._quiet()
        if self._error is not None and self._state is FiberState.FAILED:
            raise self._error
        return self

    async def _quiet(self) -> None:
        """Wait until nothing this fiber started is still running.

        Also the moment a binding change that arrived before the loop existed
        is finally acted on: this runs inside a loop by construction.
        """
        while True:
            if self._dirty:
                self._dirty = False
                self._evaluate()
            if self._deferred:
                self._task = self._runtime.spawn(self._deferred.pop(0))
                self._refresh_settled()
                continue
            # A scheduled re-evaluation counts as in flight even though the
            # state it will leave is settled: PENDING with a binding already
            # available is a state the fiber is about to leave, and resolving
            # there would report a dependency missing that is in plain sight.
            # `_evaluate_soon` clears `_evaluation` before it awaits this, so
            # the wait below is never on the task doing the waiting.
            evaluation = self._evaluation
            if evaluation is not None and not evaluation.done():
                await asyncio.shield(evaluation)
                continue
            task = self._task
            if task is not None and not task.done():
                await asyncio.shield(task)
                continue
            if self._state not in SETTLED:
                # The event is the wake-up, the state is the truth. Should the
                # two ever disagree, yield instead of re-checking immediately:
                # a tight loop over a set event never returns to the scheduler,
                # so a stuck fiber would freeze the whole loop rather than time
                # out in whoever is waiting for it.
                if self._settled.is_set():
                    await asyncio.sleep(0)
                else:
                    await self._settled.wait()
                continue
            return

    def _refresh_settled(self) -> None:
        if self._state in SETTLED and not self._deferred:
            self._settled.set()
        else:
            self._settled.clear()

    # -- state -------------------------------------------------------------

    def _transition(self, target: FiberState) -> None:
        check_transition(self._label, self._state, target)
        previous = self._state
        # Assigned before the announcement, so a listener reading `state` sees
        # the value it was just told about (SEM-005).
        self._state = target
        self._refresh_settled()
        self._runtime.announce(StatusChange(self, previous, target))


def _equal(left: object, right: object) -> bool:
    """Value equality, tolerant of types that refuse to be compared.

    Identity first so an unhashable, uncomparable object still counts as equal
    to itself; a comparison that raises or returns a non-boolean (an array, a
    query builder) is read as "not equal", which restarts. Restarting when the
    answer is unknown is the safe direction: the alternative is running with a
    config the caller believes was applied.
    """
    if left is right:
        return True
    with contextlib.suppress(Exception):
        return bool(left == right)
    return False


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    """Identity, element by element -- never equality.

    An implementation that compares equal to the one it replaced is still a
    different object, and the consumer holding the old one holds something that
    has been unloaded. Value equality here is how a stateless-looking service
    survives its own replacement (PROP-INJECT-003).
    """
    if len(left) != len(right):
        return False
    return all(one is other for one, other in zip(left, right, strict=True))


# --------------------------------------------------------------------------
# Dependency cycles (05 SEM-006)
# --------------------------------------------------------------------------


def _blocked_graph(fibers: Sequence[Fiber]) -> dict[Fiber, set[Fiber]]:
    """Who is waiting for whom, among the fibers that are waiting at all.

    Only *declared* provisions can appear here. A body that would bind a name
    when it ran declares nothing while it waits, and a graph built from what is
    already bound would be empty exactly when the answer matters.
    """
    supply: dict[str, list[Fiber]] = {}
    for fiber in fibers:
        for name in fiber.provides:
            supply.setdefault(name, []).append(fiber)

    graph: dict[Fiber, set[Fiber]] = {}
    for fiber in fibers:
        if fiber.state is not FiberState.PENDING:
            continue
        blockers = {
            supplier
            for name in fiber.missing
            for supplier in supply.get(name, ())
            if supplier is not fiber and supplier.state is FiberState.PENDING
        }
        if blockers:
            graph[fiber] = blockers
    # A blocker that is not itself blocked is a leaf: it has to be in the graph
    # for the sort to run, and it can never be part of a cycle.
    for blockers in list(graph.values()):
        for blocker in blockers:
            graph.setdefault(blocker, set())
    return graph


def _cycles(graph: dict[Fiber, set[Fiber]]) -> list[tuple[Fiber, ...]]:
    """Every cycle in ``graph``, found one at a time.

    ``TopologicalSorter`` reports a single cycle and stops, which is the right
    answer for a build tool and the wrong one for an application: two unrelated
    subsystems can each be misconfigured, and fixing the first only to be told
    about the second is a poor way to spend a morning. So the found cycle is
    cut and the sort is run again until it succeeds.
    """
    found: list[tuple[Fiber, ...]] = []
    remaining = {node: set(edges) for node, edges in graph.items()}
    while remaining:
        try:
            graphlib.TopologicalSorter(remaining).prepare()
        except graphlib.CycleError as exc:
            # args[1] is the cycle as a path, with its first node repeated.
            path: list[Fiber] = list(exc.args[1])[:-1]
            if not path:  # pragma: no cover -- graphlib does not do this
                break
            found.append(tuple(path))
            # Cut by dropping a member rather than an edge: which end of the
            # reported path the edge runs from is graphlib's business, and a
            # cut that misses would loop forever. The dropped node reappears
            # implicitly as a leaf, so a *second* cycle through the same fiber
            # is reported as one -- which reads correctly anyway, since it is
            # one fiber to fix.
            remaining.pop(path[0], None)
            continue
        break
    return found


def _provision_label(fiber: Fiber) -> str:
    """What to call a fiber in a cycle: the name it owes, or its label."""
    return fiber.provides[0] if fiber.provides else fiber.label
