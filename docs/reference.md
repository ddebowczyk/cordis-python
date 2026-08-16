# API reference

Every name `cordis` exports, grouped by the capability record that declares it.
Signatures and summaries come from the code; the grouping comes from
`spec/capabilities/`. Both are generated -- run `just docs` after changing
either.

A summary here says what a symbol *is*. What it must *do* is in its record's
normative rules, which each section links to, and what holds it to them is the
property cards listed there.

189 exported names across 20 capabilities.

## Context tree — hierarchical scope, container handle, and identity

Tier 0 &middot; [`context-tree`](../spec/capabilities/00-context-tree.yaml) &middot; 6 normative rules, 6 property cards

A Context is a lightweight handle carrying a chain of scoped metadata and a view onto the service registry; children are derived, never mutated into.

### `Context(*, resolver: ServiceResolver | None = None, label: str = 'ctx')`

*class* &middot; `cordis.context`

A handle onto a scope: some metadata, a parent, and a view of services.

### `ServiceResolver`

*class* &middot; `cordis.context`

The seam between a Context and the service registry.

## Effect scope — every registration returns its own undo

Tier 0 &middot; [`effect-scope`](../spec/capabilities/01-effect-scope.yaml) &middot; 9 normative rules, 8 property cards

A disposal scope that accepts a resource-acquiring effect in any of the supported shapes, records its disposer, and unwinds all disposers exactly once, in reverse order, even when setup or teardown raises.

### `CordisError(message: str)`

*class* &middot; `cordis.errors`

Base class for every error the framework raises deliberately.

### `EffectScope(...)`

*class* &middot; `cordis.effect`

A disposal scope: a LIFO of effects and child scopes.

### `InactiveScopeError(scope: str, operation: str = 'register an effect')`

*class* &middot; `cordis.errors`

Something was registered against a scope that is already disposed.

### `InvalidEffectError(returned: object)`

*class* &middot; `cordis.errors`

An effect factory returned something that is not a disposer.

## Service registry — named capabilities with owned lifetime

Tier 0 &middot; [`service-registry`](../spec/capabilities/02-service-registry.yaml) &middot; 7 normative rules, 6 property cards

A registry mapping (name, realm) to a live implementation, where providing is an effect owned by the provider's scope, so a provider's teardown removes the service and notifies everyone resolving it.

### `BindingChange(kind: ChangeKind, name: str, realm: Realm)`

*class* &middot; `cordis.registry`

One transition of the binding set.

### `BindingInfo(name: str, realm: Realm, value: object, provider: str, published: bool)`

*class* &middot; `cordis.registry`

A read-only description of one binding.

### `ChangeKind`

*class* &middot; `cordis.registry`

What happened to the binding set.

### `EffectHandle(scope: EffectScope, record: _Record, setup: asyncio.Task[None] | None)`

*class* &middot; `cordis.effect`

The undo for a single registration, and the completion of its setup.

### `RegistryConflictError(registry: str, key: str)`

*class* &middot; `cordis.errors`

A capability registry already holds an entry under the offered key.

### `Service(ctx: Context)`

*class* &middot; `cordis.registry`

Base class for services that want to be resolvable by their type.

### `ServiceConflictError(name: str, holder: str, claimant: str)`

*class* &middot; `cordis.errors`

A second provider claimed a service name that is already bound.

### `ServiceNotFoundError(name: str, searched: Sequence[str] = ())`

*class* &middot; `cordis.errors`

A name resolved through a Context matched no service and no scoped key.

### `ServiceRegistry()`

*class* &middot; `cordis.registry`

The binding set, plus the notifications that describe its changes.

### `resolve(base: Sequence[Entry], layers: Sequence[Layer], /) -> Resolution`

*function* &middot; `cordis.layering`

Fold ``layers`` onto ``base`` in order, or raise naming the offender.

## Plugin mounting — the unit of composition

Tier 1 &middot; [`plugin-mounting`](../spec/capabilities/03-plugin-mounting.yaml) &middot; 8 normative rules, 8 property cards

