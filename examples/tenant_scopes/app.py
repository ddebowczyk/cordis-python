"""Give two tenants separate tools and event listeners with owned lifetimes."""

from __future__ import annotations

import asyncio

from cordis import Emit, EventBus, PluginHost, ScopedRegistry, create_scope, scope_of

TENANT_ALERT: Emit[[str, str]] = Emit("tenant-alert")

EXPECTED_OUTPUT = (
    "north tools: billing, north-export",
    "south tools: billing",
    "north alerts: audit:north:invoice-ready | north:invoice-ready",
    "south alerts: audit:south:invoice-ready | south:invoice-ready",
    "south after north closes: billing",
)


def _audit(alerts: list[str], tenant: str, message: str) -> None:
    alerts.append(f"audit:{tenant}:{message}")


def _north_alert(alerts: list[str], tenant: str, message: str) -> None:
    del tenant
    alerts.append(f"north:{message}")


def _south_alert(alerts: list[str], tenant: str, message: str) -> None:
    del tenant
    alerts.append(f"south:{message}")


async def scenario() -> tuple[str, ...]:
    """Register shared and tenant-local contributions, then close one tenant."""
    host = PluginHost()
    bus = EventBus()
    tools: ScopedRegistry[str] = ScopedRegistry()
    alerts: list[str] = []

    try:
        north = create_scope(host.root.context, object(), label="north")
        south = create_scope(host.root.context, object(), label="south")
        await host.runtime.quiesce()

        tools.register("billing", ctx=host.root.context)
        tools.register("north-export", ctx=north.ctx)

        bus.through(host.root.context).on(
            TENANT_ALERT,
            lambda tenant, message: _audit(alerts, tenant, message),
            scope=scope_of(host.root.context),
            global_=True,
        )
        bus.through(north.ctx).on(
            TENANT_ALERT,
            lambda tenant, message: _north_alert(alerts, tenant, message),
            scope=scope_of(north.ctx),
        )
        bus.through(south.ctx).on(
            TENANT_ALERT,
            lambda tenant, message: _south_alert(alerts, tenant, message),
            scope=scope_of(south.ctx),
        )

        await bus.through(north.ctx).emit(TENANT_ALERT, "north", "invoice-ready")
        await bus.through(south.ctx).emit(TENANT_ALERT, "south", "invoice-ready")

        north_tools = tools.visible(ctx=north.ctx)
        south_tools = tools.visible(ctx=south.ctx)
        before_close = tuple(alerts)
        if north_tools != ("billing", "north-export"):
            raise AssertionError(f"north saw unexpected tools: {north_tools!r}")
        if south_tools != ("billing",):
            raise AssertionError(f"south saw unexpected tools: {south_tools!r}")
        if before_close != (
            "audit:north:invoice-ready",
            "north:invoice-ready",
            "audit:south:invoice-ready",
            "south:invoice-ready",
        ):
            raise AssertionError(f"unexpected alert routing: {before_close!r}")

        await north.dispose()
        after_close = tools.visible(ctx=south.ctx)
        if after_close != ("billing",):
            raise AssertionError(f"north-local tool survived teardown: {after_close!r}")
    finally:
        await host.dispose()

    return (
        "north tools: billing, north-export",
        "south tools: billing",
        "north alerts: audit:north:invoice-ready | north:invoice-ready",
        "south alerts: audit:south:invoice-ready | south:invoice-ready",
        "south after north closes: billing",
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
