---
name: cordis-solution-troubleshooting
description: Diagnose Cordis pending plugins, startup failures, configuration errors, missing events, visibility leaks, stale reloads, and disposal problems. Use when a Cordis host is not converging, a plugin is unexpectedly inactive or failed, a configuration change has no effect, or tenant and service boundaries behave incorrectly.
---

# Cordis Solution Troubleshooting

Diagnose the live tree before changing it. Cordis exposes the state and causal
chain needed to distinguish a missing dependency from a failed plugin.

Consult [the scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md)
for the suspected pattern, then capture the following evidence from the
affected host or fiber:

```python
snapshot = inspect(host)
reports = pending(host)
tree = render_tree(snapshot, effects=True)
```

Also capture recent `FiberRuntime.observe()` transitions, each affected
`Fiber.error`, and the most recent loader or reload report before attempting
recovery.

## Triage by symptom

### Plugin is pending

Read the `PendingReport.blocked` chain. Check whether the named provider is
absent, waiting on another dependency, in the wrong realm, or part of a cycle.
Restore or mount the provider in the correct context; do not manually invoke a
Consumer body to bypass continuous DI.

### Plugin failed at startup

Inspect `Fiber.error` and mount attribution. For
`ConfigValidationError`, preserve every issue path and correct the raw
configuration or schema. For loader failures, use the dotted
`EntryFailure.id` and its reason to repair the responsible entry while
leaving unrelated entries alone.

### Event has no listener or the wrong result

Verify the declared event mode and the listener's lifetime first. Then check
whether event filtering or a subject scope correctly admitted the listener.
Use the event declaration instead of reimplementing ordering or first-answer
logic in the caller.

### Tenant sees the wrong service or policy

Distinguish the mechanisms:

- A subject-specific contribution needs `Scope` and `ScopedRegistry`.
- A different service implementation needs `isolate()` and a realm.
- Different settings on one shared service need `intercept()`.

Realm isolation is not an authorization sandbox. Escalate an actual security
boundary requirement to the system's authentication, authorization, and
process-isolation design.

### A configuration or reload change appears ineffective

Compare the intended stable entry id, layer resolution, dry-run report, and
applied `ReconcileReport`. A config-only change differs from a target,
injection, isolation, or interception change, which needs a remount. For code,
read the reload report and its failures; never infer success from a changed
file timestamp alone. A saved configuration file has no live effect until the
deployment resolves it and calls loader reconciliation; an HMR-followed source
also needs an explicit reload attempt and report.

### Work fires after shutdown

Find the task or timer owner in the effect tree. Replace unmanaged tasks or
sleep loops with Cordis scheduling helpers, dispose the owning scope, and
confirm the next scheduled callback cannot run.

## Recover with evidence

Apply the smallest composition change that addresses the diagnosed cause, await
quiescence, then recapture `inspect()`, `pending()`, and the relevant
transitions. Prove both the recovered behavior and the absence of stale
listeners, services, tasks, or fibers.

Use `examples/runtime_diagnostics` for pending recovery and
`examples/runtime_observability` for transition and failure evidence.
