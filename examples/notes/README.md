# Swappable note storage

## Context

An application records notes through a `Store` definition while a declarative
Cordis configuration chooses the concrete provider. The application begins
with an in-memory store and can layer environment-specific changes over that
base configuration.

## Problem solved

Replacing a storage implementation commonly forces consumers to import a
provider directly, spreads wiring across the codebase, and makes a rollout a
code change. This example keeps consumers dependent on the `Store` contract,
then changes only the loader entry that provides it.

## Practical use case

Use this shape to move a local development service to a durable implementation
in production, or to migrate a provider at runtime while retaining the same
consumer code and service name.

## Run

From the repository root:

```console
uv run python -m examples.notes.app
```

The run creates `notes.json` in the current directory because the layered
configuration replaces `MemoryStore` with `FileStore`.

To inspect configuration provenance instead of starting the app:

```console
uv run python -m examples.notes.app --dump-config --layer swap.yaml
```

## Deterministic result

The scenario records the original provider, performs the configuration-layered
swap, and records the reactivated consumer against the file-backed provider:

```text
writer wrote hello to MemoryStore
main read 1 from MemoryStore
sandbox read 0 from MemoryStore
--- swapping the store ---
main released MemoryStore
writer wrote hello to FileStore
main read 1 from FileStore
```

The executable returns non-zero if Cordis cannot complete the swap; the CLI
smoke test also compares this full transcript exactly.
