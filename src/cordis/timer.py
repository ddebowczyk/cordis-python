"""Scheduling: time-based work whose lifetime is its scope's.

Implements ``spec/capabilities/13-scheduling.yaml``.

Background work is where lifetime discipline breaks first. A plugin that starts
a polling loop and then unloads leaves the loop calling into services that no
longer exist; asyncio's own habit of holding tasks weakly makes the failure both
common and silent. Every helper here registers as an effect, so the answer to
"when does this stop" is always the same as for everything else the plugin did:
when its scope is disposed.

Five helpers, all module-level functions taking a context:

* :func:`spawn` runs a coroutine, holds it strongly, and on disposal cancels
  *and awaits* it -- disposal does not return while cleanup is still running.
* :func:`timeout` and :func:`interval` are :func:`spawn` with a body that
  sleeps, so there is one cancellation path rather than three.
* :func:`throttle` and :func:`debounce` wrap a callable. Between them they own
  at most one task and exactly one effect, however often the wrapper is called.

Time comes from a :class:`Clock` -- the bound ``clock`` service if there is one,
:class:`SystemClock` otherwise (SEM-007). Nothing here calls a wall clock
directly, which is what lets the properties drive time instead of sleeping.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
import traceback
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    ParamSpec,
    Protocol,
    TypeAlias,
    runtime_checkable,
)

from cordis.logging import LoggerService, fiber_label
from cordis.plugin import scope_of

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from cordis.context import Context
    from cordis.effect import EffectHandle, EffectScope

__all__ = [
    "Clock",
    "Report",
    "Schedule",
    "SystemClock",
    "TimerFailure",
    "clock_of",
    "debounce",
    "interval",
    "spawn",
    "throttle",
    "timeout",
]


@runtime_checkable
class Clock(Protocol):
    """Time, as scheduling needs it: reading it and waiting on it."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float, /) -> None: ...


