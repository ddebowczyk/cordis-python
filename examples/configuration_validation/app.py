"""Validate a delivery plugin's deployment settings before its body can run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cordis import Context, PluginHost, config_schema
from cordis.errors import ConfigValidationError
from cordis.fiber import FiberState


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    """Deployment-owned settings that must be correct before startup."""

    region: str
    retries: int = 3


started: list[str] = []

EXPECTED_OUTPUT = (
    "accepted: eu-west retries=2",
    "rejected fields: region, retries",
    "plugin body runs: 1",
)


@config_schema(DeliveryConfig)
def delivery_worker(ctx: Context, config: DeliveryConfig) -> None:
    """A body that must never be reached when its raw config is malformed."""
    del ctx
    started.append(f"{config.region}:{config.retries}")


async def scenario() -> tuple[str, ...]:
    """Start one valid worker, then demonstrate a safe invalid startup failure."""
    started.clear()
    host = PluginHost()

    try:
        valid = host.root.plugin(
            delivery_worker,
            {"region": "eu-west", "retries": 2},
        )
        await valid

        invalid = host.root.plugin(
            delivery_worker,
            {"region": 9, "retries": "many"},
        )
        try:
            await invalid
        except ConfigValidationError as error:
            fields = tuple(str(issue.path[0]) for issue in error.issues)
        else:
            raise AssertionError("an invalid config started the delivery worker")

        if fields != ("region", "retries"):
            raise AssertionError(f"unexpected validation fields: {fields!r}")
        if invalid.state is not FiberState.FAILED:
            raise AssertionError(f"invalid worker ended as {invalid.state.name}")
        if started != ["eu-west:2"]:
            raise AssertionError(f"invalid config reached a plugin body: {started!r}")
    finally:
        await host.dispose()

    return (
        "accepted: eu-west retries=2",
        "rejected fields: region, retries",
        "plugin body runs: 1",
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
