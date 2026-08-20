# Extensible order events

## Context

A checkout service often needs independent teams to add approval policies,
price adjustments, and audit sinks without editing the checkout coordinator.
Cordis provides named event declarations with explicit dispatch modes and
effect-owned listener lifetimes.

## Problem solved

Ad-hoc callback lists obscure ordering, silently leave stale handlers after a
feature is unloaded, and make “deny” indistinguishable from “no opinion” when
the result is false. `Serial` stops at the first non-`None` answer, `Bail`
does the same for synchronous lookups, `Waterfall` makes middleware nesting
deliberate, and `Emit` broadcasts audits.

## Practical use case

Use this for checkout policies, synchronous tool resolution,
document-processing hooks, request middleware, or a plugin platform where
extensions must disappear cleanly when disabled.

## Run

```console
uv run python -m examples.order_events.app
```

## Deterministic result

```text
quote: 110
Ada authorized: True
blocked authorized: False
weather tool: weather-v1
calendar tool: calendar-v1
unknown tool: None
audit: accepted:Ada:110
listeners after close: 0
```

The application uses fixed prices and policy inputs. It asserts that the false
fraud answer wins, the synchronous tool resolver stops after its first answer,
the waterfall produces `110`, and disposal prevents a later audit event from
reaching any listener.