`ctx.plugin(target, config)` normalises any supported plugin form into a mounted instance with its own child context and effect scope, and returns a handle whose disposal unloads that instance and everything it mounted.

### `ConfigPreparer`

*str*

### `InvalidPluginError(plugin: object, reason: str)`

*class* &middot; `cordis.errors`

An object was mounted that is not a plugin in any of the accepted forms.

### `PluginForm(...)`

*class* &middot; `cordis.plugin`

One target, reduced to "what to call and with how many arguments".

### `PluginHandle(...)`

*class* &middot; `cordis.plugin`

One mounted instance: its context, its scope, its state, its children.

### `PluginHost(registry: ServiceRegistry | None = None, *, label: str = 'root')`

*class* &middot; `cordis.plugin`

A root instance and the registry its plugin tree binds services in.

### `PluginTarget()`

*class*

### `config_of(ctx: Context) -> object`

*function* &middot; `cordis.plugin`

The config the instance ``ctx`` belongs to was mounted with.

### `normalise(target: object) -> PluginForm`

*function* &middot; `cordis.plugin`

Reduce ``target`` to a :class:`PluginForm`, or reject it.

### `scope_of(ctx: Context) -> EffectScope`

*function* &middot; `cordis.plugin`

The effect scope owned by the instance ``ctx`` belongs to.

## Fiber lifecycle — the state machine of one mounted instance

Tier 1 &middot; [`fiber-lifecycle`](../spec/capabilities/04-fiber-lifecycle.yaml) &middot; 9 normative rules, 7 property cards

Each mounted plugin is a fiber with an observable state machine (pending, loading, active, failed, unloading, disposed) whose transitions are broadcast, making "why is nothing happening" answerable without a debugger.

### `Fiber(...)`

*class* &middot; `cordis.fiber`

One mounted instance, seen as a state machine.

### `FiberRuntime()`

*class* &middot; `cordis.fiber`

One application's status channel and its definition of "settled".

### `FiberState`

*class* &middot; `cordis.fiber`

The six states of a mounted instance (SEM-001).

### `IllegalTransitionError(subject: str, source: FiberState, target: FiberState)`

*class* &middot; `cordis.fiber`

A transition outside the permitted set was attempted.

### `SETTLED`

*frozenset*

### `StatusChange(fiber: Fiber, previous: FiberState, new: FiberState)`

*class* &middot; `cordis.fiber`

One transition, as its listeners see it (SEM-005).

### `StatusListener`

*str*

### `TRANSITIONS`

*dict*

### `spawn(...)`

*function* &middot; `cordis.timer`

Run ``coro`` as a task owned by ``ctx``'s scope.

## Dependency injection — continuous, not one-shot

Tier 1 &middot; [`dependency-injection`](../spec/capabilities/05-dependency-injection.yaml) &middot; 10 normative rules, 7 property cards

A fiber declares the services it requires; the runtime holds it inactive until all are bound, and tears it down and reloads it whenever the set of bound implementations changes — which is the single mechanism behind ordering-independence, provider swap, cascade teardown, and hot reload.

### `DependencyCycleError(cycle: Sequence[str])`

*class* &middot; `cordis.errors`

Declared injections form a cycle, so no member of it can ever activate.

### `ProblemListener`

*str*

### `Token`

*str*

### `dependencies_of(target: object) -> tuple[str, ...]`

*function* &middot; `cordis.inject`

The registry names ``target`` declares it cannot run without.

### `inject(*tokens: str | type[Service]) -> Callable[[_T], _T]`

*function* &middot; `cordis.inject`

Declare what a plugin needs, as a decorator.

### `provisions_of(target: object) -> tuple[str, ...]`

*function* &middot; `cordis.inject`

The registry names ``target`` promises to bind.

### `token_label(token: object) -> str`

*function* &middot; `cordis.registry`

How to name ``token`` in an error, without calling ``repr`` on a class.

## Event bus — five dispatch modes as explicit contracts

Tier 1 &middot; [`event-bus`](../spec/capabilities/06-event-bus.yaml) &middot; 11 normative rules, 10 property cards

