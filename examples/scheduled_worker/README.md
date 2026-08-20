# Deterministic throttled worker

## Context

A worker receives a burst of change notifications and should send one batch
using the newest payload after a five-second window. If the application closes
first, no delayed callback may touch already-disposed resources.

## Problem solved

Direct `asyncio.sleep()` tests are slow and flaky, while ad-hoc background
tasks can outlive the plugin that started them. Cordis treats the clock as an
injectable service and owns throttled tasks through the plugin effect scope.

## Practical use case

Use this for search-index updates, file-change batches, autosave, webhook
coalescing, telemetry flushing, or any noisy signal that should become bounded
background work with reliable shutdown behavior.

## Run

```console
uv run python -m examples.scheduled_worker.app
```

## Deterministic result

```text
coalesced batch: latest
after shutdown: latest
```

`ManualClock` moves only when the example moves it. The code asserts that a
three-call burst emits one latest payload and that a pending later payload never
runs after the host is disposed.
