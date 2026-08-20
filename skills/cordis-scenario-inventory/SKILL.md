---
name: cordis-scenario-inventory
description: Route Cordis solution work to the right composition pattern and lifecycle capability. Use when an agent or operator needs to choose a Cordis architecture, map a product or SDLC need to an existing example or specification, compare scopes versus realms versus interception, or decide which Cordis workflow skill to apply.
---

# Cordis Scenario Inventory

Route a request through the real Cordis capability catalog before proposing
architecture or changing code. Treat the inventory as a selection guide, not a
substitute for the applicable specification.

## Route the work

1. Identify the operational need: composition, a swappable provider, extension
   policy, configuration, tenant boundary, scheduling, observation, or recovery.
2. Read [the scenario inventory](references/scenario-inventory.md) and select
   the smallest scenario that explains the need.
3. Open the matching capability record under
   `spec/capabilities/<number>-<capability>.yaml` before making a contract
   claim. The record's `semantics` and `python_design` are authoritative.
4. Run the linked example when it exists. The examples are deterministic and
   make lifecycle behavior visible without external services.
5. Hand the work to the focused skill:

   - Architecture or a new capability: `$cordis-solution-design`
   - Plugin implementation or tests: `$cordis-solution-development`
   - Loader, layers, expressions, or deployment settings:
     `$cordis-solution-configuration`
   - Migration or cleanup of an existing composition:
     `$cordis-solution-refactoring`
   - Health, reload, telemetry, or shutdown:
     `$cordis-solution-operations`
   - A pending, failed, stale, or cross-boundary behavior:
     `$cordis-solution-troubleshooting`

## Make the three boundary choices explicitly

- Use a subject `Scope` and `ScopedRegistry` for contributions that belong
  to one tenant, session, tab, or agent run.
- Use `isolate()` for a subtree that needs a different implementation of one
  named service. It is a composition boundary, not a process or sandbox
  boundary.
- Use `intercept()` when callers need different immutable settings while
  sharing the same service instance.

Do not use one of these mechanisms as a substitute for another. Record the
chosen mechanism, its owner, and its disposal condition in the design.

## Ground every recommendation

Refer to the public API in `docs/reference.md`, not internal implementation
details. Confirm behavior with the matching example under `examples/`, then
run the affected repository checks with `just check` and `just test` when
the solution changes.
