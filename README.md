# cordis-python

A Pythonic port of the [Cordis](https://github.com/cordiverse/cordis) composition
kernel: contexts, effects, fibers, services, dependency injection, a typed event
bus, and a declarative plugin loader — for building applications whose shape is
data, not code.

> **Status: all 20 capabilities implemented and held to their specifications;
> pre-release.** The API is stable enough to build on and not yet frozen.

## What it is

An application is a **tree of mounted plugins**. Every plugin gets a `Context`,
contributes capabilities through it, and every contribution is reversible:
unloading a plugin unwinds exactly what it registered — its listeners, its
services, its background tasks, and everything it mounted in turn.

Three consequences follow, and they are the reason the model is worth the
machinery:

- **Order stops mattering.** A plugin declares what it needs and waits until it
  is there. Rows in a config file can be listed in any order, and start
  concurrently.
- **Implementations become swappable at runtime.** Losing a dependency tears a
  consumer down; regaining it brings the consumer back against the new
  implementation. No consumer code changes.
- **Hot reload is not a feature.** It falls out of unload being total.

## Quickstart

Everything below runs. It is one file, in four pieces.

A **service** is a plugin whose whole body is "bind me under my name". A
**plugin** is a function taking a context and, if it wants one, its config.
`@inject` names what the plugin cannot run without: it stays inactive until
those services exist, and unwinds if they go away.

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cordis import Service, inject, scope_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from cordis import Context


class Greeter(Service):
    name = "greeter"  # the configuration-facing name of the same binding

    def greet(self, who: str) -> str:
        return f"hello, {who}"


@inject("greeter")
def consumer(ctx: Context, config: dict[str, Any]) -> None:
    print(ctx.require(Greeter).greet(config["who"]))

    def opened() -> Callable[[], None]:
        # An effect returns its own undo. Registering it is the only place
        # teardown is written -- nothing calls it by hand.
        return lambda: print("consumer: down")

    scope_of(ctx).effect(opened)
```

Mount them in the wrong order on purpose. Nothing sequences them; the consumer
waits, runs when its dependency appears, and unwinds when it is taken away:

```python
import asyncio

from cordis import FiberState, PluginHost


async def wire() -> None:
    host = PluginHost()

    waiting = host.root.plugin(consumer, {"who": "world"})
    await host.runtime.quiesce()  # "is anything still starting?" -- no
    assert waiting.state is FiberState.PENDING  # there is nothing to greet with

    greeter = host.root.plugin(Greeter)
    await host.runtime.quiesce()
    assert waiting.state is FiberState.ACTIVE  # prints: hello, world

    await greeter.dispose()
    await host.runtime.quiesce()
    assert waiting.state is FiberState.PENDING  # prints: consumer: down

    await host.dispose()


asyncio.run(wire())
```

The same application, as data. An entry's `name` says where to find the plugin
and its `config` is passed to it; ids are explicit because they are what a later
edit, a patch, or a reload report refers to:

```yaml
- id: greeter
  name: myapp.plugins:Greeter
- id: consumer
  name: myapp.plugins:consumer
  config: { who: world }
```

The loader mounts an entry list and reconciles the live tree against a new one,
so editing the file above is a diff, not a restart:

```python
import asyncio

from cordis import LoaderService, MappingSource, PluginHost

ROWS = [
    # `YamlSource("app.yaml")` reads exactly these rows from the file above.
    # `__main__` because this quickstart is one file; an application writes
    # `myapp.plugins:Greeter`.
    {"id": "greeter", "name": "__main__:Greeter"},
    {"id": "consumer", "name": "__main__:consumer", "config": {"who": "world"}},
]


async def load() -> None:
    host = PluginHost()
    host.root.plugin(LoaderService)
    await host.runtime.quiesce()

    loader = host.root.context.require(LoaderService)
    report = await loader.reconcile(MappingSource(ROWS).read())
    await host.runtime.quiesce()
    assert report.mounted == ("greeter", "consumer")

    # Reconciling against a shorter list disposes what is no longer in it.
    report = await loader.reconcile(MappingSource(ROWS[:1]).read())
    await host.runtime.quiesce()
    assert report.disposed == ("consumer",)

    await host.dispose()


asyncio.run(load())
```

From here: `@inject` is capability 05, the loader is 14, and every name in the
public API traces to a record under [`spec/capabilities/`](spec/capabilities)
that says what it must do and how that is tested.
[`docs/reference.md`](docs/reference.md) is that mapping, generated: every
export, under the capability that declares it.

## The example

[`examples/notes/`](examples/notes) is the quickstart grown up: a Definition,
two interchangeable providers of it, two consumers that import neither, a group
that resolves its store privately, and a patch layer kept apart from the
application it patches.

```
python -m examples.notes.app                            # boot, then swap the store while it runs
python -m examples.notes.app --dump-config --layer swap.yaml   # every field, and the file it came from
```

The scenario replaces the store underneath two running consumers by reconciling
against a different entry list — the same call the first boot made. Nothing in
`consumers.py` knows a swap is possible. `tests/test_example.py` asserts all of
it, including the part behaviour cannot show: that neither side of the seam
imports the other.

## Specification first

This port is not a transliteration. Every capability is specified before it is
built, as a YAML record under [`spec/capabilities/`](spec/capabilities), and each
record carries the property-based tests that hold the implementation to it.

```
spec/
  schema/capability.v1.yaml     versioned schema; `schema_version` selects it
  capabilities/*.yaml           one record per capability
  check_spec.py                 cross-file consistency and coverage
```

A record states the **problem** the capability solves, its **provenance** in the
TypeScript original, its **normative rules** (each independently citable), the
**idiomatic Python realisation**, and a set of **property cards** — one
falsifiable claim each, with its generator domain, its oracle, an argument for
why that oracle is independent of the implementation, and the concrete defect it
would catch.

Current state: **20 capabilities, 145 normative rules, 126 property cards.**

```
just spec-check      # schema validation + cross-file consistency
just spec-table      # the catalog as a build-order table
```

Where the port deliberately departs from upstream, the record says so in a
`deviation` block with its reasoning — for example, requiring explicit entry ids
instead of generating them, containing listener exceptions in `emit`, replacing
arbitrary JavaScript in config files with a restricted expression evaluator, and
treating only `None` as an abstain value rather than every falsy value.

## Development

Property cards drive the code. For each capability: transcribe its cards into
Hypothesis tests using the declared domain and oracle, watch them fail, then
implement. A card's `failure_value` names the defect the test exists to catch —
if the test still passes once that defect is deliberately introduced, the test is
wrong, not the implementation. That last step is not left to good intentions:
`ops/test/mutations.yaml` declares those defects, and `just ops test mutate all`
introduces each one and requires the suite to go red.

Repository operations live in [`ops/`](ops/README.md), one directory per
capability, each owning its own scripts, schemas, docs and `justfile`. The root
`justfile` defines nothing itself — it delegates:

```
just ops <capability> <command>    # anything a capability declares
just list                          # every capability and its commands
just validate                      # the manifests describe the repository
```

The common ones have aliases:

```
just sync            # uv-managed dev environment
just check           # the fast lane: lint, types, YAML, specs, manifests
just test            # local-tier property tests
just test-nightly    # longer campaigns
just docs            # regenerate docs/reference.md from code + records
just ci              # everything CI runs, in CI's order
just doctor          # the tools the manifests require, and whether you have them
```

`check` and `test` are not lists kept somewhere: a command joins a lane by
declaring `aggregate: check` or `aggregate: test` in its own capability manifest,
and `ops/bin/ops.py` collects them.

The plan lives in [beads](https://github.com/steveyegge/beads): `bd ready` gives
the next unblocked task, and the dependency graph is generated from the specs'
own `depends_on` fields, so it cannot drift from the catalog.

### Constraints

- **Python 3.11+.** `ExceptionGroup`, `BaseException.add_note` and
  `asyncio.TaskGroup` are load-bearing in the specification, not conveniences.
- **`mypy --strict`, no per-module opt-outs.** A signature that cannot be
  expressed is a design signal.
- **Pure Python.** PyPy 3.11 is in the CI matrix to keep it that way, which is
  also why the code uses classic `Generic`/`TypeVar` rather than PEP 695 syntax.
- **No runtime dependencies in the kernel.** Serialisation formats and config
  validators arrive through optional adapters.

## Credits

The design is Cordis by [Shigma](https://github.com/shigma) and the Cordiverse
contributors. The application patterns the tier-3 capabilities encode — the
three-role capability seam, scoped registration, the registry shape — are drawn
from DeepSeek Harness, which is the most substantial Cordis application in the
open.

## License

MIT
