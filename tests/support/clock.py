"""A virtual clock, satisfying the scheduling capability's ``Clock`` protocol.

Timing properties are worthless if they are timing-dependent. Every scheduling
card ("an interval fires floor(elapsed / period) times", "a debounced call
fires once after the last trigger") is a statement about *logical* time, so the
tests drive logical time directly and never sleep.

Because the clock is a service, a test installs this by providing it -- the
framework's own composition mechanism applied to itself. Nothing is
monkeypatched, which means the property exercises the same lookup path
production code uses.
"""

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: How many times ``advance`` yields to the event loop after waking a sleeper.
#: A woken coroutine may schedule further work before its next await, and each
#: hop needs one pass. Eight is far above the depth these tests build; it is a
#: bound, not a guess about timing.
DRAIN_PASSES = 8


@dataclass(order=True)
class _Waiter:
    deadline: float
    seq: int
    event: asyncio.Event = field(compare=False)


class VirtualClock:
    """Deterministic time. ``now`` moves only when ``advance`` moves it.

    Sleepers wake in deadline order, and ties break by the order the sleeps
    were issued -- so a property about ordering has a defined answer rather
    than whichever answer the scheduler happened to give.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self._requested: list[float] = []

    # -- the Clock protocol ------------------------------------------------

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float, /) -> None:
        self._requested.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        self._seq += 1
        waiter = _Waiter(self._now + seconds, self._seq, asyncio.Event())
        heapq.heappush(self._waiters, waiter)
        try:
            await waiter.event.wait()
        except asyncio.CancelledError:
            # A cancelled sleeper is not a pending timer. Without this, every
            # cancellation would leave its deadline behind, and `pending` --
            # the witness for "nothing outlived the scope" -- would witness
            # nothing at all.
            self._waiters.remove(waiter)
            heapq.heapify(self._waiters)
            raise

    # -- driving -----------------------------------------------------------

    async def advance(self, seconds: float) -> None:
        """Move logical time forward, waking each sleeper at its own deadline.

        Time is set to a waiter's deadline *before* waking it, so a coroutine
        that reads ``now()`` on wake sees the time it asked for and not the
        time the test advanced to.
        """
        if seconds < 0:
            msg = "time does not run backwards"
            raise ValueError(msg)
        target = self._now + seconds
        while self._waiters and self._waiters[0].deadline <= target:
            waiter = heapq.heappop(self._waiters)
            self._now = waiter.deadline
            waiter.event.set()
            await self.drain()
        self._now = target
        await self.drain()

    async def advance_to(self, when: float) -> None:
        await self.advance(when - self._now)

    async def drain(self) -> None:
        """Let every currently-runnable coroutine reach its next await."""
        for _ in range(DRAIN_PASSES):
            await asyncio.sleep(0)

    # -- inspection --------------------------------------------------------

    @property
    def pending(self) -> tuple[float, ...]:
        """Deadlines still waiting, ascending.

        A scope that has been disposed must leave this empty: a timer that
        survives its owner is exactly the leak the scheduling properties look
        for.
        """
        return tuple(sorted(w.deadline for w in self._waiters))

    @property
    def requested(self) -> Sequence[float]:
        """Every duration passed to ``sleep``, in order.

        The witness for "the scheduler asked for the delay it was configured
        with", which is a different fact from "it woke at the right time".
        """
        return tuple(self._requested)

    def __repr__(self) -> str:
        return f"VirtualClock(now={self._now!r}, pending={list(self.pending)})"
