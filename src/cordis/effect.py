"""Effect scopes: every registration returns its own undo.

A scope accepts an *effect* -- a zero-argument function that acquires
something -- in any of the shapes Python uses for resources, records the
disposer it produces, and guarantees that disposing the scope runs every
recorded disposer exactly once, in reverse order, even when setup or teardown
raises.

Upstream fuses this into ``Fiber``. Here it is a standalone primitive: it has
no idea what a plugin is, which is why its properties can be stated and tested
without one, and why ``Fiber`` will compose it rather than reimplement it.

Three things are worth knowing before reading the code.

**Registration is atomic.** If an effect raises partway through setup, the
disposers it had already produced are run and nothing of it stays recorded
(SEM-004). The scope is never left holding half an acquisition.

**Teardown is total.** A disposer that raises does not stop the ones behind it;
every failure is collected and re-raised together as an ``ExceptionGroup``
(SEM-005). Cancellation does not stop it either -- the unwind runs to
completion and the ``CancelledError`` is re-raised afterwards.

**Async setup is deferred, not hidden.** ``effect()`` is synchronous because it
is called from synchronous plugin bodies, so an effect whose setup is
asynchronous returns an :class:`EffectHandle` that is *awaitable*: awaiting it
surfaces the setup result or its exception. This is the deliberate reading of
SEM-001's "an awaitable of either" -- upstream returns a value that is both a
handle and a promise, and this is that, spelled in Python. The handle is also
the disposer, so a caller that does not need the completion can ignore it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias, TypeGuard

from cordis.errors import InactiveScopeError, InvalidEffectError

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Generator,
        Iterable,
        Iterator,
        Sequence,
    )

__all__ = [
    "CAPTURE_LOCATIONS",
    "Disposer",
    "EffectFn",
    "EffectHandle",
    "EffectNode",
    "EffectScope",
]

#: What a disposer looks like: no arguments, and either it undoes the thing now
#: or it returns something to await.
Disposer: TypeAlias = "Callable[[], Awaitable[None] | None]"

#: What an effect looks like: no arguments, returning one of the accepted
#: shapes. Typed as ``object`` because the acceptable set is a union of six
#: unrelated protocols; the shape check at registration is what narrows it, and
#: it produces a precise error rather than a silent coercion.
EffectFn: TypeAlias = "Callable[[], object]"

#: Whether to record the source location of each registration. On by default;
#: turning it off removes the only per-registration cost without changing any
#: other behaviour (diagnostics SEM-005).
CAPTURE_LOCATIONS = True

_NO_LOCATION = "<unknown>"


def _capture_location(depth: int) -> str:
    if not CAPTURE_LOCATIONS:
        return _NO_LOCATION
    frame = sys._getframe(depth)  # noqa: SLF001 -- the documented way to do this
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


def _is_async_disposer(disposer: Disposer) -> bool:
    """Whether calling this disposer is known in advance to produce an awaitable.

    Decides batching, and deliberately conservative: a plain function that
    happens to return a coroutine is treated as synchronous, which costs
    concurrency but never costs ordering.
    """
    target: object = disposer
    while isinstance(target, functools.partial):
        target = target.func
    return inspect.iscoroutinefunction(target)


def _running_loop(what: str) -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        msg = f"{what} needs a running event loop"
        raise RuntimeError(msg) from None


@dataclass(frozen=True)
class EffectNode:
    """One node of the effect tree: what was registered, and from where."""

    label: str | None
    location: str
    children: tuple[EffectNode, ...] = ()


@dataclass
class _Record:
    """One registered effect and the disposers it produced."""

    label: str | None
    location: str
    disposers: list[Disposer] = field(default_factory=list)
    disposed: bool = False


class EffectHandle:
    """The undo for a single registration, and the completion of its setup.

    Callable: calling it disposes just this effect, leaving the rest of the
    scope alone. It returns ``None`` when every disposer ran synchronously and
    an awaitable when one did not -- the ``Disposer`` contract.

    Awaitable: awaiting it waits for asynchronous setup to finish and re-raises
    whatever the setup raised. A synchronous effect completes immediately, so
    ``await scope.effect(...)`` is always valid regardless of shape.
    """

    __slots__ = ("_record", "_scope", "_setup")

    def __init__(
        self, scope: EffectScope, record: _Record, setup: asyncio.Task[None] | None
    ) -> None:
        self._scope = scope
        self._record = record
        self._setup = setup

    def __call__(self) -> Awaitable[None] | None:
        return self._scope.dispose_record(self._record)

    def __await__(self) -> Generator[Any, None, None]:
        return self._settled().__await__()

    async def _settled(self) -> None:
        if self._setup is not None:
            await self._setup

    @property
    def pending(self) -> bool:
        """True while asynchronous setup is still in flight."""
        return self._setup is not None and not self._setup.done()

    def __repr__(self) -> str:
        state = "pending" if self.pending else "settled"
        return f"EffectHandle({self._record.label!r}, {state})"


class EffectScope:
    """A disposal scope: a LIFO of effects and child scopes.

    Entries -- effects and child scopes alike -- unwind in reverse
    registration order, so a child scope created between two effects is
    disposed between them. That is what makes "dispose the parent" a complete
    answer rather than a starting point.
    """

    def __init__(
        self,
        label: str | None = None,
        *,
        parent: EffectScope | None = None,
        location: str | None = None,
    ) -> None:
        self.label = label
        self.location = location if location is not None else _capture_location(2)
        self._parent = parent
        self._entries: list[_Record | EffectScope] = []
        self._disposer_ids: set[int] = set()
        self._active = True
        self._unwind_task: asyncio.Task[None] | None = None

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        """False from the moment disposal starts, not from when it finishes.

        The distinction is the whole point: a registration racing a teardown
        must be rejected while that teardown is still running, or it acquires
        into a scope the unwind has already walked past.
        """
        return self._active

    def _require_active(self, operation: str) -> None:
        if not self._active:
            raise InactiveScopeError(self._describe(), operation)

    def _describe(self) -> str:
        return self.label or f"<unlabelled scope {id(self):#x}>"

    # -- registration ------------------------------------------------------

    def child(self, label: str | None = None) -> EffectScope:
        """A nested scope, disposed at this point in the parent's LIFO order."""
        self._require_active("create a child scope")
        scope = EffectScope(label, parent=self, location=_capture_location(2))
        self._entries.append(scope)
        return scope

    def effect(self, fn: EffectFn, /, label: str | None = None) -> EffectHandle:
        """Run ``fn``, record whatever it produced, and return the undo.

        Raises :class:`InvalidEffectError` if ``fn`` returns something that
        cannot be disposed and :class:`InactiveScopeError` if the scope is
        already disposing -- in both cases without recording anything.
        """
        self._require_active("register an effect")
        record = _Record(label=label, location=_capture_location(2))

        result = fn()  # setup exceptions propagate; nothing is recorded yet

        if _is_async_shape(result):
            task = _running_loop("an asynchronous effect").create_task(
                self._finish_async(record, result)
            )
            self._entries.append(record)
            return EffectHandle(self, record, task)

        try:
            self._absorb(record, result)
        except BaseException:
            self._rollback(record)
            raise
        self._entries.append(record)
        return EffectHandle(self, record, None)

    async def _finish_async(self, record: _Record, result: object) -> None:
        """Complete a registration whose setup is asynchronous.

        Rollback on failure is the same rule as the synchronous path: whatever
        this effect already produced is released, and the record is removed.
        """
        try:
            if hasattr(result, "__aiter__"):
                async for item in result:
                    self._record_disposer(record, item)
            elif hasattr(result, "__aenter__"):
                await result.__aenter__()
                self._record_disposer(record, _AsyncContextExit(result))
            else:
                self._absorb(record, await result)  # type: ignore[misc]
        except BaseException:
            self._rollback(record)
            with contextlib.suppress(ValueError):
                self._entries.remove(record)
            raise

    # -- shape dispatch ----------------------------------------------------

    def _absorb(self, record: _Record, result: object) -> None:
        """Turn one effect result into recorded disposers.

        Structural checks, not isinstance against concrete classes, so a
        third-party resource object that implements the context-manager
        protocol works without knowing this module exists.
        """
        if result is None:
            return

        if hasattr(result, "__enter__") and hasattr(result, "__exit__"):
            # Checked before iteration: a context manager that also happens to
            # be iterable is a context manager.
            result.__enter__()
            self._record_disposer(record, _ContextExit(result))
            return

        if _is_disposer_iterable(result):
            for item in result:  # may contribute more than one disposer
                self._record_disposer(record, item)
            return

        if callable(result):
            self._record_disposer(record, result)
            return

        raise InvalidEffectError(result)

    def _record_disposer(self, record: _Record, item: object) -> None:
        if item is None:
            return
        if not callable(item):
            raise InvalidEffectError(item)
        if id(item) in self._disposer_ids:
            return  # SEM-008: the same disposer re-yielded is recorded once
        self._disposer_ids.add(id(item))
        record.disposers.append(item)

    # -- teardown ----------------------------------------------------------

    def _rollback(self, record: _Record) -> None:
        """Undo a failed registration's own disposers, synchronously.

        Only this effect's disposers, never the scope's -- a bad argument to
        one late registration must not tear down the valid ones before it.
        """
        for disposer in reversed(record.disposers):
            self._disposer_ids.discard(id(disposer))
            try:
                result = disposer()
            except Exception:
                continue
            if inspect.isawaitable(result):
                # Nothing here may await: rollback happens on the synchronous
                # path. Closing the coroutine is the only way to avoid an
                # un-awaited-coroutine warning masking the real exception.
                closer = getattr(result, "close", None)
                if callable(closer):
                    with contextlib.suppress(RuntimeError):
                        closer()
        record.disposers.clear()

    def dispose_record(self, record: _Record) -> Awaitable[None] | None:
        """Dispose one recorded effect. Public so :class:`EffectHandle` can call it."""
        if record.disposed:
            return None
        record.disposed = True
        with contextlib.suppress(ValueError):
            self._entries.remove(record)
        disposers = list(record.disposers)
        record.disposers.clear()
        for disposer in disposers:
            self._disposer_ids.discard(id(disposer))
        if any(_is_async_disposer(d) for d in disposers):
            return _run_disposers(disposers)
        return _run_sync_disposers(disposers)

    async def dispose(self) -> None:
        """Unwind everything, once, in reverse order.

        Concurrent callers do not race: the first starts the unwind and the
        rest await the same completion, so no caller returns while teardown is
        still in flight. Cancelling a caller does not abandon the unwind --
        it runs to the end, and the ``CancelledError`` is raised afterwards.
        """
        if self._unwind_task is None:
            self._active = False  # before the unwind, not after
            self._unwind_task = _running_loop("dispose()").create_task(self._unwind())
        task = self._unwind_task

        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                cancellation = exc
                if task.done():
                    break
        if cancellation is not None:
            raise cancellation

    async def _unwind(self) -> None:
        self._active = False
        errors: list[Exception] = []
        entries, self._entries = self._entries, []

        for entry in reversed(entries):
            if isinstance(entry, EffectScope):
                errors.extend(await _dispose_child(entry))
                continue
            if entry.disposed:
                continue
            entry.disposed = True
            disposers = list(entry.disposers)
            entry.disposers.clear()
            errors.extend(await _run_disposers_collecting(disposers))

        self._disposer_ids.clear()
        if errors:
            raise ExceptionGroup(f"errors while disposing {self._describe()}", errors)

    # -- diagnostics -------------------------------------------------------

    def tree(self, *, skip: EffectScope | None = None) -> EffectNode:
        """What is registered right now, mirroring nesting.

        A value, not a view: it does not change when the scope does, and
        mutating it cannot reach the scope.

        ``skip`` leaves out one nested scope, by identity. The caller that
        needs it is diagnostics: a fiber's nursery holds the scopes of the
        instances it mounted, and those are fibers in their own right, so
        including them would report every effect in the application once per
        ancestor.
        """
        return EffectNode(
            label=self.label,
            location=self.location,
            children=tuple(
                entry.tree()
                if isinstance(entry, EffectScope)
                else EffectNode(entry.label, entry.location)
                for entry in self._entries
                if entry is not skip
            ),
        )

    def effects(self) -> tuple[EffectNode, ...]:
        """This scope's own registered effects, in registration order."""
        return tuple(
            EffectNode(entry.label, entry.location)
            for entry in self._entries
            if not isinstance(entry, EffectScope)
        )

    def __repr__(self) -> str:
        state = "active" if self._active else "disposed"
        return f"EffectScope({self._describe()}, {state}, {len(self._entries)} entries)"


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------


