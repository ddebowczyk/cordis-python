---
name: cordis-solution-design
description: Design Cordis-based applications and capability boundaries with reversible lifecycles. Use when planning a new Cordis service, plugin tree, provider/consumer split, multi-tenant model, extension pipeline, or live-reconfiguration strategy before implementation.
---

# Cordis Solution Design

Create a decision-ready composition design: what mounts, what each plugin
owns, what can change, and how the host proves its state.

Read the shared
[scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md)
first. Then open the capability records that cover the selected mechanisms.

## Build the composition model

1. State the user-visible workflow and the expected failure behavior.
2. Draw the mounted plugin tree from `PluginHost.root`. For each node, state
   its config, services provided, dependencies injected, children, effects, and
   disposal owner.
3. Define every interchangeable capability as a three-role seam:
   `Definition` owns the name and types, Providers implement it, and
   Consumers import only the Definition. Do not let Consumers import Providers.
4. Choose the activation mechanism deliberately:

   - Use `@inject` for a capability that cannot run without named services.
   - Use an explicit event mode for extensible decisions or notifications.
   - Use a service for a live owned implementation, not a callback list.

5. Choose the boundary deliberately:

   - `Scope` for per-subject contributions and lifetime.
   - `isolate()` for a separate implementation of selected service names.
   - `intercept()` for per-subtree settings on a shared implementation.

6. Decide whether composition is code-owned or deployment-owned. Use
   `LoaderService` entries for inspectable, patchable application shape;
   retain code composition for embedding or cases that truly require it.

## Specify lifecycle before code

For every plugin, document:

- Start preconditions and the `PENDING` explanation when they are absent.
- The effects it registers and which scope owns their undo.
- Provider removal and replacement behavior.
- Whether a config edit is an update or requires a remount.
- Shutdown behavior for children, listeners, tasks, and scheduled callbacks.

Cordis permits one live binding for a `(name, realm)` pair. A provider target
replacement is therefore a stop/restart path for its declared consumers, not a
seamless dual-provider cutover. When requests must remain uninterrupted, keep
one stable provider bound and switch its private delegate only after the
candidate is ready; make that application-level handoff explicit in the plan.

Do not accept a plan that needs manual stop lists or a separate cleanup path.
Cordis effects and mounted children are the ownership model.

## Plan the control plane

Include a small operator contract:

- How the host reaches quiescence after boot or a reconcile.
- Which `inspect()`, `pending()`, and `FiberRuntime.observe()` signals
  show healthy, blocked, and failed state.
- What configuration entry ids identify an operator's change.
- The rollback or provider-restoration action for a failed rollout.

## Deliver and verify the design

Produce a concise design containing the plugin tree, seam table, configuration
shape, lifecycle matrix, observability points, and acceptance tests. Cite
`docs/reference.md` and the relevant `spec/capabilities/*.yaml` records.

Before implementation, use `ast-grep outline` on the target code and run the
matching example from `examples/` when one demonstrates the same pattern.
