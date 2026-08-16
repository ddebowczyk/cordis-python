"""Logging: structured records, attributed to their fiber, exported by plugins.

Implements ``spec/capabilities/12-logging.yaml``. The shipped exporters live in
:mod:`cordis.exporters`; nothing here imports them, and nothing here imports the
standard library's ``logging``.

A composable runtime needs a record channel that knows *which plugin* produced
each record, that works before any destination is configured, and whose
destinations come and go with the plugins that own them. That is three
properties the stdlib's global, imperatively-configured logger does not have.

The shape is small:

* :class:`Record` is what happened -- a sequence number, a time, a name, a
  level, an unformatted message with its arguments, the fiber that wrote it,
  and whatever extra the call site attached. Nothing is rendered until someone
  asks (SEM-004): a debug call in a hot path with no debug reader costs one
  integer comparison.
* :class:`LoggerService` is the channel. Exporters register on it *as effects*,
  so an exporter goes away when the plugin that mounted it does (SEM-006).
* :func:`logger` is how a plugin gets a writer: ``logger(ctx, "tools")``
  returns one bound to ``ctx``'s instance, and it keeps naming that instance
  wherever it is subsequently passed.

Records written before any exporter exists are held in a bounded buffer and
replayed to the first one that arrives (SEM-005). Upstream drops them, which
loses exactly the boot-time failures that are hardest to diagnose.
"""

from __future__ import annotations

import enum
import sys
import time
import traceback
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from cordis.plugin import config_of, fiber_of
from cordis.registry import Service

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis.context import Context
    from cordis.effect import EffectHandle, EffectScope

__all__ = [
    "DEFAULT_BUFFER",
    "DETACHED",
    "ExportFailure",
    "Exporter",
    "Level",
    "Logger",
    "LoggerService",
    "Record",
    "logger",
]

#: The fiber a logger names when it was made outside any mounted instance.
#: Spelled, rather than left empty, because "no instance" is a real answer and
#: an empty column reads as a bug in the exporter.
DETACHED: Final = "<detached>"

#: How many records are held while no exporter has ever registered.
DEFAULT_BUFFER: Final = 256

#: The service's own name, used for the records it writes about itself.
CHANNEL: Final = "logger"


class Level(enum.IntEnum):
    """Severity, with the standard library's numbers.

    Sharing the numbers means the bridge exporter passes a level straight
    through, and a threshold comparison is an integer comparison.
    """

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


#: A threshold no level clears: what the service uses once every exporter has
#: gone and there is nobody left to write for.
OFF: Final = int(Level.ERROR) + 1