def _is_async_shape(result: object) -> bool:
    return (
        inspect.isawaitable(result)
        or hasattr(result, "__aiter__")
        or (hasattr(result, "__aenter__") and hasattr(result, "__aexit__"))
    )


def _is_disposer_iterable(result: object) -> TypeGuard[Iterable[object]]:
    """Whether to read ``result`` as a sequence of disposers.

    Strings and mappings are excluded: both are iterable, and iterating either
    yields something that is never a disposer. Letting them through would turn
    a returned string into a run of `InvalidEffectError`s about its characters
    rather than one error about the string.
    """
    if isinstance(result, (str, bytes, bytearray)):
        return False
    if hasattr(result, "keys"):  # a mapping; its keys are not disposers
        return False
    return hasattr(result, "__iter__")


@dataclass(frozen=True)
class _ContextExit:
    """Adapts an entered context manager to the disposer contract."""

    manager: Any

    def __call__(self) -> None:
        self.manager.__exit__(None, None, None)


@dataclass(frozen=True)
class _AsyncContextExit:
    """Adapts an entered async context manager to the disposer contract."""

    manager: Any

    async def __call__(self) -> None:
        await self.manager.__aexit__(None, None, None)


# --------------------------------------------------------------------------
# Disposer execution
# --------------------------------------------------------------------------


