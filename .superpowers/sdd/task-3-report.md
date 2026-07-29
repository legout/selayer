# Task 3 Report — Runtime profiles, sanitized errors, and adapter contracts

## Status

**Complete.** All scoped files implemented, all four validation gates green,
committed with the brief's Task 3 message.

One defect was found during finalization and fixed (see
[Finalization fix](#finalization-fix)).

## Scope

Only Task 3 files were created:

- `src/selayer/sources/profiles.py` — opaque `RuntimeProfile`, resolver map,
  and the two structural protocols
- `src/selayer/sources/errors.py` — sanitized `SourceError` hierarchy with
  UUIDv4 operation ids
- `src/selayer/sources/base.py` — immutable lifecycle value objects and the
  private `SourceAdapter` protocol
- `tests/sources/test_profiles.py` — 13 tests
- `tests/sources/test_adapter_contract.py` — 20 tests

No other files (catalog, config, schema, model, query, OKF) were modified.

## Validation gates

| Gate                                          | Result                            |
| --------------------------------------------- | --------------------------------- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **33 passed**          |
| `pytest -q` (full suite)                      | **995 passed** in 2.79s          |
| `ruff check src/selayer/sources tests/sources` | **All checks passed!**          |
| `ruff format --check src/selayer/sources tests/sources` | **12 files already formatted** |
| `pyright src/selayer/sources tests/sources`   | **0 errors, 0 warnings, 0 infos** |

## Interfaces produced (exact names from the brief)

**profiles.py:**

- `ArrowObject` — PEP 695 `type` alias over `padataset.Dataset | padataset.Scanner | pa.Table | pa.RecordBatchReader`
- `RuntimeProfile(name, _values)` — frozen+slotted; `_values` is `repr=False`
  and snapshotted into a `MappingProxyType` over a private dict copy
- `MappingProfileResolver(profiles)` — defensively copies the input map into
  per-entry `RuntimeProfile`s; `resolve(name, *, source_id)` raises a sanitized
  `SourceProfileError` (code `"missing_profile"`) **outside** any `except` scope
- `RuntimeProfileResolver` — `@runtime_checkable` `Protocol`:
  `resolve(name: str, *, source_id: str) -> RuntimeProfile`
- `ArrowProviderResolver` — `@runtime_checkable` `Protocol`:
  `resolve(handle: str, *, source_id: str) -> Callable[[], ArrowObject]`

**errors.py:**

- `new_operation_id() -> str` — fresh UUIDv4 string
- `SourceError(source_id, code, message, *, operation_id=None)` — base; stores
  `operation_id`, `source_id`, `code`, constant `message`; never retains driver
  exceptions; sanitized `__repr__`
- `SourceDependencyError(SourceError)` — profile/provider resolution failure
- `SourceProfileError(SourceDependencyError)` — missing profile
- `SourceConnectionError(SourceError)` — connection failures
- `SourceSchemaError(SourceError)` — schema inspection/mismatch
- `SourceReloadError(SourceError)` — reload failures

**base.py:**

- `SourceHealth(StrEnum)` — `READY` / `STALE` / `UNHEALTHY`
- `SourceFilterOperator` — PEP 695 `Literal` of nine symbolic operators
  (`eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `is_null`, `is_not_null`)
- `SourceFilter(column, operator, value)` — frozen+slotted; repr via `_format_repr`
- `SourceScanRequirement(columns, filters=())` — frozen+slotted; coerces
  iterables to tuples; carries only structured filters (no raw SQL)
- `SourceHandle(source_id, connector, resource, schema, snapshot=None, query_scoped=False, cleanup=None)` — frozen+slotted; `resource`, `schema`, `cleanup`
  are `repr=False` and absent from the custom `__repr__`
- `SourceStatus(source_id, connector, generation, schema_fingerprint, snapshot, health)` — frozen+slotted;
  `from_handle(handle, generation, *, health=READY)` computes the fingerprint
- `ReloadResult(source_id, old_generation, new_generation, schema_fingerprint, snapshot)` — frozen+slotted
- `QueryBinding(source_id, stable_name, cleanup)` — frozen+slotted; context
  manager invoking `cleanup` exactly once (idempotent closure); cleanup hidden
  from repr
- `SourceAdapter` — `@runtime_checkable` `Protocol` with the exact brief
  signatures:

  ```text
  prepare(source, profiles, arrow_providers) -> SourceHandle
  inspect_schema(handle) -> TableSchema
  register(connection, stable_name, handle) -> None
  bind_query(handle, requirement) -> QueryBinding | None
  close(handle) -> None
  ```

## Brief-contract verification

| Brief requirement (Step) | Implementation | Tests |
| --- | --- | --- |
| **S4** `RuntimeProfile` defensive copy into `MappingProxyType`, `repr=False`, single `value()` accessor | `__post_init__` snapshots into `MappingProxyType(dict(...))`; `_values = field(repr=False)` | `test_runtime_profile_never_reprs_secret_values`, `_defensive_copy_isolates_original`, `_repr_exposes_only_name`, `_value_missing_raises_keyerror` |
| **S4** `MappingProfileResolver` copies its map; unknown → `SourceProfileError` outside `except` | snapshot into `RuntimeProfile` per entry, `MappingProxyType` over map; raised with no active handler | `test_missing_profile_has_safe_domain_error` (+ `__cause__`/`__context__` is `None`), `_defensively_copies_profile_map` |
| **S4** `RuntimeProfileResolver` / `ArrowProviderResolver` protocols | `@runtime_checkable Protocol` with exact signatures | protocol-acceptance tests for both |
| **S4** `ArrowObject` = Dataset \| Scanner \| Table \| RecordBatchReader | PEP 695 `type` alias | exercised in `_FakeArrowResolver` |
| **S5** `operation_id` UUIDv4, `source_id`, `code`, constant message | `new_operation_id()` defaults `operation_id`; all fields stored | `test_source_error_carries_uuidv4_operation_id_and_safe_fields`, `_explicit_operation_id_is_honored` |
| **S5** no retained driver exception; raise outside `except` → `__cause__`/`__context__` `None` | documented + the driver-exception-is-swallowed test shape | `test_source_error_raised_clean_has_no_cause_or_context`, `_does_not_retain_driver_exception` |
| **S5** error hierarchy | `SourceDependencyError` ⊂ `SourceError`, `SourceProfileError` ⊂ `SourceDependencyError`, etc. | `test_source_error_hierarchy_subclasses_base` |
| **S6** `resource`/`cleanup` `repr=False`; `SourceStatus`/`ReloadResult` carry only IDs/generation/fingerprint/snapshot/health | custom `__repr__` via `_format_repr` excludes them; fields are safe identifiers only | `test_handle_..._excludes_sensitive_fields_from_repr`, `test_status_repr_has_no_resource_or_schema`, `test_reload_result_is_immutable_and_safe` |
| **S6** `SourceAdapter` exact five methods | `Protocol` with exact param/return types | `test_fake_adapter_satisfies_protocol_without_cast`, `_methods_are_usable` |
| **S6** `QueryBinding` context-managed cleanup, idempotent, hidden from repr | `__post_init__` wraps in `done`-guarded closure; `__enter__`/`__exit__`; `repr=False` | `test_query_binding_runs_cleanup_on_context_exit`, `_cleanup_is_idempotent`, `_repr_excludes_cleanup_and_does_not_invoke_it` |
| **S6** `SourceScanRequirement` ordered columns + structured filters, no raw SQL | tuple columns + `SourceFilter` tuple; `Literal` operators | `test_scan_requirement_carries_structured_filters_no_raw_sql`, `_coerces_iterables_to_tuples` |

The brief's two named tests (`test_runtime_profile_never_reprs_secret_values`,
`test_missing_profile_has_safe_domain_error` in Step 1; `test_handle_and_status_are_immutable_and_safe`
in Step 2) are present verbatim and passing.

## Secrecy model

Three layers keep credentials out of every observable surface, and each is
covered by tests:

1. **Opaque profiles.** `RuntimeProfile` copies the caller's mapping into a
   private `MappingProxyType` and exposes only `value(name)`; the mapping is
   `repr=False` so secrets never surface in diagnostics even if a profile value
   is a credential.
2. **Sanitized errors.** Driver exceptions are never stored; errors are
   constructed and raised outside `except` scopes so `__cause__`/`__context__`
   stay `None`, and the only stored text is a constant `message` of safe
   identifiers. A fresh UUIDv4 `operation_id` correlates without retaining
   driver state.
3. **Safe reprs.** Every lifecycle value object routes its string-bearing
   fields through the centralized `_format_repr` → `_safe` → `_sanitize_location`
   sanitizer ( userinfo redaction), so `snapshot`/`source_id`/etc. embedded
   URIs cannot leak. Resource objects, schemas, and cleanup callbacks are
   `repr=False` and absent from custom reprs.

## Finalization fix

The worktree arrived with all 33 scoped tests passing and Ruff clean, but
Pyright reported one error that blocked the brief's "all commands exit zero"
requirement:

```
tests/sources/test_profiles.py:57 - error: Cannot assign to attribute "name"
  for class "RuntimeProfile" — Attribute "name" is read-only
```

`test_runtime_profile_is_immutable` exercised immutability via direct attribute
assignment (`profile.name = "other"`) inside `pytest.raises(AttributeError)`.
Pyright statically rejects direct writes to frozen-dataclass attributes even
though the assignment raises at runtime.

The fix had to satisfy two conflicting static checks: Ruff **B009** flags
constant-attribute `setattr(x, "attr", v)` calls and auto-reverts them to
`x.attr = v` (which reintroduces the Pyright error). The established project
resolution — already used in `test_schema.py` (variable attribute) and the
sibling `test_adapter_contract.py` — is the `__setattr__` dunder method, which
trips neither check. The test now reads:

```python
with pytest.raises(AttributeError):
    profile.__setattr__("name", "other")
```

After the fix: focused 33/33, full 995/995, Ruff check clean, Ruff format
clean, Pyright 0 errors.

## Remaining issues / concerns

- **Snapshot sanitization is structural, not enforced.** `SourceHandle.snapshot`
  and `SourceStatus.snapshot` are opaque `str | None` rendered through
  `_format_repr` → `_safe` (userinfo redaction). The secrecy guarantee relies
  on Task 4 adapters emitting already-sanitized snapshot strings (e.g. a
  `file-set:<generation>` token) rather than raw driver URIs. This matches the
  brief's "safe snapshot/version" wording; worth confirming in Task 4.
- **`SourceHealth` is defined but unused in Task 3.** It is consumed by
  `SourceStatus.from_handle` (defaults to `READY`) and reserved for the reload
  path; no behavioural coverage of `STALE`/`UNHEALTHY` yet. Expected — those
  states are exercised by the reload lifecycle (Task 4).
- **No `__init__.py` re-exports.** `selayer.sources` does not re-export the new
  symbols; callers import from the submodules directly (matching the existing
  `catalog`/`config`/`schema` convention). Consistent with the package style;
  no change needed.

## Commit

```
feat(sources): define adapter lifecycle contracts
```

Files staged: `src/selayer/sources/profiles.py`,
`src/selayer/sources/errors.py`, `src/selayer/sources/base.py`,
`tests/sources/test_profiles.py`, `tests/sources/test_adapter_contract.py`.

---

## Follow-up: Task 3 reviewer P1 fixes

**Status: Complete.** All reviewer P1 findings fixed; every validation gate
green; scoped follow-up commit.

### Findings addressed

1. **`SourceError` leaked arbitrary caller/driver text.** `message`,
   `source_id`, `code`, and `operation_id` were stored verbatim. Fixed:
   - `message` → normalized to a *constant per-code* generic message via a
     `_CODE_MESSAGES` lookup (the caller-supplied message is discarded);
     unknown codes fall back to `"a source lifecycle error occurred"`.
   - `source_id` → validated against the SQL-identifier shape, else coerced
     to `"<source>"`.
   - `code` → validated against `[a-z][a-z0-9_]*`, else `"unknown"` (exact
     safe codes retained).
   - `operation_id` → validated as a UUIDv4 (normalized to canonical form);
     any non-UUIDv4 value (including non-v4 UUIDs) is replaced with a fresh
     UUIDv4.
   - Errors are still constructed/raised outside `except` scopes so
     `__cause__`/`__context__` stay `None`.

2. **`MappingProfileResolver` interpolated untrusted names.** The missing-
   profile message no longer interpolates `name`/`source_id`; it passes a
   constant string (ignored by `SourceError` anyway).

3. **No concrete Arrow-provider resolver.** Added
   `MappingArrowProviderResolver` (defensively-copied provider map) raising a
   sanitized `SourceDependencyError` (code `missing_arrow_provider`) for
   unknown handles, outside any `except` scope. The `ArrowProviderResolver`
   protocol's exact `resolve(handle, *, source_id)` signature is unchanged.

4. **Value-object reprs leaked SQL / URIs / handles / resources / cleanup.**
   Added repr-only sanitizers in `base.py` (stored values unchanged so
   adapters keep working): identifier fields (`source_id`, `connector`,
   `column`, `stable_name`) are validated and placeholder-ed; free-form
   fields (`value`, `snapshot`, `schema_fingerprint`) are URI-userinfo-redacted
   via the existing config redactor (`_sanitize_location`) then placeholder-ed
   if not a safe token. Resources/schemas/cleanup remain `repr=False`.

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **59 passed** |
| `pytest -q` (full suite) | **1021 passed** |
| `ruff check src/selayer/sources tests/sources` | **All checks passed!** |
| `ruff format --check src/selayer/sources tests/sources` | **12 files already formatted** |
| `pyright src/selayer/sources tests/sources` | **0 errors, 0 warnings, 0 infos** |

### New hostile regression tests (26 added)

- **Driver-text/error args:** `ignores_arbitrary_driver_message`,
  `sanitizes_hostile_source_id`, `rejects_arbitrary_code`,
  `invalid_operation_id_replaced_with_uuidv4`,
  `non_v4_operation_id_replaced_with_uuidv4`,
  `arbitrary_args_never_leak_in_repr_or_str`, `unknown_code_uses_fallback_message`.
- **Missing arrow handle:** `mapping_arrow_provider_resolver_*`
  (returns_provider, satisfies_protocol, missing_handle_raises_sanitized,
  missing_does_not_interpolate_handle, missing_has_uuidv4_operation_id,
  defensively_copies_map) and `missing_profile_error_does_not_interpolate_hostile_name`.
- **SQL/secret/handle in reprs:** filter value credential-URI / SQL-fragment /
  key=value / tuple-element redaction, filter column placeholdering, scan
  requirement hostile-column placeholdering, handle/status/reload-result
  credential-snapshot redaction + resource/schema/cleanup exclusion,
  QueryBinding hostile stable_name + cleanup-not-invoked.

### Public interfaces preserved

`SourceError(source_id, code, message, *, operation_id=None)` signature
unchanged (the `message` param is kept for interface compatibility but its
value is discarded). All dataclass fields/signatures unchanged. Two existing
tests updated to assert the new constant message and UUIDv4-honored operation
id.