A typed event bus where the dispatch mode (emit, parallel, serial, bail, waterfall) is part of each event's declaration, so a listener author knows whether they may return a value, run concurrently, veto, or intercept.

### `Bail(name: str)`

*class* &middot; `cordis.events`

``Serial`` for synchronous dispatch sites: listeners are not awaited.

### `Emit(name: str)`

*class* &middot; `cordis.events`

Announce something. Listeners return nothing and never block a caller.

### `ErrorReport(event: str, listener: str, error: BaseException)`

*class* &middot; `cordis.events`

A listener failure that must not reach the dispatching caller.

### `Event(name: str)`

*class* &middot; `cordis.events`

The declaration of an extension point.

### `EventBus()`

*class* &middot; `cordis.events`

Registration, ordering, and the five dispatchers.

### `EventModeError(event: str, declared: str, attempted: str)`

*class* &middot; `cordis.errors`

An event was dispatched through a mode it was not declared with.

### `Next`

*str*

### `NextCalledTwiceError(event: str, listener: str)`

*class* &middot; `cordis.errors`

A waterfall listener invoked ``next`` more than once.

### `Parallel(name: str)`

*class* &middot; `cordis.events`

Announce something to listeners that run concurrently and are awaited.

### `Serial(name: str)`

*class* &middot; `cordis.events`

Ask, in order, until someone answers. ``None`` abstains.

### `Waterfall(name: str)`

*class* &middot; `cordis.events`

Wrap the operation. Each listener receives ``next`` and the arguments.

## Config validation — a plugin never starts half-configured

Tier 1 &middot; [`config-validation`](../spec/capabilities/07-config-validation.yaml) &middot; 7 normative rules, 7 property cards

Plugin configuration is validated and defaulted against a declared schema before the plugin body runs; invalid config fails that fiber with per-field issue paths and leaves the rest of the tree untouched.

### `AsyncValidationError(schema: object)`

*class* &middot; `cordis.errors`

A schema's ``validate`` returned an awaitable.

### `CONFIG_SCHEMA_ATTR`

*str*

### `ConfigIssue(path: tuple[str | int, ...], message: str)`

*class* &middot; `cordis.config`

One thing wrong, and where.

### `ConfigResult(value: T | None, issues: tuple[ConfigIssue, ...] = ())`

*class* &middot; `cordis.config`

Either a resolved value or the reasons there isn't one.

### `ConfigSchema`

*class* &middot; `cordis.config`

Anything that can say whether a config is acceptable.

### `ConfigValidationError(plugin: str, issues: Sequence[IssueLike])`

*class* &middot; `cordis.errors`

A plugin's configuration did not satisfy its declared schema.

### `IssueLike`

*class* &middot; `cordis.errors`

The shape of a validation issue, as this module needs to render it.

### `config_schema(schema: ConfigSchema[Any] | type) -> Callable[[F], F]`

*function* &middot; `cordis.config`

Attach ``schema`` to a plugin that cannot carry a ``Config`` attribute.

### `from_dataclass(cls: type[T]) -> ConfigSchema[T]`

*function* &middot; `cordis.config`

Read ``cls`` as a schema, refusing annotations it cannot check.

### `resolve_config(...)`

*function* &middot; `cordis.config`

The value a body should be handed, or the reason it may not run.

### `schema_of(target: object) -> ConfigSchema[Any] | None`

*function* &middot; `cordis.config`

The schema ``target`` declared, adapted, or ``None`` if it declared none.

## Service isolation — realms give a subtree its own implementation

Tier 2 &middot; [`service-isolation`](../spec/capabilities/08-service-isolation.yaml) &middot; 6 normative rules, 5 property cards

`ctx.isolate(name, label=None)` returns a child context in which a service name resolves in a fresh realm, so a subtree can be given its own provider without affecting siblings; a shared label joins an existing realm instead of minting one.

### `DEFAULT_REALM`

*Realm* &middot; `cordis.registry`

### `Isolation`

*str*

