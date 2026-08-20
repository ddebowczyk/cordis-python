"""Explain a pending worker, provide its dependency, and inspect recovery."""

from __future__ import annotations

import asyncio

from cordis import Context, PluginHost, Service, inject, inspect, pending
from cordis.fiber import FiberState


class Database(Service):
    """The database capability the reporting worker requires."""

    name = "database"


@inject(Database)
def reporting_worker(ctx: Context) -> None:
    """A worker that cannot start until the database service is bound."""
    del ctx


def _leaf(label: str) -> str:
    """Keep diagnostic labels human-scale while preserving real snapshot data."""
    return label.rsplit("/", 1)[-1].split("#", 1)[0]


EXPECTED_OUTPUT = (
    "pending dependency: database",
    "worker after database: ACTIVE",
    "tree: reporting_worker, Database",
)


async def scenario() -> tuple[str, ...]:
    """Use pending reports and snapshots as a health-check recovery loop."""
    host = PluginHost()

    try:
        worker = host.root.plugin(reporting_worker)
        await host.runtime.quiesce()

        reports = pending(host)
        if len(reports) != 1 or reports[0].names != ("database",):
            raise AssertionError(f"unexpected pending report: {reports!r}")
        if worker.state.name != "PENDING":
            raise AssertionError(f"worker was not pending: {worker.state.name}")

        await host.root.plugin(Database)
        await host.runtime.quiesce()
        snapshot = inspect(host)
        names = tuple(_leaf(child.label) for child in snapshot.children)

        if pending(host) != ():
            raise AssertionError("the runtime still reports a blocked worker")
        if worker.state is not FiberState.ACTIVE:
            raise AssertionError(f"worker did not recover: {worker.state.name}")
        if names != ("reporting_worker", "Database"):
            raise AssertionError(f"unexpected recovered tree: {names!r}")
    finally:
        await host.dispose()

    return (
        "pending dependency: database",
        "worker after database: ACTIVE",
        "tree: reporting_worker, Database",
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
