# Tenant-scoped tools and alerts

## Context

A multi-tenant product may keep a common tool catalog while individual tenants
or user sessions contribute their own tools and react only to their own events.
Each tenant needs a lifetime that is independent of the rest of the host.

## Problem solved

Global registries leak tenant-specific tools to other customers, and ordinary
event subscriptions make every tenant react to every notification. Manually
unregistering all those contributions when a session ends is error-prone.

## Practical use case

Use subject scopes for tenant workspaces, browser tabs, chat sessions, agent
runs, or temporary projects. `ScopedRegistry` makes contributions visible only
to a scope and its descendants; a bound event bus routes listeners through the
same scope boundary. A global audit listener remains explicitly global.

## Run

```console
uv run python -m examples.tenant_scopes.app
```

## Deterministic result

```text
north tools: billing, north-export
south tools: billing
north alerts: audit:north:invoice-ready | north:invoice-ready
south alerts: audit:south:invoice-ready | south:invoice-ready
south after north closes: billing
```

The code asserts both the visibility boundary and the precise alert routing,
then disposes the north scope and verifies that its local tool is gone.