### `Realm(label: str = 'default', *, parent: Realm | None = None)`

*class* &middot; `cordis.registry`

An isolation boundary for bindings.

### `enter_realm(ctx: Context, realm: Realm) -> Context`

*function* &middot; `cordis.registry`

A child context whose subtree resolves inside ``realm``.

### `isolate(ctx: Context, names: Isolation, /) -> Context`

*function* &middot; `cordis.realm`

A child context resolving each of ``names`` in a realm of its own.

### `isolated_names(ctx: Context) -> frozenset[str]`

*function* &middot; `cordis.realm`

Every name isolated anywhere in ``ctx``'s lineage.

### `isolated_realm(name: str, label: str | None = None) -> Realm`

*function* &middot; `cordis.realm`

The realm ``name`` should resolve in under ``label``.

### `realm_for(ctx: Context, name: str) -> Realm`

*function* &middot; `cordis.registry`

The realm ``ctx`` resolves ``name`` in.

### `realm_key(name: str) -> str`

*function* &middot; `cordis.registry`

The scoped-metadata key carrying a context's realm for one name.

### `realm_of(ctx: Context) -> Realm`

*function* &middot; `cordis.registry`

The realm this context belongs to.

## Service interception — per-subtree configuration of a shared service

Tier 2 &middot; [`service-interception`](../spec/capabilities/09-service-interception.yaml) &middot; 5 normative rules, 4 property cards

`ctx.intercept(name, config)` returns a child context whose calls into a shared service carry accumulated per-subtree configuration, so one instance can behave differently for different callers without being duplicated.

### `INTERCEPT_KEY`

*str*

### `InterceptResolver`

*class* &middot; `cordis.intercept`

What a service defines when it wants to fold the chain itself.

### `Interception`

*str*

### `effective_config(...)`

*function* &middot; `cordis.intercept`

What ``service`` should do for a caller resolving from ``ctx``.

### `intercept(...)`

*function* &middot; `cordis.intercept`

A child context contributing one entry to ``name``'s chain.

### `intercept_all(ctx: Context, entries: Interception, /) -> Context`

*function* &middot; `cordis.intercept`

A child context contributing an entry to each of several chains.

### `intercept_key(name: str) -> str`

*function* &middot; `cordis.intercept`

The scoped-metadata key carrying a context's own entry for ``name``.

### `intercepted_names(ctx: Context) -> frozenset[str]`

*function* &middot; `cordis.intercept`

Every name intercepted anywhere in ``ctx``'s lineage.

### `interceptions(...)`

*function* &middot; `cordis.intercept`

Every entry ``ctx`` sees for ``name``, outermost first.

### `merge_interceptions(chain: Sequence[Mapping[str, object]], /) -> Mapping[str, object]`

*function* &middot; `cordis.intercept`

The default fold: shallow, last wins, and ``None`` is a value.

### `service_name(token: object, *, subject: object = None) -> str`

*function* &middot; `cordis.registry`

The registry name ``token`` stands for.

## Event filtering — listener admission decided by the registration context

Tier 2 &middot; [`event-filtering`](../spec/capabilities/10-event-filtering.yaml) &middot; 6 normative rules, 5 property cards

A context may carry a filter that decides, per dispatch, whether listeners registered under it are admitted, which is what lets one shared event bus serve many independent subjects without any listener checking whether an event is "for it".

### `BoundBus(bus: EventBus, ctx: Context)`

*class* &middot; `cordis.events`

An :class:`EventBus` viewed through one context.

### `FILTER_KEY`

*str*

### `Filter`

*str*

### `filter_of(ctx: Context) -> Filter | None`

*function* &middot; `cordis.filter`

The filter ``ctx`` registers under, or ``None`` when it admits everything.

### `with_filter(ctx: Context, predicate: Filter, /) -> Context`

*function* &middot; `cordis.filter`

A child context admitting only the dispatches ``predicate`` accepts.

## Diagnostics — the runtime explains itself

Tier 2 &middot; [`diagnostics`](../spec/capabilities/11-diagnostics.yaml) &middot; 6 normative rules, 5 property cards

