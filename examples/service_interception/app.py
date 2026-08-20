"""Use one HTTP client while giving each workload its own effective settings."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from cordis import Context, PluginHost, Service, effective_config, inject

if TYPE_CHECKING:
    from collections.abc import Mapping


class HttpClient(Service):
    """A shared client whose callers can read subtree-specific policy settings."""

    name = "http"
    defaults: ClassVar[Mapping[str, object]] = MappingProxyType(
        {"retries": 2, "timeout_ms": 100}
    )


seen: list[tuple[str, int, int]] = []
clients: list[HttpClient] = []

EXPECTED_OUTPUT = (
    "bulk-export: retries=2 timeout_ms=250",
    "payment-webhook: retries=0 timeout_ms=100",
    "shared client: yes",
)


@inject(HttpClient)
def fetch(ctx: Context, workload: str) -> None:
    """Record the shared client and the config this mount sees for it."""
    client = ctx.require(HttpClient)
    config = effective_config(ctx, client)
    retries = config["retries"]
    timeout = config["timeout_ms"]
    if not isinstance(retries, int) or not isinstance(timeout, int):
        raise AssertionError("HTTP client settings must be integers")
    clients.append(client)
    seen.append((workload, retries, timeout))


async def scenario() -> tuple[str, ...]:
    """Mount one provider and two consumers with independent intercepts."""
    seen.clear()
    clients.clear()
    host = PluginHost()

    try:
        await host.root.plugin(HttpClient)
        await host.root.plugin(
            fetch,
            "bulk-export",
            intercept={"http": {"timeout_ms": 250}},
        )
        await host.root.plugin(
            fetch,
            "payment-webhook",
            intercept={"http": {"retries": 0}},
        )

        if seen != [
            ("bulk-export", 2, 250),
            ("payment-webhook", 0, 100),
        ]:
            raise AssertionError(f"unexpected effective settings: {seen!r}")
        if len(clients) != 2 or clients[0] is not clients[1]:
            raise AssertionError("interception replaced the shared client")
    finally:
        await host.dispose()

    return (
        "bulk-export: retries=2 timeout_ms=250",
        "payment-webhook: retries=0 timeout_ms=100",
        "shared client: yes",
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
