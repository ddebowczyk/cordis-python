# Cordis scenario inventory

Use this inventory to classify a problem before choosing an implementation
workflow. Each scenario is grounded in a capability record, public API, and,
where available, a runnable deterministic example.

## Compose an application host

- **SDLC and use cases:** Greenfield design, plugin platforms, agent hosts, and
  worker systems.
- **Mechanism:** `PluginHost`, `Context`, `ctx.plugin()`, and effect
  scopes.
- **Evidence:** 00 context tree, 01 effect scope, and 03 plugin mounting.

## Keep a consumer correct while a provider changes

- **SDLC and use cases:** Provider migration, endpoint rotation, failover, and
  credential replacement.
- **Mechanism:** `Service`, `@inject`, and continuous fiber activation.
- **Evidence:** 02 service registry, 05 dependency injection, and
  `examples/dynamic_service_restart`.

## Decouple an interface from its providers

- **SDLC and use cases:** Ports and adapters; model, store, tool, or transport
  replacement.
- **Mechanism:** `Definition`, Provider, Consumer, and `Definition.of()`.
- **Evidence:** 19 capability seam and `examples/notes`.

## Let extensions participate in a workflow

- **SDLC and use cases:** Approval policy, pricing, tool lookup, middleware,
  and audit hooks.
- **Mechanism:** `Emit`, `Parallel`, `Serial`, `Bail`, and
  `Waterfall`.
- **Evidence:** 06 event bus and `examples/order_events`.

## Reject bad settings before side effects begin

- **SDLC and use cases:** Deployment configuration, workers, webhooks, and
  integrations.
- **Mechanism:** `@config_schema`, `from_dataclass`, and
  `ConfigValidationError`.
- **Evidence:** 07 config validation and
  `examples/configuration_validation`.

## Make composition deployment-owned data

- **SDLC and use cases:** Feature selection, multiple instances, and
  repeatable deployments.
- **Mechanism:** `Entry`, `LoaderService.reconcile()`, and stable ids.
- **Evidence:** 14 declarative loader and `examples/notes`.

## Apply environment profiles and patches

- **SDLC and use cases:** Development versus production, bundles, tenant
  defaults, and user customization.
- **Mechanism:** `Layer`, `Patch`, `resolve()`, and `Resolution`.
- **Evidence:** 16 config layering and
  `examples/notes --dump-config`.

## Derive a permitted configuration value

- **SDLC and use cases:** Environment lookup, entry references, and live
  service values.
- **Mechanism:** `Expr` and restricted expression evaluation.
- **Evidence:** 15 config expressions.

## Change config or code while the host remains live

- **SDLC and use cases:** Controlled rollout, developer reload, and local
  iteration.
- **Mechanism:** Reconcile reports, `HmrService`, and remount ordering.
- **Evidence:** 17 hot reload and 14 declarative loader.

## Keep tenant or session contributions separate

- **SDLC and use cases:** Multi-tenancy, browser tabs, chat sessions, and
  agent runs.
- **Mechanism:** `Scope`, `ScopedRegistry`, and event filtering.
- **Evidence:** 18 scoped registration and `examples/tenant_scopes`.

## Give a subtree its own implementation of one service

- **SDLC and use cases:** Per-tenant adapters, privileged integrations, and
  test doubles.
- **Mechanism:** `isolate()` and service realms.
- **Evidence:** 08 service isolation and `examples/service_isolation`.

## Vary policy without duplicating a shared resource

- **SDLC and use cases:** Timeouts, retries, routing, budgets, and feature
  switches.
- **Mechanism:** `intercept()` and effective service configuration.
- **Evidence:** 09 service interception and
  `examples/service_interception`.

## Own delayed and background work safely

- **SDLC and use cases:** Debounce, throttle, periodic jobs, batching, and
  graceful shutdown.
- **Mechanism:** `timeout()`, `interval()`, `throttle()`,
  `debounce()`, and `spawn()`.
- **Evidence:** 13 scheduling and `examples/scheduled_worker`.

## Make a live host observable

- **SDLC and use cases:** Readiness, health endpoints, telemetry, and audit
  logs.
- **Mechanism:** `FiberRuntime.observe()`, `inspect()`, and structured
  logging.
- **Evidence:** 04 fiber lifecycle, 11 diagnostics, 12 logging, and
  `examples/runtime_observability`.

## Explain an inactive or failed capability

- **SDLC and use cases:** Incident triage, deployment recovery, and support
  runbooks.
- **Mechanism:** `pending()`, `inspect()`, `render_tree()`, and the
  retained fiber error.
- **Evidence:** 11 diagnostics and `examples/runtime_diagnostics`.

## Change kernel behavior safely

- **SDLC and use cases:** Library development, regression repair, and
  behavior-preserving refactors.
- **Mechanism:** Capability records, property cards, and mutation checks.
- **Evidence:** `spec/`, `tests/`, and `ops/test/mutations.yaml`.

## Selection guardrails

- Continuous DI controls whether a plugin can be active. Do not simulate it with
  ad-hoc polling or manual restart loops.
- A loader id identifies a live configured instance. Keep ids stable across
  harmless edits; target, injection, isolation, and interception changes have
  a different lifecycle cost from a config-only update.
- Realm isolation changes lookup for selected service names. It does not create
  an operating-system, network, or authorization sandbox.
- Interception changes per-subtree policy for one shared service; it does not
  select a different provider.
- Scope ownership determines visibility and cleanup for subject-specific
  contributions; it is not a replacement for a per-service realm.

Read the relevant `spec/capabilities/*.yaml` record for the full contract and
`docs/reference.md` for signatures before implementing a scenario.
