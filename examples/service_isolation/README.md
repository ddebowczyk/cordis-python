# Service isolation: protect a billing token

## Context

A shared agent host can safely expose common services such as logging, while a
privileged billing token must not become available to every tenant capability.

## Problem solved

One unrestricted global service container makes accidental credential access
possible whenever another plugin registers a sensitive service. Convention is
not a security boundary.

## Practical use case

Use a per-service Cordis realm around tenant integrations, third-party tool
adapters, and sandboxed automation. The tenant still sees the shared logger,
while an adapter that needs the root-only billing token stays pending and can
be diagnosed explicitly.

## Run

```console
uv run python -m examples.service_isolation.app
```

## Deterministic result

```text
tenant worker: ACTIVE using shared-logger
billing adapter: PENDING
hidden dependency: billing-token
root token remains: root-only-token
```

The application uses fixed services and checks both sides of the boundary: the
worker starts with the permitted logger, the adapter reports the hidden token,
and the root context still retains that token unchanged.

## APIs demonstrated

- `PluginHandle.plugin(..., isolate=("billing-token",))`
- per-service realms and inherited non-isolated services
- `pending()` diagnostics for intentionally blocked capabilities
- scope-owned service providers