def _iter_batches(disposers: Sequence[Disposer]) -> Iterator[list[Disposer]]:
    """Group a reverse-order walk into concurrency batches.

    Consecutive known-async disposers form one batch and are awaited together.
    A synchronous disposer is a barrier: it ends the batch before it, runs
    alone, and the disposers recorded before it start only afterwards. That is
    SEM-006 -- concurrency where it is free, ordering where it is observable.
    """
    batch: list[Disposer] = []
    for disposer in reversed(disposers):
        if _is_async_disposer(disposer):
            batch.append(disposer)
            continue
        if batch:
            yield batch
            batch = []
        yield [disposer]
    if batch:
        yield batch


async def _run_disposers_collecting(disposers: Sequence[Disposer]) -> list[Exception]:
    errors: list[Exception] = []
    for batch in _iter_batches(disposers):
        awaitables: list[Awaitable[None]] = []
        for disposer in batch:
            try:
                result = disposer()
            except Exception as exc:
                errors.append(exc)
                continue
            if inspect.isawaitable(result):
                awaitables.append(result)
        if not awaitables:
            continue
        settled = await asyncio.gather(*awaitables, return_exceptions=True)
        errors.extend(r for r in settled if isinstance(r, Exception))
    return errors


async def _dispose_child(child: EffectScope) -> list[Exception]:
    try:
        await child.dispose()
    except ExceptionGroup as group:
        return [e for e in group.exceptions if isinstance(e, Exception)]
    except Exception as exc:
        return [exc]
    return []


