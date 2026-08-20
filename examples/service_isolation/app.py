"""Keep a root-only billing token outside a tenant service realm."""

from __future__ import annotations

import asyncio

from cordis import Context, PluginHost, inject, pending

EXPECTED_OUTPUT = (
    "tenant worker: ACTIVE using shared-logger",
    "billing adapter: PENDING",
    "hidden dependency: billing-token",
    "root token remains: root-only-token",
)


async def scenario() -> tuple[str, ...]:
    """Prove a realm hides one root service while leaving another visible."""
    activity: list[str] = []

    @inject("logger")
    def tenant_worker(ctx: Context) -> None:
        logger = ctx.require("logger")
        if logger != "shared-logger":
            raise TypeError("tenant worker did not receive the shared logger")
        activity.append(f"worker:{logger}")

    @inject("billing-token")
    def billing_adapter(ctx: Context) -> None:
        token = ctx.require("billing-token")
        if not isinstance(token, str):
            raise TypeError("billing token must be a string")
        activity.append(f"billing:{token}")

    host = PluginHost()
    try:
        host.registry.provide("logger", "shared-logger", scope=host.root.scope)
        host.registry.provide("billing-token", "root-only-token", scope=host.root.scope)
        worker = host.root.plugin(tenant_worker, isolate=("billing-token",))
        billing = host.root.plugin(billing_adapter, isolate=("billing-token",))
        await host.runtime.quiesce()

        reports = pending(host)
        root_token = host.root.context.require("billing-token")
        if worker.state.name != "ACTIVE" or activity != ["worker:shared-logger"]:
            raise AssertionError("the tenant worker did not retain its shared service")
        if billing.state.name != "PENDING":
            raise AssertionError("the isolated billing adapter unexpectedly started")
        if len(reports) != 1 or reports[0].names != ("billing-token",):
            raise AssertionError(f"unexpected isolation report: {reports!r}")
        if root_token != "root-only-token":
            raise AssertionError("the root token changed while creating the tenant")
    finally:
        await host.dispose()

    return (
        "tenant worker: ACTIVE using shared-logger",
        "billing adapter: PENDING",
        "hidden dependency: billing-token",
        "root token remains: root-only-token",
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
