"""A resource ledger: the oracle for every conservation property.

The recurring claim across the specification is some form of *what was
acquired is released, exactly once, in reverse order*. Proving that needs a
witness the implementation does not own, so effects in tests acquire and
release against this ledger and the assertions read the ledger, never the
runtime's own bookkeeping.

The ledger is strict on purpose. Double-release and release-without-acquire
raise where they happen rather than being tallied and reported later, so the
traceback names the effect that misbehaved instead of the assertion that
noticed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


class LedgerViolationError(AssertionError):
    """The ledger was used in a way no correct implementation could produce."""


@dataclass(frozen=True)
class LedgerEvent:
    """One acquire or release, in the order it happened."""

    resource: str
    action: str  # "acquire" | "release"
    seq: int


class ResourceLedger:
    """Counts acquisitions and releases per resource id.

    Thread-safe because effect disposal may run from a thread executor, and a
    ledger that races would report a phantom leak -- the worst kind of test
    failure, because the natural response is to distrust the test.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[LedgerEvent] = []
        self._live: dict[str, int] = {}
        self._seq = 0

    # -- recording ---------------------------------------------------------

    def acquire(self, resource: str) -> None:
        with self._lock:
            if self._live.get(resource):
                raise LedgerViolationError(
                    f"{resource!r} acquired while already held; resource ids "
                    f"must be unique per acquisition"
                )
            self._live[resource] = 1
            self._record(resource, "acquire")

    def release(self, resource: str) -> None:
        with self._lock:
            held = self._live.get(resource)
            if held is None:
                raise LedgerViolationError(f"{resource!r} released but never acquired")
            if held == 0:
                raise LedgerViolationError(f"{resource!r} released twice")
            self._live[resource] = 0
            self._record(resource, "release")

    def _record(self, resource: str, action: str) -> None:
        self._seq += 1
        self._events.append(LedgerEvent(resource, action, self._seq))

    def scoped(self, resource: str) -> Iterator[None]:
        """Acquire, yield once, release -- the shape a generator effect takes."""
        self.acquire(resource)
        try:
            yield
        finally:
            self.release(resource)

    def disposer(self, resource: str) -> _Disposer:
        """Acquire now and return the callable that releases.

        Returning a bound object rather than a closure keeps the resource id
        visible in a repr, which is what a shrunk counterexample shows.
        """
        self.acquire(resource)
        return _Disposer(self, resource)

    # -- reading -----------------------------------------------------------

    @property
    def events(self) -> Sequence[LedgerEvent]:
        with self._lock:
            return tuple(self._events)

    @property
    def live(self) -> frozenset[str]:
        """Resources acquired and not yet released."""
        with self._lock:
            return frozenset(r for r, held in self._live.items() if held)

    @property
    def balanced(self) -> bool:
        return not self.live

    @property
    def acquire_order(self) -> tuple[str, ...]:
        return tuple(e.resource for e in self.events if e.action == "acquire")

    @property
    def release_order(self) -> tuple[str, ...]:
        return tuple(e.resource for e in self.events if e.action == "release")

    def counts(self) -> Mapping[str, int]:
        """Release count per resource. Every value must be 1 at quiescence."""
        out: dict[str, int] = {}
        for event in self.events:
            if event.action == "release":
                out[event.resource] = out.get(event.resource, 0) + 1
        return out

    def assert_unwound(self, expected: Sequence[str] | None = None) -> None:
        """Assert everything released exactly once, innermost first.

        ``expected`` is the acquisition order the *test* issued. Passing it
        makes the check a real comparison against an independent sequence
        rather than a self-consistency check on the ledger.
        """
        if self.live:
            raise LedgerViolationError(f"still held: {sorted(self.live)}")
        acquired = self.acquire_order if expected is None else tuple(expected)
        if self.release_order != tuple(reversed(acquired)):
            raise LedgerViolationError(
                f"released {self.release_order} but reverse acquisition order "
                f"is {tuple(reversed(acquired))}"
            )

    def __repr__(self) -> str:
        return f"ResourceLedger(live={sorted(self.live)}, events={len(self.events)})"


@dataclass
class _Disposer:
    ledger: ResourceLedger = field(repr=False)
    resource: str

    def __call__(self) -> None:
        self.ledger.release(self.resource)
