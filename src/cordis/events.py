"""The event bus: five dispatch modes, each an explicit contract.

Implements ``spec/capabilities/06-event-bus.yaml``.

The mode is a property of the *event*, not of the call site. An event is a
module-level object that carries both its wire name and, through its type, the
signature its listeners must have::

    TOOL_EXECUTE: Waterfall[[ToolCall], ToolResult] = Waterfall("tools/execute")
    CONFIG_CHANGED: Emit[[str]] = Emit("config/changed")

A listener author reading that declaration knows whether they may return a
value, run concurrently, veto, or wrap the operation -- which is the whole
point of having five modes instead of one ``emit`` with five conventions
layered on top (SEM-001).

The dispatch rules, in one place:

``emit``       every listener runs, return values are discarded, and one
               listener's failure never stops another's delivery. Failures go
               to the error channel, never to the caller (SEM-004).
``parallel``   listeners run concurrently; dispatch waits for all of them and
               raises one ``ExceptionGroup`` at the end (SEM-005).
``serial``     listeners run in order until one returns something that is not
               ``None``; that value is the result (SEM-006).
``bail``       ``serial`` without awaiting, for synchronous dispatch sites.
``waterfall``  each listener wraps the rest of the chain through ``next``
               (SEM-007), may decline to call it (SEM-008), and may not call
               it twice (SEM-009).

Two structural choices carry most of the safety. Listener lists are immutable
tuples replaced on mutation, so a dispatch holding one has a snapshot and
registration or disposal from inside a listener cannot make the loop skip
anyone (SEM-010). And ``next`` is a fresh closure per invocation with its own
guard, so the once-only rule is enforced per call rather than per chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import traceback
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Concatenate,
    Generic,
    ParamSpec,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

from cordis.errors import EventModeError, NextCalledTwiceError
from cordis.filter import filter_of

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cordis.context import Context
    from cordis.effect import EffectHandle, EffectScope
    from cordis.filter import Filter

__all__ = [
    "Bail",
    "BoundBus",
    "Emit",
    "ErrorReport",
    "Event",
    "EventBus",
    "Next",
    "Parallel",
    "Serial",
    "Waterfall",
]

P = ParamSpec("P")
R = TypeVar("R")

#: The continuation handed to a waterfall listener: call it to run the rest of
#: the chain and get its result.
Next: TypeAlias = "Callable[[], Awaitable[R]]"


class Event:
    """The declaration of an extension point.

    Subclasses differ only in the mode they declare and the listener signature
    their type expresses. Identity is the object: two events with the same name
    are two events, and the name exists for configuration and diagnostics.
    """

    __slots__ = ("name",)

    mode: ClassVar[str]

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"


class Emit(Event, Generic[P]):
    """Announce something. Listeners return nothing and never block a caller."""

    __slots__ = ()
    mode: ClassVar[str] = "emit"


class Parallel(Event, Generic[P]):
    """Announce something to listeners that run concurrently and are awaited."""

    __slots__ = ()
    mode: ClassVar[str] = "parallel"


class Serial(Event, Generic[P, R]):
    """Ask, in order, until someone answers. ``None`` abstains."""

    __slots__ = ()
    mode: ClassVar[str] = "serial"


class Bail(Event, Generic[P, R]):
    """``Serial`` for synchronous dispatch sites: listeners are not awaited."""

    __slots__ = ()
    mode: ClassVar[str] = "bail"


class Waterfall(Event, Generic[P, R]):
    """Wrap the operation. Each listener receives ``next`` and the arguments."""

    __slots__ = ()
    mode: ClassVar[str] = "waterfall"


@dataclass(frozen=True)
class ErrorReport:
    """A listener failure that must not reach the dispatching caller."""

    event: str
    listener: str
    error: BaseException


ErrorHandler: TypeAlias = "Callable[[ErrorReport], None]"


@dataclass(frozen=True)
class _Entry:
    """One registration. Identity, not equality, decides what a disposer removes."""

    listener: Callable[..., Any]
    prepend: bool
    #: The context this registration was made under -- whose filter decides
    #: admission (event-filtering SEM-001). ``None`` means no filter, ever.
    ctx: Context | None = None
    #: Set at the registration site by a listener that must see everything:
    #: recorders, exporters, the session log (SEM-003).
    global_: bool = False


@dataclass
class _Channel:
    """The listeners for one event name, split by precedence.

    Two tuples rather than one list because SEM-003's ordering rule is a fact
    about which group a listener is in, and keeping the groups apart means
    inserting never has to reason about where the boundary currently is.
    """

    prepended: tuple[_Entry, ...] = ()
    normal: tuple[_Entry, ...] = ()

    def snapshot(self) -> tuple[_Entry, ...]:
        """Invocation order: prepended newest-first, then normal oldest-first."""
        return self.prepended + self.normal


class EventBus:
    """Registration, ordering, and the five dispatchers.

    The bus owns no lifetime of its own: every registration is an effect on the
    scope the registrant passed in, so a listener cannot outlive the plugin
    that added it (SEM-002).
    """

    __slots__ = ("_channels", "_error_handlers")

    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._error_handlers: list[ErrorHandler] = []

    # -- registration ------------------------------------------------------

    @overload
    def on(
        self,
        event: Emit[P],
        listener: Callable[P, Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Parallel[P],
        listener: Callable[P, Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Serial[P, R],
        listener: Callable[P, Awaitable[R | None] | R | None],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Bail[P, R],
        listener: Callable[P, R | None],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Waterfall[P, R],
        listener: Callable[Concatenate[Next[R], P], Awaitable[R] | R],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle: ...

    def on(
        self,
        event: Event,
        listener: Callable[..., Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        ctx: Context | None = None,
        global_: bool = False,
    ) -> EffectHandle:
        """Register ``listener`` for the lifetime of ``scope``.

        ``prepend`` puts the listener ahead of every non-prepended one, and
        ahead of earlier prepended ones: "get in front of everyone" has to keep
        meaning that when a second plugin asks for the same thing (SEM-003).

        ``ctx`` is the registration context, and its filter decides which
        dispatches reach this listener (event-filtering SEM-001); ``global_``
        opts out of that decision entirely, which is what a recorder needs and
        what its registration site should say out loud. Both default to the
        pre-filtering behaviour, so a bus used without contexts is unchanged.
        Usually neither is passed by hand: ``bus.through(ctx).on(...)`` is the
        spelling applications reach for.
        """
        entry = _Entry(listener=listener, prepend=prepend, ctx=ctx, global_=global_)

        def register() -> Callable[[], None]:
            channel = self._channels.setdefault(event.name, _Channel())
            if prepend:
                channel.prepended = (entry, *channel.prepended)
            else:
                channel.normal = (*channel.normal, entry)
            return lambda: self._remove(event.name, entry)

        return scope.effect(register, label=f"on:{event.name}")

    def _remove(self, name: str, entry: _Entry) -> None:
        channel = self._channels.get(name)
        if channel is None:
            return
        channel.prepended = tuple(e for e in channel.prepended if e is not entry)
        channel.normal = tuple(e for e in channel.normal if e is not entry)

    def listeners(self, event: Event) -> tuple[Callable[..., Any], ...]:
        """The live listeners for ``event``, in invocation order."""
        return tuple(entry.listener for entry in self._snapshot(event))

    def _snapshot(self, event: Event) -> tuple[_Entry, ...]:
        """The listener list this dispatch will use, fixed for its duration."""
        channel = self._channels.get(event.name)
        return () if channel is None else channel.snapshot()

    # -- admission ---------------------------------------------------------

    def _admitted(self, event: Event, carrier: Context | None) -> tuple[_Entry, ...]:
        """The listeners this dispatch reaches, decided once (SEM-002).

        Computed from the snapshot the dispatch already froze, so a
        registration arriving mid-dispatch is neither admitted nor
        half-admitted -- the same rule the listener list itself follows.

        A dispatch with no carrier admits everyone and asks nothing: absence of
        something to filter on is not a denial, exactly as absence of a filter
        is not (see the record's deviations).
        """
        entries = self._snapshot(event)
        if carrier is None:
            return entries
        verdicts: dict[int, bool] = {}
        alive: list[Filter] = []  # keeps every keyed predicate from being recycled
        admitted: list[_Entry] = []
        for entry in entries:
            if entry.global_ or entry.ctx is None:
                admitted.append(entry)
                continue
            predicate = filter_of(entry.ctx)
            if predicate is None:
                admitted.append(entry)
                continue
            key = id(predicate)
            verdict = verdicts.get(key)
            if verdict is None:
                alive.append(predicate)
                verdict = self._ask(event, predicate, carrier)
                verdicts[key] = verdict
            if verdict:
                admitted.append(entry)
        return tuple(admitted)

    def _ask(self, event: Event, predicate: Filter, carrier: Context) -> bool:
        """Put the question to one filter; a failure is a denial (SEM-005).

        Reported once per evaluation rather than once per denied listener: the
        evaluation is what happened, and a bad predicate under a hundred
        listeners would otherwise arrive as a hundred identical failures.
        """
        try:
            return bool(predicate(carrier))
        except Exception as exc:
            self._report_failure(event, _describe(predicate), exc)
            return False

    # -- the error channel -------------------------------------------------

    def on_error(self, handler: ErrorHandler) -> Callable[[], None]:
        """Route contained listener failures somewhere; returns the undo."""
        self._error_handlers.append(handler)

        def stop() -> None:
            if handler in self._error_handlers:
                self._error_handlers.remove(handler)

        return stop

    def _report(self, event: Event, entry: _Entry, error: BaseException) -> None:
        """Deliver a failure that must not reach the caller.

        With no handler installed the traceback goes to stderr. Silence would
        be the one unacceptable option: ``emit`` deliberately withholds these
        failures from the dispatching caller, so the channel is the only place
        they can still be seen.
        """
        self._report_failure(event, _describe(entry.listener), error)

    def _report_failure(self, event: Event, source: str, error: BaseException) -> None:
        """The channel itself. ``source`` is whatever failed -- a listener, or
        the filter that was deciding whether to run one."""
        report = ErrorReport(event=event.name, listener=source, error=error)
        if not self._error_handlers:
            traceback.print_exception(error, file=sys.stderr)
            return
        for handler in list(self._error_handlers):
            handler(report)

    # -- dispatch ----------------------------------------------------------

    async def emit(self, event: Emit[P], /, *args: P.args, **kwargs: P.kwargs) -> None:
        """Tell everyone. Return values are discarded and failures contained.

        Every listener runs even if earlier ones raised: a broadcast whose
        delivery depends on every previous listener succeeding is not a
        broadcast (SEM-004).
        """
        await self._emit(None, event, args, kwargs)

    async def _emit(
        self,
        carrier: Context | None,
        event: Emit[P],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        _require_mode(event, Emit, "emit")
        for entry in self._admitted(event, carrier):
            try:
                await _settle(entry.listener(*args, **kwargs))
            except Exception as exc:
                self._report(event, entry, exc)

    async def parallel(
        self, event: Parallel[P], /, *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """Run every listener concurrently and wait for all of them.

        Failures are collected rather than raced: the first exception must not
        decide whether the other listeners finish (SEM-005).
        """
        await self._parallel(None, event, args, kwargs)

    async def _parallel(
        self,
        carrier: Context | None,
        event: Parallel[P],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        _require_mode(event, Parallel, "parallel")
        entries = self._admitted(event, carrier)
        if not entries:
            return

        async def run(entry: _Entry) -> None:
            await _settle(entry.listener(*args, **kwargs))

        settled = await asyncio.gather(
            *(run(entry) for entry in entries), return_exceptions=True
        )
        errors = [item for item in settled if isinstance(item, Exception)]
        if errors:
            raise ExceptionGroup(f"errors while dispatching {event.name!r}", errors)

    async def serial(
        self, event: Serial[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R | None:
        """Ask each listener in turn; the first non-``None`` answer wins.

        ``None`` is the only abstention. ``False``, ``0`` and ``""`` are
        answers, which is the difference between "deny" and "no opinion"
        (SEM-006).
        """
        return await self._serial(None, event, args, kwargs)

    async def _serial(
        self,
        carrier: Context | None,
        event: Serial[P, R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R | None:
        _require_mode(event, Serial, "serial")
        for entry in self._admitted(event, carrier):
            answer = await _settle(entry.listener(*args, **kwargs))
            if answer is not None:
                return cast("R", answer)
        return None

    def bail(self, event: Bail[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R | None:
        """``serial`` for synchronous dispatch sites: nothing is awaited."""
        return self._bail(None, event, args, kwargs)

    def _bail(
        self,
        carrier: Context | None,
        event: Bail[P, R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R | None:
        _require_mode(event, Bail, "bail")
        for entry in self._admitted(event, carrier):
            answer = entry.listener(*args, **kwargs)
            if inspect.isawaitable(answer):
                _discard(answer)
                raise EventModeError(
                    event.name, "bail", "serial (the listener returned an awaitable)"
                )
            if answer is not None:
                return cast("R", answer)
        return None

    async def waterfall(
        self,
        event: Waterfall[P, R],
        default: Callable[[], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run the listeners as nested wrappers around ``default``.

        The chain is built from the inside out, so the first listener is the
        outermost wrapper -- ``next()`` reaches the ones registered after it,
        and the innermost ``next()`` reaches ``default`` (SEM-007).

        ``default`` is positional-only and precedes the event arguments: a
        keyword-only parameter sitting between ``*args: P.args`` and
        ``**kwargs: P.kwargs`` is not expressible, so the spec's original
        signature does not type-check.
        """
        return await self._waterfall(None, event, default, args, kwargs)

    async def _waterfall(
        self,
        carrier: Context | None,
        event: Waterfall[P, R],
        default: Callable[[], R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R:
        _require_mode(event, Waterfall, "waterfall")

        async def terminal() -> R:
            return default()

        chain: Callable[[], Awaitable[R]] = terminal
        # Built from the admitted subset, not filtered afterwards: a denied
        # listener occupying a link would break the chain rather than leave it
        # (event-filtering SEM-006).
        for entry in reversed(self._admitted(event, carrier)):
            chain = self._link(event, entry, chain, args, kwargs)
        return await chain()

    def _link(
        self,
        event: Waterfall[P, R],
        entry: _Entry,
        downstream: Callable[[], Awaitable[R]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Callable[[], Awaitable[R]]:
        """Wrap ``downstream`` in one listener, with a fresh ``next`` guard.

        The guard is per invocation, not per chain: the same chain dispatched
        twice must give each dispatch its own single use of ``next``, and a
        flag on the entry would let the first dispatch spend the second's.
        """

        async def step() -> R:
            entered = False

            async def advance() -> R:
                nonlocal entered
                if entered:
                    raise NextCalledTwiceError(event.name, _describe(entry.listener))
                entered = True  # set before descending: a second call must be
                return await downstream()  # rejected, not merely detected after

            result = entry.listener(advance, *args, **kwargs)
            return cast("R", await _settle(result))

        return step

    # -- the bound view ----------------------------------------------------

    def through(self, ctx: Context, /) -> BoundBus:
        """The bus as seen from ``ctx``: registration context and carrier at once.

        This is what ``ctx.on(...)`` and ``ctx.emit(...)`` are upstream. One
        context plays both roles because in practice they are the same role --
        a plugin registers listeners for its own subject and dispatches on
        behalf of it.
        """
        return BoundBus(self, ctx)


class BoundBus:
    """An :class:`EventBus` viewed through one context.

    Registrations made here carry that context, so its filter decides which
    dispatches reach them; dispatches made here carry it as the carrier, which
    is what those filters are asked about.

    The five dispatchers repeat the bus's signatures rather than forwarding
    through ``*args: Any``: this is the form applications call, and a facade
    that erased the per-mode listener typing would make the typed path the one
    nobody uses.
    """

    __slots__ = ("_bus", "_ctx")

    def __init__(self, bus: EventBus, ctx: Context) -> None:
        self._bus = bus
        self._ctx = ctx

    @property
    def bus(self) -> EventBus:
        """The bus underneath, for a caller that needs the unbound form."""
        return self._bus

    @property
    def context(self) -> Context:
        """The context this view registers and dispatches through."""
        return self._ctx

    @overload
    def on(
        self,
        event: Emit[P],
        listener: Callable[P, Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Parallel[P],
        listener: Callable[P, Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Serial[P, R],
        listener: Callable[P, Awaitable[R | None] | R | None],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Bail[P, R],
        listener: Callable[P, R | None],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        event: Waterfall[P, R],
        listener: Callable[Concatenate[Next[R], P], Awaitable[R] | R],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle: ...

    def on(
        self,
        event: Event,
        listener: Callable[..., Any],
        /,
        *,
        scope: EffectScope,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle:
        """Register ``listener`` under this context's filter.

        Typed by the five overloads above; the forwarding call goes to the
        bus's implementation rather than through its overloads, which cannot
        resolve a `(Event, Callable[..., Any])` pair that the caller already
        matched here.
        """
        register: Any = self._bus.on
        handle: EffectHandle = register(
            event,
            listener,
            scope=scope,
            prepend=prepend,
            ctx=self._ctx,
            global_=global_,
        )
        return handle

    async def emit(self, event: Emit[P], /, *args: P.args, **kwargs: P.kwargs) -> None:
        """:meth:`EventBus.emit`, carried by this context."""
        await self._bus._emit(self._ctx, event, args, kwargs)  # noqa: SLF001

    async def parallel(
        self, event: Parallel[P], /, *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """:meth:`EventBus.parallel`, carried by this context."""
        await self._bus._parallel(self._ctx, event, args, kwargs)  # noqa: SLF001

    async def serial(
        self, event: Serial[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R | None:
        """:meth:`EventBus.serial`, carried by this context."""
        return await self._bus._serial(self._ctx, event, args, kwargs)  # noqa: SLF001

    def bail(self, event: Bail[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R | None:
        """:meth:`EventBus.bail`, carried by this context."""
        return self._bus._bail(self._ctx, event, args, kwargs)  # noqa: SLF001

    async def waterfall(
        self,
        event: Waterfall[P, R],
        default: Callable[[], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """:meth:`EventBus.waterfall`, carried by this context."""
        return await self._bus._waterfall(  # noqa: SLF001
            self._ctx, event, default, args, kwargs
        )


def _require_mode(event: Event, expected: type[Event], attempted: str) -> None:
    """Reject a dispatch through the wrong mode before any listener runs."""
    if not isinstance(event, expected):
        raise EventModeError(event.name, event.mode, attempted)


async def _settle(result: object) -> object:
    """Await ``result`` if it is awaitable, otherwise pass it through."""
    if inspect.isawaitable(result):
        return await result
    return result


def _discard(awaitable: object) -> None:
    """Close a coroutine that will never be awaited, so it warns about nothing."""
    closer = getattr(awaitable, "close", None)
    if callable(closer):
        with contextlib.suppress(RuntimeError):
            closer()


def _describe(listener: Callable[..., Any]) -> str:
    return getattr(listener, "__qualname__", None) or repr(listener)
