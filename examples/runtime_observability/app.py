"""Observe an active worker, a failed exporter, and their lifecycle states."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cordis import Context, PluginHost, inject, inspect

if TYPE_CHECKING:
    from cordis.fiber import StatusChange

EXPECTED_OUTPUT = (
    "health: reporting_worker=ACTIVE trace_exporter=FAILED",
    "failure: collector unreachable",
    "transition: reporting_worker:PENDING->LOADING",
    "transition: reporting_worker:LOADING->ACTIVE",
    "transition: trace_exporter:PENDING->LOADING",
    "transition: trace_exporter:LOADING->FAILED",
    "after shutdown: 0 fibers",
)


@inject("clock")
def reporting_worker(ctx: Context) -> None:
    """A healthy worker that only starts after its clock is available."""
    if ctx.require("clock") != "12:00":
        raise RuntimeError("reporting worker received an unexpected clock")


def trace_exporter(ctx: Context) -> None:
    """A deterministic stand-in for an exporter whose collector is offline."""
    del ctx
    raise RuntimeError("collector unreachable")


def _leaf(label: str) -> str:
    """Remove host and instance suffixes from a diagnostic label."""
    return label.rsplit("/", 1)[-1].split("#", 1)[0]


async def scenario() -> tuple[str, ...]:
    """Build a health record without placing telemetry in every plugin body."""
    host = PluginHost()
    transitions: list[str] = []

    def record(change: StatusChange) -> None:
        transitions.append(
            f"{_leaf(change.fiber.label)}:{change.previous.name}->{change.new.name}"
        )

    stop_observing = host.runtime.observe(record)
    try:
        worker = host.root.plugin(reporting_worker)
        await host.runtime.quiesce()
        if worker.state.name != "PENDING":
            raise AssertionError("worker should wait before the clock is supplied")

        host.registry.provide("clock", "12:00", scope=host.root.scope)
        await host.runtime.quiesce()
        exporter = host.root.plugin(trace_exporter)
        await host.runtime.quiesce()

        snapshot = inspect(host)
        health = tuple(
            (_leaf(child.label), child.state.name) for child in snapshot.children
        )
        if health != (
            ("reporting_worker", "ACTIVE"),
            ("trace_exporter", "FAILED"),
        ):
            raise AssertionError(f"unexpected health snapshot: {health!r}")
        if exporter.error is None or str(exporter.error) != "collector unreachable":
            raise AssertionError("the exporter failure was not retained")
        if transitions != [
            "reporting_worker:PENDING->LOADING",
            "reporting_worker:LOADING->ACTIVE",
            "trace_exporter:PENDING->LOADING",
            "trace_exporter:LOADING->FAILED",
        ]:
            raise AssertionError(f"unexpected lifecycle stream: {transitions!r}")
    finally:
        stop_observing()
        await host.dispose()

    if host.runtime.fibers != ():
        raise AssertionError("shutdown left fibers in the runtime")

    return (
        "health: reporting_worker=ACTIVE trace_exporter=FAILED",
        "failure: collector unreachable",
        *(f"transition: {transition}" for transition in transitions),
        "after shutdown: 0 fibers",
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
