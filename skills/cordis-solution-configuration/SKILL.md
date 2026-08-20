---
name: cordis-solution-configuration
description: Configure declarative Cordis applications, schemas, loader entries, layers, expressions, and live reconciliation safely. Use when creating or changing Cordis YAML, JSON, or TOML configuration; deployment profiles; plugin settings; hot-reload inputs; or configuration validation.
---

# Cordis Solution Configuration

Treat application shape as validated data with stable instance identity. Start
with the selected scenario in
[the scenario inventory](../cordis-scenario-inventory/references/scenario-inventory.md)
and the loader, config, expression, and layering capability records.

## Build a valid configuration boundary

1. Give every loader entry a stable `id` and a resolvable `name`. Use
   `config`, `disabled`, `inject`, `isolate`, `intercept`, or a group
   only where their semantics are needed.
2. Keep an id stable across harmless edits and reordering. It identifies the
   live configured instance and lets `LoaderService.reconcile()` calculate a
   useful change report.
3. Put plugin-specific validation on the plugin with
   `@config_schema(from_dataclass(...))` or another `ConfigSchema`.
   Reject bad deployment data before the plugin body starts.
4. Parse the complete entry list with a supported source such as
   `YamlSource`, `JsonSource`, `TomlSource`, or `read_entries()`.
   Treat all reported entry issues as a rejected configuration, not a list of
   rows to mount opportunistically.

## Compose deployment variation

Use a base entry list plus `Layer` and `Patch` records for environment,
bundle, or user variation. Resolve layers first, inspect the resulting
`Resolution` and provenance, then hand the resolved entries to the loader.
Keep a patch targeted at a stable entry id; do not duplicate the whole
application file for one deployment difference.

Use `Expr` only for the restricted computed-value language. Keep expressions
in fields Cordis accepts for them, and never treat config expressions as a
place to execute arbitrary deployment code.

## Reconcile with the correct lifecycle expectation

Use `LoaderService.reconcile(entries, dry_run=True)` to discover the actual
mount, update, disposal, and failure report before applying a consequential
change. Then reconcile the same entries and wait for the runtime to quiesce.

Expect a config-only change to use the update path. Changing target, injection,
isolation, or interception changes the instance shape and requires a remount.
Use an explicit group only when its child lifetime and inherited settings are
part of the desired design.

## Validate the operator-visible outcome

Record the entry ids changed, source-layer provenance, dry-run report, applied
`ReconcileReport`, and post-change `inspect()` or `pending()` result.
Use `examples/notes` for layers and provider replacement, and
`examples/configuration_validation` for schema rejection.

Run the affected example or focused test, then `just check`. Use `yq` to
query or preview structured YAML changes instead of line-oriented edits.