A read-only introspection surface over the live plugin tree, effect tree, binding set, and listener set, plus composed stacks that name the configuration entry responsible for a failure — because the dominant failure mode of this architecture is silence, not exceptions.

### `Blockage(name: str, provider: str | None, root: str | None)`

*class* &middot; `cordis.diagnostics`

One unmet dependency, and who owes it.

### `FiberSnapshot(...)`

*class* &middot; `cordis.diagnostics`

One instance, frozen at a point in the event loop.

### `MOUNT_NOTE_PREFIX`

*str*

### `PendingReport(uid: int, label: str, blocked: tuple[Blockage, ...])`

*class* &middot; `cordis.diagnostics`

One waiting instance, with a cause per unmet name.

### `attribute_mount_site(exc: BaseException, site: str) -> None`

*function* &middot; `cordis.errors`

Record ``site`` on ``exc`` as an enclosing mount site.

### `inspect(subject: Subject, /) -> FiberSnapshot`

*function* &middot; `cordis.diagnostics`

Snapshot ``subject`` and everything it mounted.

### `mount_attribution(site: str) -> Iterator[None]`

*function* &middot; `cordis.errors`

Annotate anything escaping this block with the mount site, then re-raise.

### `mount_sites(exc: BaseException) -> tuple[str, ...]`

*function* &middot; `cordis.errors`

The mount trail recorded on ``exc``, innermost first.

### `pending(subject: Subject, /) -> tuple[PendingReport, ...]`

*function* &middot; `cordis.diagnostics`

Every waiting instance under ``subject``, with each unmet name's cause.

### `render_tree(...)`

*function* &middot; `cordis.diagnostics`

Render a snapshot for a reader.

### `walk(snapshot: FiberSnapshot, /) -> Iterator[FiberSnapshot]`

*function* &middot; `cordis.diagnostics`

Depth-first over a snapshot tree, the node itself first.

## Logging — a structured record channel with pluggable exporters

Tier 2 &middot; [`logging`](../spec/capabilities/12-logging.yaml) &middot; 6 normative rules, 6 property cards

A logger service producing structured records tagged with their originating fiber, delivered to zero or more exporters that are themselves plugins — so where logs go is a configuration decision, not a code decision.

### `DEFAULT_BUFFER`

*int*

### `DETACHED`

*str*

### `ExportFailure(exporter: str, record: Record, error: BaseException)`

*class* &middot; `cordis.logging`

An exporter that raised. Contained, reported, never re-raised (SEM-003).

### `Exporter`

*class* &middot; `cordis.logging`

Anything that can be handed a record. Structural: no base class.

### `FIBER_KEY`

*str*

### `Level`

*class* &middot; `cordis.logging`

Severity, with the standard library's numbers.

### `Logger(service: LoggerService, name: str, fiber: str)`

*class* &middot; `cordis.logging`

A writer under one name, for one fiber.

### `LoggerService(...)`

*class* &middot; `cordis.logging`

The record channel: sequence numbers, exporters, and the boot buffer.

### `Record(...)`

*class* &middot; `cordis.logging`

One thing that happened, not yet turned into text.

### `fiber_of(ctx: Context) -> Fiber | None`

*function* &middot; `cordis.plugin`

The instance ``ctx`` belongs to, or ``None`` outside any mount.

### `logger(ctx: Context, name: str, /) -> Logger`

*function* &middot; `cordis.logging`

A logger under ``name``, attributed to the instance ``ctx`` belongs to.

## Scheduling — time-based work that unloads with its owner

Tier 2 &middot; [`scheduling`](../spec/capabilities/13-scheduling.yaml) &middot; 7 normative rules, 5 property cards

Timer, interval, throttle, debounce and background-task helpers that are effects, so every scheduled callback and every spawned task is cancelled when the plugin that created it unloads.

### `Clock`

*class* &middot; `cordis.timer`

Time, as scheduling needs it: reading it and waiting on it.

### `Report`

*str*

### `Schedule(handle: EffectHandle, counts: _Counts)`

