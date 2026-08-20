---
name: cordis-solution-refactoring
description: Refactor Cordis-based applications while preserving capability contracts, ownership, isolation, and restart behavior. Use when replacing direct provider imports, manual lifecycle code, global state, hand-written composition, or unsafe reload logic in an existing Cordis solution.
---

# Cordis Solution Refactoring

Refactor toward clearer capability boundaries without silently changing when a
plugin starts, stops, or becomes visible.

Read the relevant row in
[the scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md)
and capture the current lifecycle evidence before editing.

## Establish the behavioral baseline

1. Map the current mounted tree, providers, consumers, effects, listeners, and
   scheduled work. Use `ast-grep outline` before reading unfamiliar modules.
2. Capture a representative `inspect()` snapshot, `pending()` report, and
   deterministic example transcript or focused test result.
3. Identify public contracts: service names, `Definition` types, event modes,
   loader entry ids, config schema, realm labels, and scope keys.
4. State which behaviors must stay unchanged across the refactor: activation,
   cleanup order, visibility, listener admission, and configuration provenance.

## Apply one structural transformation at a time

- Replace a Consumer-to-Provider import with a Definition package and
  `Definition.of(ctx)`. Keep the service name and required dependency
  explicit.
- Replace manual startup and shutdown bookkeeping with mounted children and
  scope-owned effects. Use `spawn(ctx, ...)` for durable coroutines. Every
  acquired resource needs one owning scope.
- Replace global tenant or session registries with `Scope` and
  `ScopedRegistry`; use `create_scope()` for the subject lifetime and
  `scope_target()` to act on it from elsewhere. Use a realm only when one
  service needs a different implementation.
- Bind scoped event listeners through `EventBus.through(ctx).on(...)` with
  `scope=scope_of(ctx)` rather than maintaining manual unsubscribe lists.
- Replace per-caller service copies with `intercept()` when resource identity
  must remain shared but policy varies.
- Move deployment-owned composition from ad-hoc Python wiring into stable
  loader entries, then use layers for variation.
- Replace broad restart behavior with a targeted reconcile or hot-reload
  mechanism only after proving total disposal of the affected subtree.
- Treat a same-name, same-realm provider replacement as a restart path. Where
  literal request continuity is required, retain a stable facade provider and
  refactor its delegate handoff internally rather than swapping the binding.

## Preserve the contract

Do not rename a loader id merely because a Python symbol moves. Do not turn a
`PENDING` consumer into a failed one to make startup look synchronous. Do not
paper over a missing provider with a global default unless that is an explicit
contract change.

When changing a Cordis kernel capability rather than an application, change its
`spec/capabilities/*.yaml` record before tests and code. Treat its property
cards and mutation verification as the regression boundary.

## Prove the refactor

Compare the baseline with the new tree, pending state, event result, scope or
realm visibility, and post-disposal state. Test provider removal and
replacement when the refactor touches DI or seams. Run focused tests, then
`just lint`, `just types`, `just test`, and `just check` as appropriate.
