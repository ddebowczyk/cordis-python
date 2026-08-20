"""Coalesce a burst of work and cancel delayed work during application shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from cordis import PluginHost, throttle


@dataclass
class ManualClock:
    """A tiny controllable Clock implementation for a deterministic CLI demo."""

    current: float = 0
    _waiters: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    def now(self) -> float:
        return self.current

    async def sleep(self, seconds: float, /) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.current + seconds, future))
        try:
            await future
        finally:
            self._waiters = [entry for entry in self._waiters if entry[1] is not future]

    async def drain(self) -> None:
        """Let newly scheduled Cordis tasks reach their next manual-clock sleep."""
        for _ in range(3):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Move logical time forward and settle every now-due sleep."""
        self.current += seconds
        due = tuple(
            future for deadline, future in self._waiters if deadline <= self.current
        )
        for future in due:
            if not future.done():
                future.set_result(None)
        await self.drain()


EXPECTED_OUTPUT = (
    "coalesced batch: latest",
    "after shutdown: latest",
)


async def scenario() -> tuple[str, ...]:
    """Throttle a fixed burst, then prove teardown cancels a pending batch."""
    host = PluginHost()
    clock = ManualClock()
    batches: list[str] = []

    try:
        await host.registry.provide(
            "clock",
            clock,
            scope=host.root.scope,
            ctx=host.root.context,
        )
        send_batch = throttle(host.root.context, 5, batches.append)

        send_batch("first")
        await clock.drain()
        await clock.advance(1)
        send_batch("second")
        await clock.drain()
        await clock.advance(1)
        send_batch("latest")
        await clock.drain()
        await clock.advance(3)

        if batches != ["latest"]:
            raise AssertionError(
                f"throttle did not keep only the latest batch: {batches!r}"
            )

        send_batch("cancelled")
        await clock.drain()
    finally:
        await host.dispose()

    await clock.advance(10)
    if batches != ["latest"]:
        raise AssertionError(f"shutdown allowed delayed work to run: {batches!r}")

    return (
        "coalesced batch: latest",
        "after shutdown: latest",
    )


def main() -> int:
    """Run and verify the CLI demonstration."""
    lines = asyncio.run(scenario())
    if lines != EXPECTED_OUTPUT:
        raise AssertionError(f"expected {EXPECTED_OUTPUT!r}, got {lines!r}")
    print(*lines, sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
