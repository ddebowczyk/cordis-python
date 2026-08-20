"""Follow a rotating endpoint as its service appears and disappears."""

from __future__ import annotations

import asyncio

from cordis import Context, PluginHost, inject, scope_of

EXPECTED_OUTPUT = (
    "initial: PENDING (model-endpoint)",
    "primary endpoint: ACTIVE",
    "after removal: PENDING (model-endpoint)",
    "secondary endpoint: ACTIVE",
    "lifecycle: connect:primary | disconnect:primary | connect:secondary | "
    "disconnect:secondary",
)


async def scenario() -> tuple[str, ...]:
    """Rotate one dependency and prove its consumer follows every change."""
    events: list[str] = []

    @inject("model-endpoint")
    def model_client(ctx: Context) -> None:
        endpoint = ctx.require("model-endpoint")
        if not isinstance(endpoint, str):
            raise TypeError("model-endpoint must be a string")
        events.append(f"connect:{endpoint}")

        def release() -> None:
            events.append(f"disconnect:{endpoint}")

        scope_of(ctx).effect(lambda: release, label=f"model-client:{endpoint}")

    host = PluginHost()
    try:
        client = host.root.plugin(model_client)
        await host.runtime.quiesce()
        if client.state.name != "PENDING" or client.missing != ("model-endpoint",):
            raise AssertionError("client did not start pending on its endpoint")

        primary = host.root.scope.child("primary-endpoint")
        host.registry.provide("model-endpoint", "primary", scope=primary)
        await host.runtime.quiesce()
        if client.state.name != "ACTIVE" or events != ["connect:primary"]:
            raise AssertionError("primary endpoint did not start the client")

        await primary.dispose()
        await host.runtime.quiesce()
        if client.state.name != "PENDING" or client.missing != ("model-endpoint",):
            raise AssertionError("removing the endpoint did not stop the client")
        if events != ["connect:primary", "disconnect:primary"]:
            raise AssertionError("primary client effects were not released")

        secondary = host.root.scope.child("secondary-endpoint")
        host.registry.provide("model-endpoint", "secondary", scope=secondary)
        await host.runtime.quiesce()
        if client.state.name != "ACTIVE" or events[-1:] != ["connect:secondary"]:
            raise AssertionError("secondary endpoint did not restart the client")
    finally:
        await host.dispose()

    expected_events = [
        "connect:primary",
        "disconnect:primary",
        "connect:secondary",
        "disconnect:secondary",
    ]
    if events != expected_events:
        raise AssertionError(f"unexpected client lifecycle: {events!r}")

    return (
        "initial: PENDING (model-endpoint)",
        "primary endpoint: ACTIVE",
        "after removal: PENDING (model-endpoint)",
        "secondary endpoint: ACTIVE",
        f"lifecycle: {' | '.join(events)}",
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