*class* &middot; `cordis.timer`

A repeating schedule: the disposer, plus what it actually did.

### `SystemClock()`

*class* &middot; `cordis.timer`

Real time. Monotonic, because a schedule must not follow a clock change.

### `TimerFailure(label: str, error: BaseException)`

*class* &middot; `cordis.timer`

A scheduled callable that raised. Contained, reported, never propagated.

### `clock_of(ctx: Context) -> Clock`

*function* &middot; `cordis.timer`

The ``clock`` service ``ctx`` resolves, or the system clock.

### `debounce(...)`

*function* &middot; `cordis.timer`

Call ``fn`` once, ``delay`` after the last call in a burst.

### `interval(...)`

*function* &middot; `cordis.timer`

Call ``fn`` every ``period``, on a grid, without ever overlapping itself.

### `throttle(...)`

*function* &middot; `cordis.timer`

Call ``fn`` at most once per ``period``, with that window's last arguments.

### `timeout(...)`

*function* &middot; `cordis.timer`

Call ``fn`` once, ``delay`` from now, unless the scope goes first.

## Declarative loader — the application is a config file

Tier 3 &middot; [`declarative-loader`](../spec/capabilities/14-declarative-loader.yaml) &middot; 8 normative rules, 9 property cards

A plugin that reads a list of entries from a config file, mounts each as a plugin, and reconciles the live tree against the file whenever it changes — diffing by stable entry id so unchanged rows are never remounted.

### `Entry(...)`

*class* &middot; `cordis.loader`

One row of the file: what to mount, how, and under what id.

### `EntryFailure(id: str, reason: str, error: BaseException)`

*class* &middot; `cordis.loader`

An entry that did not reach a running state, and why.

### `FileSource`

*class* &middot; `cordis.loader`

Anything that can produce an entry list.

### `GROUP`

*str*

### `ImportTargets(ctx: Context)`

*class* &middot; `cordis.loader`

The default source: ``pkg.mod:attr``, a bare module, or a file path.

### `JsonSource(path: Path | str, key: str | None = None)`

*class* &middot; `cordis.loader`

A JSON file holding a list, or a mapping with ``key`` in it.

### `LoaderService(ctx: Context)`

*class* &middot; `cordis.loader`

Mounts an entry list, and reconciles it against the live tree.

### `MappingSource(raw: object)`

*class* &middot; `cordis.loader`

Entries already in memory, still validated like anything else.

### `ReconcileReport(...)`

*class* &middot; `cordis.loader`

What one reconcile did, by dotted entry path.

### `TargetSource(ctx: Context)`

*class* &middot; `cordis.loader`

The contract for turning an entry's ``name`` into something mountable.

### `TomlSource(path: Path | str, key: str | None = 'plugins')`

*class* &middot; `cordis.loader`

A TOML file. Its top level is a table, so the list lives under a key.

### `YamlSource(path: Path | str, key: str | None = None)`

*class* &middot; `cordis.loader`

A YAML file. Needs PyYAML, which nothing else here does.

### `as_mapping(entry: Entry, /) -> dict[str, object]`

*function* &middot; `cordis.loader`

The inverse of :func:`read_entries` for one entry, defaults omitted.

### `read_entries(raw: object, /) -> tuple[Entry, ...]`

*function* &middot; `cordis.loader`

Validate a raw entry list in full, or raise naming every problem.

## Config expressions — computed values without arbitrary code execution

Tier 3 &middot; [`config-expressions`](../spec/capabilities/15-config-expressions.yaml) &middot; 6 normative rules, 6 property cards

A restricted, side-effect-free expression language usable in specific entry fields, so a config file can reference the environment, other entries, and live service values without becoming a program.

### `BUDGET`

*int*

### `CompiledExpr(source: str, tree: ast.Expression)`

*class* &middot; `cordis.expr`

A source string that has already been checked against the grammar.

### `ENVELOPE`

*str*

### `Expr(source: str)`

*class* &middot; `cordis.expr`

An expression as it appears in a document: its source, and nothing else.

