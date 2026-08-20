# Per-workload settings for a shared HTTP client

## Context

An application owns one HTTP client and its connection pool, but a bulk export
may need a longer timeout while a payment webhook should fail fast with no
retries. These callers must still receive the same client instance.

## Problem solved

Creating a client per workload multiplies pools and changes resource ownership.
Mutating a global client to serve one request races with other workloads.
Cordis interception instead attaches an immutable configuration entry to a
subtree and folds it over the service defaults when that subtree uses the
service.

## Practical use case

Use interception for retry policy, timeouts, API routing, feature switches, or
resource budgets that vary by request, tenant, or plugin but should not create
a new shared connection/resource service.

## Run

```console
uv run python -m examples.service_interception.app
```

## Deterministic result

```text
bulk-export: retries=2 timeout_ms=250
payment-webhook: retries=0 timeout_ms=100
shared client: yes
```

The program asserts the two effective configurations and verifies object
identity, proving that interception altered policy rather than provider choice.
