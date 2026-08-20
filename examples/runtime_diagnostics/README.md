# Diagnose a waiting reporting worker

## Context

Cordis dependency injection is continuous: a worker that declares a database
dependency remains pending until a provider exists, then activates when one is
mounted. That is safe, but operators still need to know why a worker has not
started.

## Problem solved

Without a runtime diagnostic surface, an intentionally pending plugin looks
like a silent failure. `pending()` identifies every unmet dependency, while
`inspect()` returns a stable snapshot that an operator can render or query
without mutating the live plugin tree.

## Practical use case

Use this as the basis for a startup health check, an admin endpoint, a CLI
doctor command, or an incident runbook that explains blocked workers before
trying to restart them blindly.

## Run

```console
uv run python -m examples.runtime_diagnostics.app
```

## Deterministic result

```text
pending dependency: database
worker after database: ACTIVE
tree: reporting_worker, Database
```

The program asserts the initial pending report, mounts `Database`, waits for
the runtime to quiesce, then checks both the recovered state and snapshot tree.
