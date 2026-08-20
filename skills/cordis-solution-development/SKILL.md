---
name: cordis-solution-development
description: Implement and test Cordis plugins, services, events, effects, and capability seams safely. Use when building a Cordis application feature, adding a provider or consumer, writing lifecycle-sensitive tests, or extending the Cordis kernel against its specification.
---

# Cordis Solution Development

Build against the capability contract, then make cleanup and restart behavior
part of the implementation instead of an afterthought.

Read the matching entry in
[the scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md),
then inspect the public signature in `docs/reference.md` and the associated
capability record before editing.

## Implement from owned lifetimes

1. Start with a plugin that receives a `Context`; mount descendants through
   the owning handle or context.
2. Register listeners, service provisions, resources, and background work as
   scope-owned effects. Put their undo in the effect disposer rather than a
   global cleanup list.
3. Use `@inject` for required services. Expect a consumer to become pending
   when its provider leaves and to restart when a provider returns.
4. Implement a swappable capability with `Definition`, Provider, and Consumer
   packages. Import the Definition in the outer two roles only.
5. Declare an event with its actual dispatch contract. Choose `Emit` for
   broadcast, `Parallel` for concurrent results, `Serial` or `Bail` for
   first-answer decisions, and `Waterfall` for deliberate middleware flow.
6. Use `spawn(ctx, coro, label=..., on_error=...)` for durable async work and
   Cordis timer helpers for delayed work. `spawn`, `timeout`, `interval`,
   `throttle`, and `debounce` bind work to the context's effect scope; avoid
   naked long-lived tasks that outlive their plugin.

## Validate configuration at the edge

Use `@config_schema(from_dataclass(...))` or another supported
`ConfigSchema` when raw deployment data reaches a plugin. Make the plugin
body rely on the resolved value; do not duplicate unchecked dictionary parsing
inside it.

## Test lifecycle, not only output

Add evidence for the behavior that a simple function test misses:

- A consumer waits for every required service.
- Provider removal disposes dependent effects, then replacement reactivates it.
- Plugin or scope disposal removes listeners, registrations, and delayed work.
- A bad configuration never reaches the plugin body and names its issue paths.
- A realm, scope, or interception boundary changes only the promised surface.
- A loader provider-target swap intentionally reaches `PENDING` and restarts
  declared consumers. For uninterrupted requests, test a stable provider
  facade that switches its private delegate instead of replacing the binding.

For a kernel change, change the relevant capability record first, derive the
property test from its card, and run the declared mutation check. Do not revise
the contract and its test in the same unexamined change.

## Use the repository's evidence

Run the nearest deterministic example, then the focused test module. Finish
with `just lint`, `just types`, `just test`, and `just check` in
proportion to the change. Use `uv run` for Python commands.