### `ExpressionError(entry_id: str, field: str, source: str, reason: str)`

*class* &middot; `cordis.errors`

A configuration expression was rejected or failed to evaluate.

### `FUNCTIONS`

*dict*

### `FunctionSource`

*class* &middot; `cordis.expr`

Extra allow-listed functions, bound as a service.

### `MAX_DEPTH`

*int*

### `Opaque(why: str)`

*class* &middot; `cordis.expr`

A value an expression is not permitted to read, and why.

### `Problem`

*GenericAlias*

### `TAG`

*str*

### `compile_expr(source: str, /, *, entry_id: str, field: str) -> CompiledExpr`

*function* &middot; `cordis.expr`

Parse and check ``source``, or raise naming where it was written.

### `envelope_of(expr: Expr, /) -> dict[str, str]`

*function* &middot; `cordis.expr`

The portable form of ``expr``, valid in YAML, JSON and TOML alike.

### `evaluate(...)`

*function* &middot; `cordis.expr`

Evaluate ``expr`` against ``env``, or raise naming where it was written.

### `expression_paths(value: object, /) -> tuple[tuple[str | int, ...], ...]`

*function* &middot; `cordis.expr`

Every position in ``value`` holding an expression, envelope or parsed.

### `is_envelope(value: object, /) -> bool`

*function* &middot; `cordis.expr`

Whether ``value`` is the portable form: a mapping of exactly one key.

### `opaque(value: object, why: str, /) -> object`

*function* &middot; `cordis.expr`

``value`` with every expression in it replaced by :class:`Opaque`.

### `parse_expressions(value: object, /) -> tuple[object, tuple[Problem, ...]]`

*function* &middot; `cordis.expr`

Replace every envelope in ``value`` with an :class:`Expr`, compiling it.

### `substitute(...)`

*function* &middot; `cordis.expr`

Evaluate every expression inside ``value``, in place of itself.

### `unparse_expressions(value: object, /) -> object`

*function* &middot; `cordis.expr`

The inverse: every :class:`Expr` back to its portable envelope.

### `yaml_loader() -> type`

*function* &middot; `cordis.expr`

A ``SafeLoader`` subclass that reads ``!expr`` as an :class:`Expr`.

## Config layering — bundles, profiles, and patches compose into one tree

Tier 3 &middot; [`config-layering`](../spec/capabilities/16-config-layering.yaml) &middot; 7 normative rules, 5 property cards

An ordered sequence of patch layers is folded onto a base entry list, where each patch targets entries by id to replace fields or insert rows — turning product packaging, deployment profiles, and user customisation into the same mechanism.

### `BASE`

*str*

### `Fields`

*GenericAlias*

### `Layer(source: str, patches: tuple[Patch, ...] = ())`

*class* &middot; `cordis.layering`

One party's contribution, named by where it came from.

### `Patch(...)`

*class* &middot; `cordis.layering`

One instruction from one layer.

### `PatchTargetError(patch_id: str, layer_source: str, target: str, reason: str)`

*class* &middot; `cordis.errors`

A config-layer patch cannot be applied to the entry it names.

### `Resolution(...)`

*class* &middot; `cordis.layering`

The folded entry list, and who wrote each field of it.

### `read_layer(raw: object, source: str, /) -> Layer`

*function* &middot; `cordis.layering`

Validate one patch document in full, or raise naming every problem.

### `wrote(resolution: Resolution, source: str, /) -> tuple[tuple[str, str], ...]`

*function* &middot; `cordis.layering`

Every ``(path, field)`` ``source`` is responsible for, sorted.

## Hot reload — a consequence of lifetime discipline, not a feature

Tier 3 &middot; [`hot-reload`](../spec/capabilities/17-hot-reload.yaml) &middot; 7 normative rules, 6 property cards

A plugin that watches source modules and config files and, on change, reimports the affected modules and restarts only the fibers whose code or configuration actually changed — possible only because unload is total.

### `HmrService(ctx: Context)`

*class* &middot; `cordis.hmr`

