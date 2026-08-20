# Dynamic service restart: rotating model endpoint

## Context

A model client depends on an endpoint supplied by deployment configuration or a
credential-rotation process. The endpoint may disappear briefly and return at
a different address while the host remains alive.

## Problem solved

A client that captures an old dependency can keep sending traffic to a stale
endpoint. Hand-written stop/start code is easy to omit during a rotation.

## Practical use case

Use continuous Cordis dependency injection for model gateways, database
failover, credential refresh, or any provider whose availability changes at
runtime. The consumer becomes pending when its provider leaves and restarts
when a replacement is bound.

## Run

```console
uv run python -m examples.dynamic_service_restart.app
```

## Deterministic result

```text
initial: PENDING (model-endpoint)
primary endpoint: ACTIVE
after removal: PENDING (model-endpoint)
secondary endpoint: ACTIVE
lifecycle: connect:primary | disconnect:primary | connect:secondary | disconnect:secondary
```

The program supplies fixed endpoint names, disposes the primary provider, and
asserts every state change and cleanup action. No timers, network calls, or
wall-clock timing are involved.

## APIs demonstrated

- `@inject("model-endpoint")`
- `ServiceRegistry.provide()` and scope-owned provider removal
- continuous dependency evaluation and `FiberState`
- `scope_of(ctx).effect()` cleanup across restart and shutdown
