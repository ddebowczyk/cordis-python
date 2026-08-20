---
name: cordis-solution-operations
description: Operate, observe, reconcile, reload, and shut down live Cordis hosts safely. Use when building Cordis health checks or telemetry, applying a configuration change, supervising plugins, responding to provider loss, managing scheduled work, or writing a production runbook.
---

# Cordis Solution Operations

Operate the composition tree as a control plane: observe it first, make one
owned change, then prove convergence.

Use [the scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md)
to identify the running pattern, and consult the diagnostics, loader, logging,
scheduling, and hot-reload records for its precise contract.

## Establish a health view

Expose or collect a read-only view built from:

- `FiberRuntime.observe()` for state transitions.
- `inspect(host)` and `walk()` for the current tree and owned effects.
- `pending(host)` for unmet dependencies and their causal chain.
- `Fiber.error` for retained startup failure information.
- `LoggerService` and exporter plugins for structured records tagged by
  originating fiber.

Classify a capability as active, pending, failed, or disposing. Do not collapse
pending and failed into one unhealthy state: they call for different actions.

## Apply a configuration or code change

1. Capture the current snapshot, configuration revision or layer sources, and
   the prior resolved entries for rollback.
2. Before a provider target or realm change, drain application ingress and
   application-owned in-flight work. Cordis permits one live provider for a
   `(name, realm)` pair, so this is a restart rollout and consumers can become
   `PENDING`.
3. Read and validate the candidate entry list. Run loader reconciliation with
   `dry_run=True` before a meaningful rollout.
4. Apply the reconcile or a targeted remount. Record its `ReconcileReport`.
5. Wait for `host.runtime.quiesce()`, then compare the new snapshot and
   pending reports with the intended tree.
6. Escalate a source-code change through `HmrService` only when the host has
   the relevant reload strategy. Review its reload report and failures rather
   than assuming a file change applied.

Dry-run validates the resolved entry shape and lifecycle plan, not a remote
provider's network readiness. Make a provider's startup gate prove that
separately. A saved configuration file has no live effect until it is resolved
and reconciled through the loader.

Do not mutate registry internals or force fiber state to recover a deployment.
Restore the missing provider, correct the configuration, or revert the
responsible entry through the same composition path that created it.

## Operate time-based work and shutdown

Use Cordis scheduling helpers so the plugin effect scope owns all timers and
spawned tasks. On shutdown, dispose the owning host or plugin, await completion,
and verify that no live fibers or unexpected delayed callbacks remain.

Use `examples/runtime_observability`, `examples/runtime_diagnostics`, and
`examples/scheduled_worker` as runnable runbook seeds. Validate a changed
operational adapter with the relevant example and `just check`.