Reimports changed modules and has the loader rebuild what they reach.

### `RELOAD_FLAG`

*str*

### `ReloadFailure(module: str, error: BaseException)`

*class* &middot; `cordis.hmr`

A module whose new code would not import, and why.

### `ReloadReport(...)`

*class* &middot; `cordis.hmr`

What one reload did, in the same currency as a reconcile.

### `affected(...)`

*function* &middot; `cordis.hmr`

``changed`` and everything that reaches it through the import graph.

### `declines(name: str, /) -> bool`

*function* &middot; `cordis.hmr`

Whether ``name`` declared itself non-reloadable.

### `escalated(...)`

*function* &middot; `cordis.hmr`

Split the affected set into what may be reloaded and what refuses.

### `import_graph(...)`

*function* &middot; `cordis.hmr`

Every project module mapped to what it imports.

### `imports_of(name: str, /, *, root: Path | None = None) -> frozenset[str]`

*function* &middot; `cordis.hmr`

The project modules ``name``'s source imports, at any nesting depth.

### `project_module(path: Path | str, /, *, root: Path | None = None) -> str | None`

*function* &middot; `cordis.hmr`

The loaded module ``path`` is, or ``None`` if it is not one.

### `reload_order(...)`

*function* &middot; `cordis.hmr`

``names`` ordered so that a module follows everything it imports.

## Scoped registration — per-subject contributions in one process

Tier 3 &middot; [`scoped-registration`](../spec/capabilities/18-scoped-registration.yaml) &middot; 7 normative rules, 6 property cards

A pattern layered on context tagging and event filtering in which the context a contribution is registered from determines both who can see it and how long it lives, so many live subjects (sessions, agents, tenants) share one plugin tree without seeing each other's contributions.

### `SUBJECT_KEY`

*str*

### `Scope(key: object, parent: Scope | None, label: str)`

*class* &middot; `cordis.scope`

One subject's lifetime, and the context its contributions go through.

### `ScopedRegistry()`

*class* &middot; `cordis.scope`

A registry whose entries are visible down the scope chain.

### `admits(listener: Scope | None, carrier: Scope | None, /) -> bool`

*function* &middot; `cordis.scope`

Whether a registration at ``listener`` answers for ``carrier``.

### `create_scope(ctx: Context, key: object, /, *, label: str | None = None) -> Scope`

*function* &middot; `cordis.scope`

Start a subject under ``ctx``, and return its scope.

### `scope_target(base: Context, scope: Scope | None, /) -> Context`

*function* &middot; `cordis.scope`

``base``, as seen from inside ``scope``.

### `subject_of(ctx: Context, /) -> Scope | None`

*function* &middot; `cordis.scope`

The scope ``ctx`` registers and dispatches in, or ``None`` if unscoped.

## Capability seam — the three-role split that makes providers swappable

Tier 3 &middot; [`capability-seam`](../spec/capabilities/19-capability-seam.yaml) &middot; 7 normative rules, 5 property cards

The packaging discipline that makes the runtime's substitution machinery actually usable: a Definition owns the service name and its request/result types, Providers implement it, Consumers depend on it, and neither Provider nor Consumer ever depends on the other.

### `Candidate(key: str, item: T)`

*class* &middot; `cordis.seam`

An item that has passed validation, with the key it will be filed under.

### `Definition(ctx: Context)`

*class* &middot; `cordis.seam`

Base class for a capability contract.

### `Registry()`

*class* &middot; `cordis.seam`

Entries that are registered as effects and removed by their own scope.

### `RegistryChange(kind: ChangeKind, key: str, item: T)`

*class* &middot; `cordis.seam`

One transition of a registry's entries.

### `RegistryFailure(...)`

*class* &middot; `cordis.seam`

A listener that raised, and what it was being told at the time.

### `UNSET`

*_Unset* &middot; `cordis.seam`

### `resolve_spec(spec: type[T], raw: object = None, /, *, plugin: str | None = None) -> T`

*function* &middot; `cordis.seam`

Read ``raw`` as ``spec``, with every field resolved to a value.