class SystemClock:
    """Real time. Monotonic, because a schedule must not follow a clock change."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float, /) -> None:
        await asyncio.sleep(seconds)

    def __repr__(self) -> str:
        return "<SystemClock>"


#: The clock used when nothing is bound, so a caller that does not care about
#: time never has to mount one.
SYSTEM: Final = SystemClock()


def clock_of(ctx: Context) -> Clock:
    """The ``clock`` service ``ctx`` resolves, or the system clock.

    Structural rather than nominal: the tests' virtual clock satisfies
    :class:`Clock` without importing it, which is the point of making time
    injectable in the first place.
    """
    found = ctx.get("clock")
    return found if isinstance(found, Clock) else SYSTEM


@dataclass(frozen=True)
class TimerFailure:
    """A scheduled callable that raised. Contained, reported, never propagated."""

    label: str
    error: BaseException


Report: TypeAlias = "Callable[[TimerFailure], None]"

#: The wrapped callable's own parameters, preserved through `throttle` and
#: `debounce` so wrapping does not erase argument types. Spelled the 3.11 way,
#: because the package supports 3.11 and PEP 695 syntax does not.
P = ParamSpec("P")


# --------------------------------------------------------------------------
# Spawning
# --------------------------------------------------------------------------


def spawn(
    ctx: Context,
    coro: Coroutine[Any, Any, object],
    /,
    *,
    label: str | None = None,
    on_error: Report | None = None,
) -> EffectHandle:
    """Run ``coro`` as a task owned by ``ctx``'s scope.

    The task is held by the disposer closure recorded on the scope, which is
    both halves of SEM-003 in one object: a strong reference for as long as the
    effect lives, and a disposer that cancels *and awaits* it. Awaiting is what
    makes disposal mean "the task has stopped" rather than "the task has been
    asked to stop".
    """
    scope = scope_of(ctx)
    name = label or getattr(coro, "__name__", None) or "spawn"

    def start() -> Callable[[], Awaitable[None]]:
        task = asyncio.get_running_loop().create_task(
            _guard(ctx, coro, name, on_error), name=name
        )

        async def stop() -> None:
            task.cancel()
            # return_exceptions keeps a task that failed on its way out from
            # replacing the teardown's own outcome with its last exception.
            await asyncio.gather(task, return_exceptions=True)

        return stop

    return scope.effect(start, label=f"spawn:{name}")


async def _guard(
    ctx: Context,
    coro: Coroutine[Any, Any, object],
    label: str,
    on_error: Report | None,
) -> None:
    """Run ``coro``, containing anything but cancellation (SEM-004)."""
    try:
        await coro
    except asyncio.CancelledError:
        raise  # teardown asked; it is not a failure and must not be reported
    except Exception as exc:
        _report(ctx, label, exc, on_error)


# --------------------------------------------------------------------------
# Timers
# --------------------------------------------------------------------------


def timeout(
    ctx: Context,
    delay: float,
    fn: Callable[[], object],
    /,
    *,
    on_error: Report | None = None,
) -> EffectHandle:
    """Call ``fn`` once, ``delay`` from now, unless the scope goes first."""
    clock = clock_of(ctx)

    async def once() -> None:
        await clock.sleep(delay)
        await _settle(fn())

    return spawn(ctx, once(), label=f"timeout:{delay}", on_error=on_error)


@dataclass
class _Counts:
    completed: int = 0
    skipped: int = 0


class Schedule:
    """A repeating schedule: the disposer, plus what it actually did.

    SEM-005 requires skipped iterations to be *counted*, and a count nobody can
    read is not a count -- so the handle carries them. Disposing is the same
    call as for any other effect, because it is the same handle.
    """

    __slots__ = ("_counts", "_handle")

    def __init__(self, handle: EffectHandle, counts: _Counts) -> None:
        self._handle = handle
        self._counts = counts

    @property
    def completed(self) -> int:
        """Iterations that ran, whether or not the callback raised."""
        return self._counts.completed

    @property
    def skipped(self) -> int:
        """Deadlines that passed while the previous iteration was still running."""
        return self._counts.skipped

    @property
    def handle(self) -> EffectHandle:
        """The effect this schedule is registered as."""
        return self._handle

    def __call__(self) -> Awaitable[None] | None:
        return self._handle()

    def __repr__(self) -> str:
        return f"<Schedule completed={self.completed} skipped={self.skipped}>"


def interval(
    ctx: Context,
    period: float,
    fn: Callable[[], object],
    /,
    *,
    on_error: Report | None = None,
) -> Schedule:
    """Call ``fn`` every ``period``, on a grid, without ever overlapping itself.

    Deadlines sit on a fixed grid from the moment the schedule starts, so a slow
    iteration does not push the cadence out. A deadline that arrives while the
    previous iteration is *still running* is skipped and counted (SEM-005) --
    an iteration that finished exactly on the deadline is not late, and runs.
    """
    if period <= 0:
        msg = f"interval period must be positive, not {period!r}"
        raise ValueError(msg)
    clock = clock_of(ctx)
    counts = _Counts()
    label = f"interval:{period}"

    async def repeat() -> None:
        start = clock.now()
        tick = 0
        while True:
            tick += 1
            deadline = start + tick * period
            now = clock.now()
            if now > deadline:
                # The previous iteration was still running when this deadline
                # arrived. Nothing is awaited here: no time passes while we
                # count the deadlines that already went by.
                counts.skipped += 1
                continue
            await clock.sleep(deadline - now)
            try:
                await _settle(fn())
            except Exception as exc:  # SEM-004: one bad poll is not the end
                _report(ctx, label, exc, on_error)
            counts.completed += 1

    return Schedule(spawn(ctx, repeat(), label=label, on_error=on_error), counts)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


@dataclass
class _Pending:
    """The single task a wrapper owns, and whatever call is waiting on it."""

    task: asyncio.Task[None] | None = None
    call: tuple[tuple[Any, ...], dict[str, Any]] | None = None


def throttle(
    ctx: Context,
    period: float,
    fn: Callable[P, object],
    /,
    *,
    on_error: Report | None = None,
) -> Callable[P, None]:
    """Call ``fn`` at most once per ``period``, with that window's last arguments.

    The first call opens a window and the invocation happens at the *end* of it,
    carrying whatever arguments arrived last -- which is the only way to honour
    both halves of SEM-006 at once, since a leading-edge call fires before the
    most recent arguments exist.
    """
    if period <= 0:
        msg = f"throttle period must be positive, not {period!r}"
        raise ValueError(msg)
    clock = clock_of(ctx)
    state = _Pending()
    label = f"throttle:{period}"

    async def close_window() -> None:
        await clock.sleep(period)
        waiting = state.call
        state.call = None
        state.task = None  # a call arriving now opens the next window
        if waiting is not None:
            args, kwargs = waiting
            await _run(ctx, fn, args, kwargs, label, on_error)

    def call(*args: P.args, **kwargs: P.kwargs) -> None:
        state.call = (args, kwargs)
        if state.task is None:
            state.task = _start(close_window(), label)

    _register(scope_of(ctx), state, label)
    return call


def debounce(
    ctx: Context,
    delay: float,
    fn: Callable[P, object],
    /,
    *,
    on_error: Report | None = None,
) -> Callable[P, None]:
    """Call ``fn`` once, ``delay`` after the last call in a burst.

    Each call cancels the pending one, so a burst costs one invocation and the
    arguments are the burst's last -- the difference from :func:`throttle`,
    which fires once per window however long the burst runs.
    """
    if delay <= 0:
        msg = f"debounce delay must be positive, not {delay!r}"
        raise ValueError(msg)
    clock = clock_of(ctx)
    state = _Pending()
    label = f"debounce:{delay}"

    async def settle(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        await clock.sleep(delay)
        state.task = None
        await _run(ctx, fn, args, kwargs, label, on_error)

    def call(*args: P.args, **kwargs: P.kwargs) -> None:
        pending = state.task
        if pending is not None:
            pending.cancel()
        state.task = _start(settle(args, kwargs), label)

    _register(scope_of(ctx), state, label)
    return call


def _register(scope: EffectScope, state: _Pending, label: str) -> None:
    """One effect per wrapper, not one per call.

    A wrapper called a thousand times must not leave a thousand records on the
    scope; the wrapper owns at most one task, so one disposer covers all of it.
    """

    def start() -> Callable[[], Awaitable[None]]:
        async def stop() -> None:
            task = state.task
            state.task = None
            state.call = None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return stop

    scope.effect(start, label=label)


def _start(coro: Coroutine[Any, Any, None], label: str) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(coro, name=label)


async def _run(
    ctx: Context,
    fn: Callable[..., object],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    label: str,
    on_error: Report | None,
) -> None:
    """Invoke a wrapped callable, containing whatever it raises (SEM-004)."""
    try:
        await _settle(fn(*args, **kwargs))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _report(ctx, label, exc, on_error)


# --------------------------------------------------------------------------
# The bits every helper shares
# --------------------------------------------------------------------------


async def _settle(result: object) -> None:
    """Await ``result`` if it is awaitable, so a callback may be either."""
    if inspect.isawaitable(result):
        await result


def _report(
    ctx: Context, label: str, error: BaseException, on_error: Report | None
) -> None:
    """Report a contained failure, in descending order of specificity.

    The call site's own handler if it passed one; otherwise the log, which is
    where an operator is already looking; otherwise stderr, because silence is
    the one unacceptable answer for a failure the caller was never told about.
    """
    if on_error is not None:
        on_error(TimerFailure(label=label, error=error))
        return
    service = ctx.get(LoggerService)
    if service is not None:
        service("timer", fiber=fiber_label(ctx)).error(
            "%s raised %r", label, error, label=label
        )
        return
    traceback.print_exception(error, file=sys.stderr)
