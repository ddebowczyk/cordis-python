# Runtime observability: health and telemetry adapter

## Context

A long-running agent host must distinguish a healthy capability from one that
is waiting on a dependency or has failed during startup. Logs alone do not
offer a reliable control-plane view.

## Problem solved

Without lifecycle transitions and snapshots, operators can only infer why a
plugin is unavailable from incidental side effects. A failed exporter can look
the same as one that was never configured.

## Practical use case

Subscribe once to `FiberRuntime.observe()` from a metrics, tracing, or health
adapter. Combine the transition stream with `inspect()` to power an admin
endpoint, alert rule, or deployment readiness check without putting telemetry
code into each plugin.

## Run

```console
uv run python -m examples.runtime_observability.app
```

## Deterministic result

```text
health: reporting_worker=ACTIVE trace_exporter=FAILED
failure: collector unreachable
transition: reporting_worker:PENDING->LOADING
transition: reporting_worker:LOADING->ACTIVE
transition: trace_exporter:PENDING->LOADING
transition: trace_exporter:LOADING->FAILED
after shutdown: 0 fibers
```

The worker receives a fixed clock and the exporter raises a fixed error. The
script asserts the health snapshot, exact transition order, retained failure,
and an empty runtime after shutdown.

## APIs demonstrated

- `FiberRuntime.observe()` and `StatusChange`
- `inspect()` for a non-mutating health snapshot
- `Fiber.error` for retained startup failures
- orderly host disposal and empty-runtime verification