_EMPTY: Final[Mapping[str, object]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Record:
    """One thing that happened, not yet turned into text."""

    seq: int
    ts: float
    name: str
    level: Level
    message: str
    args: tuple[object, ...] = ()
    fiber: str = DETACHED
    # A factory, because 3.11's dataclasses reject a `mappingproxy` default
    # as mutable. It returns the one shared empty proxy.
    extra: Mapping[str, object] = field(default_factory=lambda: _EMPTY)

    def render(self) -> str:
        """The message with its arguments substituted, stdlib ``%`` style.

        Called by whoever wants text, which is the whole of SEM-004: an
        argument whose ``__str__`` is expensive pays for itself once per
        exporter that actually reads it, and never for a record nobody gets.
        """
        return self.message % self.args if self.args else self.message


@runtime_checkable
class Exporter(Protocol):
    """Anything that can be handed a record. Structural: no base class."""

    def export(self, record: Record, /) -> None: ...


@dataclass(frozen=True)
class ExportFailure:
    """An exporter that raised. Contained, reported, never re-raised (SEM-003)."""

    exporter: str
    record: Record
    error: BaseException


@dataclass
class _Sink:
    """One registered exporter and the level it asked for."""

    exporter: Exporter
    level: int


class LoggerService(Service):
    """The record channel: sequence numbers, exporters, and the boot buffer.

    Provided like any other service (``root.plugin(LoggerService)``), which is
    what makes "where do logs go" a mounting decision rather than a global one.
    """

    name = "logger"

    def __init__(
        self,
        ctx: Context,
        *,
        buffer: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(ctx)
        self._sinks: list[_Sink] = []
        self._seq = 0
        self._threshold = int(Level.DEBUG)  # everything, until someone reads
        self._replayed = False
        self._buffer: deque[Record] = deque(maxlen=_bound(ctx, buffer))
        self._dropped = 0
        self._handlers: list[Callable[[ExportFailure], None]] = []
        self._now = clock if clock is not None else _clock_of(ctx)

    # -- writing -----------------------------------------------------------

    def __call__(self, name: str, /, *, fiber: str = DETACHED) -> Logger:
        """A writer under ``name``, attributed to ``fiber``.

        Reached as ``logger(ctx, name)`` by anything that has a context; the
        keyword is for callers that hold the service directly and know the
        attribution themselves.
        """
        return Logger(self, name, fiber)

    def write(
        self,
        level: Level,
        name: str,
        fiber: str,
        message: str,
        args: tuple[object, ...],
        extra: Mapping[str, object],
    ) -> None:
        """The single entry point. Below the threshold, nothing is built."""
        if level < self._threshold:
            return
        self._seq += 1
        self._dispatch(
            Record(
                seq=self._seq,
                ts=self._now(),
                name=name,
                level=level,
                message=message,
                args=args,
                fiber=fiber,
                extra=MappingProxyType(dict(extra)) if extra else _EMPTY,
            )
        )

    @property
    def sequence(self) -> int:
        """How many records exist so far; the last one assigned this number."""
        return self._seq

    @property
    def threshold(self) -> int:
        """The level below which a call returns without building anything."""
        return self._threshold

    @property
    def dropped(self) -> int:
        """Records the boot buffer had to discard, over the whole run."""
        return self._dropped

    # -- exporters ---------------------------------------------------------

    def add_exporter(
        self,
        exporter: Exporter,
        /,
        *,
        scope: EffectScope,
        level: Level | None = None,
    ) -> EffectHandle:
        """Deliver to ``exporter`` for the lifetime of ``scope``.

        ``level`` is the lowest severity it wants; omitted, the exporter's own
        ``level`` attribute is honoured, and failing that it gets everything.
        Registered as an effect, so disposing the scope stops delivery in the
        same breath as everything else the plugin owned (SEM-006).
        """
        sink = _Sink(exporter=exporter, level=_interest(exporter, level))

        def register() -> Callable[[], None]:
            self._sinks.append(sink)
            self._recompute()
            if not self._replayed:
                self._replay(sink)
            return lambda: self._remove(sink)

        return scope.effect(register, label=f"export:{_describe(exporter)}")

    def _remove(self, sink: _Sink) -> None:
        # Identity, not equality: two exporters that compare equal are still
        # two registrations, and a disposer must undo exactly its own.
        self._sinks = [held for held in self._sinks if held is not sink]
        self._recompute()

    def _recompute(self) -> None:
        """The threshold, after a registration or a disposal.

        With no exporter left the channel closes rather than reopening the boot
        buffer: SEM-005's window is the one before output was ever configured,
        and re-buffering during shutdown would turn teardown into a leak.
        """
        if self._sinks:
            self._threshold = min(sink.level for sink in self._sinks)
        else:
            self._threshold = OFF if self._replayed else int(Level.DEBUG)

    # -- delivery ----------------------------------------------------------

    def _dispatch(self, record: Record) -> None:
        if not self._sinks:
            self._park(record)
            return
        for sink in tuple(self._sinks):  # a disposal mid-delivery is allowed
            if record.level >= sink.level:
                self._give(sink, record)

    def _park(self, record: Record) -> None:
        """Hold a record until someone arrives to read it (SEM-005)."""
        if len(self._buffer) == self._buffer.maxlen:
            self._dropped += 1  # the deque evicts the oldest for us
        self._buffer.append(record)

    def _replay(self, sink: _Sink) -> None:
        """Hand the first exporter everything that happened before it."""
        self._replayed = True
        held = tuple(self._buffer)
        self._buffer.clear()
        for record in held:
            if record.level >= sink.level:
                self._give(sink, record)
        if self._dropped:
            # After the replay, not before it: this notice is built now, so its
            # sequence number is higher than every record it describes, and
            # delivering it first would hand an exporter a descending pair.
            self.write(
                Level.WARNING,
                CHANNEL,
                DETACHED,
                "%s earlier record(s) were dropped before any exporter registered",
                (self._dropped,),
                {"dropped": self._dropped},
            )

    def _give(self, sink: _Sink, record: Record) -> None:
        try:
            sink.exporter.export(record)
        except Exception as exc:  # SEM-003: containment is the whole point
            self._report(sink.exporter, record, exc)

    # -- the error channel -------------------------------------------------

    def on_error(self, handler: Callable[[ExportFailure], None]) -> Callable[[], None]:
        """Route contained exporter failures somewhere; returns the undo."""
        self._handlers.append(handler)

        def stop() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return stop

    def _report(self, exporter: Exporter, record: Record, error: BaseException) -> None:
        """Deliver a failure that must not reach the logging call site.

        With no handler installed the traceback goes to stderr. Silence is the
        one unacceptable option: the call site is deliberately not told, so the
        channel is the only place the failure can still be seen. Deliberately
        not logged through this service either -- an exporter that fails on
        every record would then be handed the report of its own failure.
        """
        failure = ExportFailure(
            exporter=_describe(exporter), record=record, error=error
        )
        if not self._handlers:
            traceback.print_exception(error, file=sys.stderr)
            return
        for handler in list(self._handlers):
            handler(failure)


class Logger:
    """A writer under one name, for one fiber.

    Attribution is captured here, when the logger is made, and never read again
    -- a logger handed to a callback keeps naming the instance that created it,
    which is the attribution a reader of the log is looking for.
    """

    __slots__ = ("_fiber", "_name", "_service")

    def __init__(self, service: LoggerService, name: str, fiber: str) -> None:
        self._service = service
        self._name = name
        self._fiber = fiber

    @property
    def name(self) -> str:
        return self._name

    @property
    def fiber(self) -> str:
        return self._fiber

    def log(
        self, level: Level, message: str, /, *args: object, **extra: object
    ) -> None:
        """Write at ``level``. ``message`` is a ``%`` template, rendered later."""
        self._service.write(level, self._name, self._fiber, message, args, extra)

    def debug(self, message: str, /, *args: object, **extra: object) -> None:
        self.log(Level.DEBUG, message, *args, **extra)

    def info(self, message: str, /, *args: object, **extra: object) -> None:
        self.log(Level.INFO, message, *args, **extra)

    def warning(self, message: str, /, *args: object, **extra: object) -> None:
        self.log(Level.WARNING, message, *args, **extra)

    def error(self, message: str, /, *args: object, **extra: object) -> None:
        self.log(Level.ERROR, message, *args, **extra)

    def __repr__(self) -> str:
        return f"<Logger {self._name!r} of {self._fiber}>"


def logger(ctx: Context, name: str, /) -> Logger:
    """A logger under ``name``, attributed to the instance ``ctx`` belongs to.

    A module-level reader taking a context, like ``scope_of`` and ``config_of``
    before it. Upstream spells this ``ctx.logger(name)``, and ``ctx.logger``
    does resolve the service here -- but attribute resolution returns one
    shared object, which cannot know who is asking, and knowing who is asking
    is the entire point (SEM-001).
    """
    service = ctx.require(LoggerService)
    return service(name, fiber=fiber_label(ctx))


def fiber_label(ctx: Context) -> str:
    """The name of the instance ``ctx`` belongs to, or :data:`DETACHED`."""
    owner = fiber_of(ctx)
    return DETACHED if owner is None else owner.label


# --------------------------------------------------------------------------
# Small decisions, kept out of the class
# --------------------------------------------------------------------------


def _interest(exporter: Exporter, level: Level | None) -> int:
    """The lowest level ``exporter`` wants: the keyword, its own, or all of it."""
    if level is not None:
        return int(level)
    declared = getattr(exporter, "level", None)
    return int(declared) if isinstance(declared, int) else int(Level.DEBUG)


def _bound(ctx: Context, buffer: int | None) -> int:
    """How many records to hold before the first exporter arrives."""
    if buffer is not None:
        return buffer
    config = config_of(ctx)
    if isinstance(config, Mapping):
        found = config.get("buffer")
        if isinstance(found, int) and not isinstance(found, bool):
            return found
    return DEFAULT_BUFFER


def _clock_of(ctx: Context) -> Callable[[], float]:
    """A bound ``clock`` service's ``now``, or the wall clock.

    Read structurally rather than by importing the scheduling capability: a
    logger that pulled in the scheduler to ask the time would make the
    dependency run the wrong way, and a test that wants deterministic
    timestamps only has to provide a clock.
    """
    found = ctx.get("clock")
    now = getattr(found, "now", None)
    return now if callable(now) else time.time


def _describe(target: object) -> str:
    """A name for an exporter, for labels and failure reports."""
    return type(target).__qualname__
