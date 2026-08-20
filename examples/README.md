# Practical Cordis examples

Each directory below is a small, runnable application rather than an isolated
API fragment. Its `README.md` explains the operational context, the problem it
solves, and a practical use case. Each `app.py` fixes its inputs, checks the
outcome in code, and prints a stable transcript.

Run the commands from the repository root with `uv`:

```console
uv run python -m examples.notes.app
uv run python -m examples.order_events.app
uv run python -m examples.dynamic_service_restart.app
uv run python -m examples.tenant_scopes.app
uv run python -m examples.service_isolation.app
uv run python -m examples.service_interception.app
uv run python -m examples.configuration_validation.app
uv run python -m examples.runtime_diagnostics.app
uv run python -m examples.runtime_observability.app
uv run python -m examples.scheduled_worker.app
```

`notes` intentionally writes `notes.json` to the current working directory
after switching from its in-memory provider to its file-backed provider. The
other examples have no filesystem side effects.

| Example | Cordis capabilities | Practical scenario |
| --- | --- | --- |
| [`notes`](notes/README.md) | declarative loader, config layering, DI, isolation | Swap a note-store implementation without changing consumers. |
| [`order_events`](order_events/README.md) | event bus, serial and bail decisions, waterfall middleware, effect-owned listeners | Price, approve, and resolve an order through extensible policy hooks. |
| [`dynamic_service_restart`](dynamic_service_restart/README.md) | continuous DI, service registry, effect cleanup | Rotate a model endpoint without leaving a stale client alive. |
| [`tenant_scopes`](tenant_scopes/README.md) | subject scopes, scoped registry, filtered events | Keep tenant/session tools and notifications isolated. |
| [`service_isolation`](service_isolation/README.md) | service realms, DI, pending diagnostics | Hide a privileged token from an untrusted tenant subtree. |
| [`service_interception`](service_interception/README.md) | services, DI, interception | Change a shared HTTP client's settings for individual workloads. |
| [`configuration_validation`](configuration_validation/README.md) | dataclass schemas, plugin mounting | Reject malformed deployment settings before a worker starts. |
| [`runtime_diagnostics`](runtime_diagnostics/README.md) | continuous DI, pending reports, snapshots | Explain why a reporting worker is waiting, then verify recovery. |
| [`runtime_observability`](runtime_observability/README.md) | status transitions, snapshots, retained failures | Build a health and telemetry adapter for a live host. |
| [`scheduled_worker`](scheduled_worker/README.md) | injectable clock, throttling, effect cleanup | Coalesce a burst of updates and prevent delayed work after shutdown. |
