"""Price, approve, and audit one order through event-owned extensions."""

from __future__ import annotations

import asyncio

from cordis import Bail, EffectScope, Emit, EventBus, Next, Serial, Waterfall

APPROVE_ORDER: Serial[[str, int], bool] = Serial("approve-order")
PRICE_ORDER: Waterfall[[int], int] = Waterfall("price-order")
ORDER_AUDIT: Emit[[str]] = Emit("order-audit")
RESOLVE_TOOL: Bail[[str], str] = Bail("resolve-tool")

EXPECTED_OUTPUT = (
    "quote: 110",
    "Ada authorized: True",
    "blocked authorized: False",
    "weather tool: weather-v1",
    "calendar tool: calendar-v1",
    "unknown tool: None",
    "audit: accepted:Ada:110",
    "listeners after close: 0",
)


def fraud_screen(customer: str, total: int) -> bool | None:
    """Return an explicit denial; return None to let later policies decide."""
    del total
    return False if customer == "blocked" else None


def budget_screen(customer: str, total: int) -> bool:
    """The fallback approval rule for a checkout that passed fraud screening."""
    del customer
    return total <= 100


async def add_tax(next_price: Next[int], subtotal: int) -> int:
    """A waterfall listener that wraps the remaining pricing pipeline."""
    del subtotal
    return await next_price() + 20


async def apply_loyalty_discount(next_price: Next[int], subtotal: int) -> int:
    """A second pricing extension, registered after the tax extension."""
    del subtotal
    return await next_price() - 10


async def scenario() -> tuple[str, ...]:
    """Run fixed inputs through the checkout extension points."""
    scope = EffectScope("checkout")
    bus = EventBus()
    audit: list[str] = []
    tool_calls: list[str] = []

    def primary_tool(name: str) -> str | None:
        tool_calls.append("primary")
        return "weather-v1" if name == "weather" else None

    def fallback_tool(name: str) -> str | None:
        tool_calls.append("fallback")
        return "calendar-v1" if name == "calendar" else None

    try:
        bus.on(ORDER_AUDIT, audit.append, scope=scope)
        bus.on(APPROVE_ORDER, fraud_screen, scope=scope)
        bus.on(APPROVE_ORDER, budget_screen, scope=scope)
        bus.on(PRICE_ORDER, add_tax, scope=scope)
        bus.on(PRICE_ORDER, apply_loyalty_discount, scope=scope)
        bus.on(RESOLVE_TOOL, primary_tool, scope=scope)
        bus.on(RESOLVE_TOOL, fallback_tool, scope=scope)

        quote = await bus.waterfall(PRICE_ORDER, lambda: 100, 100)
        approved = await bus.serial(APPROVE_ORDER, "Ada", 100)
        denied = await bus.serial(APPROVE_ORDER, "blocked", 100)
        weather_tool = bus.bail(RESOLVE_TOOL, "weather")
        weather_calls = tuple(tool_calls)
        tool_calls.clear()
        calendar_tool = bus.bail(RESOLVE_TOOL, "calendar")
        calendar_calls = tuple(tool_calls)
        tool_calls.clear()
        unknown_tool = bus.bail(RESOLVE_TOOL, "unknown")
        unknown_calls = tuple(tool_calls)
        await bus.emit(ORDER_AUDIT, f"accepted:Ada:{quote}")

        if (
            quote,
            approved,
            denied,
            weather_tool,
            weather_calls,
            calendar_tool,
            calendar_calls,
            unknown_tool,
            unknown_calls,
            tuple(audit),
        ) != (
            110,
            True,
            False,
            "weather-v1",
            ("primary",),
            "calendar-v1",
            ("primary", "fallback"),
            None,
            ("primary", "fallback"),
            ("accepted:Ada:110",),
        ):
            raise AssertionError(
                "the event pipeline produced an unexpected order result"
            )
    finally:
        await scope.dispose()

    await bus.emit(ORDER_AUDIT, "after-close")
    if audit != ["accepted:Ada:110"]:
        raise AssertionError("disposed listeners received a later event")
    listener_count = sum(
        len(bus.listeners(event))
        for event in (ORDER_AUDIT, APPROVE_ORDER, PRICE_ORDER, RESOLVE_TOOL)
    )
    if listener_count != 0:
        raise AssertionError(f"disposed listeners remain: {listener_count}")

    return (
        "quote: 110",
        "Ada authorized: True",
        "blocked authorized: False",
        "weather tool: weather-v1",
        "calendar tool: calendar-v1",
        "unknown tool: None",
        "audit: accepted:Ada:110",
        "listeners after close: 0",
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