async def _run_disposers(disposers: Sequence[Disposer]) -> None:
    errors = await _run_disposers_collecting(disposers)
    if errors:
        raise ExceptionGroup("errors while disposing effect", errors)


def _run_sync_disposers(disposers: Sequence[Disposer]) -> Awaitable[None] | None:
    """Run disposers believed synchronous, in reverse order.

    If one turns out to return an awaitable after all, that awaitable and the
    remaining disposers are handed back as a coroutine for the caller to await.
    Ordering is preserved either way.
    """
    errors: list[Exception] = []
    remaining = list(reversed(disposers))
    while remaining:
        disposer = remaining.pop(0)
        try:
            result = disposer()
        except Exception as exc:
            errors.append(exc)
            continue
        if inspect.isawaitable(result):
            return _finish_mixed(result, remaining, errors)
    if errors:
        raise ExceptionGroup("errors while disposing effect", errors)
    return None


async def _finish_mixed(
    pending: Awaitable[None],
    remaining: Sequence[Disposer],
    errors: list[Exception],
) -> None:
    try:
        await pending
    except Exception as exc:
        errors.append(exc)
    # `remaining` is already in disposal order, and _run_disposers_collecting
    # reverses what it is given, so hand it the sequence back to front.
    errors.extend(await _run_disposers_collecting(list(reversed(remaining))))
    if errors:
        raise ExceptionGroup("errors while disposing effect", errors)
