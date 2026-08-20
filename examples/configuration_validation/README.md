# Validate deployment configuration before startup

## Context

A delivery worker is started from deployment-owned configuration. Its region
and retry count are not optional assumptions: a string retry count should never
reach code that begins making network calls.

## Problem solved

Untyped dictionaries fail late, usually after a plugin has constructed partial
state. A Cordis dataclass schema validates raw configuration before creating a
working plugin context or invoking the body; invalid data becomes a failed
fiber with field-level errors.

## Practical use case

Use this for YAML/JSON environment configuration, task-worker settings,
webhook credentials, import jobs, or any plugin whose operational inputs must
be accepted or rejected at a clear boundary.

## Run

```console
uv run python -m examples.configuration_validation.app
```

## Deterministic result

```text
accepted: eu-west retries=2
rejected fields: region, retries
plugin body runs: 1
```

The code starts a valid worker and awaits an invalid one. It asserts that the
failure names both malformed fields, that the fiber is `FAILED`, and that the
plugin body ran only for the valid configuration.
