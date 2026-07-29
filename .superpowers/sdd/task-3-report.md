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

---

## Follow-up 2: remaining reviewer P1 — token-shaped secret leaks

**Status: Complete.** A re-review found that the repr sanitizers still relied
on a permissive "safe token" regex (`_SAFE_TOKEN_RE`), which let any
token-shaped string through. `TOKENONLYSECRET` matched that regex (and the
identifier regex used for `stable_name`/`source_id`), so it leaked verbatim in
`SourceFilter.value`, `SourceHandle`/`SourceStatus`/`ReloadResult.snapshot`,
`QueryBinding.stable_name`, and arbitrary SQL rendered as a `SourceFilter`
operator (the `Literal` alias is not enforced at runtime).

### Root cause

No permissive token-shaped regex can separate a benign string from a secret
(`TOKENONLYSECRET` is token-shaped yet secret). The fix abandons the
token-regex approach entirely for free-form fields.

### Changes

**`src/selayer/sources/base.py`**

- Replaced `_repr_token` / `_SAFE_TOKEN_RE` (the permissive "safe token"
  regex) with `_repr_literal`, which **redacts every string to a fixed
  `<redacted>` placeholder by default** — only non-string scalars (ints,
  bools, floats, `None`) pass through. Collections are projected element-wise.
  Applied to `SourceFilter.value`, all `snapshot` fields, `stable_name`, and
  `schema_fingerprint`.
- Split `_repr_id` into `_repr_source_name` (catalog source-name shape
  `[a-z][a-z0-9_]*` — the exact shape the catalog enforces, so only
  catalog-valid names render; `TOKENONLYSECRET` is rejected) for
  `source_id`/`connector`, and `_repr_column` (SQL-identifier shape) for
  physical columns.
- **`SourceFilter.operator` is now validated at construction** against the
  closed `_FILTER_OPERATORS` set; an out-of-set value raises a clean
  `ValueError("invalid SourceFilter operator")` (no hostile text in the
  message), so arbitrary SQL can never be stored or rendered.
- Removed the now-unused `_sanitize_location` import.

**`src/selayer/sources/errors.py`**

- `_safe_code` changed from a permissive `[a-z][a-z0-9_]*` regex to a
  **known-code allowlist** (`_KNOWN_CODES`); anything not in the set is
  coerced to `"unknown"` (`some_future_code`, `TOKENONLYCODE` → `unknown`).
- `_safe_source_id` regex tightened to the catalog source-name shape
  `[a-z][a-z0-9_]*`; token-shaped ids (`TOKENONLYSECRET`) render as
  `<source>`. `.source_id` and known `.code` properties preserved for the
  tests that read them (`orders`/`missing_profile`).
- `message`/args/cause/context guarantees unchanged (constant per-code
  message; raised outside `except` so `__cause__`/`__context__` are `None`).

**`tests/sources/test_adapter_contract.py`**

- 9 new hostile regression tests; 1 existing test updated.

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **68 passed** |
| `pytest -q` (full suite) | **1030 passed** |
| `ruff check src/selayer/sources tests/sources` | **All checks passed!** |
| `ruff format --check src/selayer/sources tests/sources` | **12 files already formatted** |
| `pyright src/selayer/sources tests/sources` | **0 errors, 0 warnings, 0 infos** |

### New / updated hostile regression tests

- **`TOKENONLYSECRET` in filter value / collection:**
  `test_source_filter_value_token_secret_is_redacted`,
  `test_source_filter_value_token_in_collection_is_redacted`.
- **Arbitrary SQL operator:**
  `test_source_filter_rejects_arbitrary_sql_operator` (asserts `ValueError`).
- **`TOKENONLYSECRET` in snapshots:**
  `test_source_handle_snapshot_token_secret_is_redacted`,
  `test_source_status_snapshot_token_secret_is_redacted`,
  `test_reload_result_snapshot_token_secret_is_redacted`.
- **`TOKENONLYSECRET` in stable name:**
  `test_query_binding_stable_name_token_secret_is_redacted`.
- **Untrusted error ids:** `test_source_error_token_source_id_rendered_as_source`
  (`source_id` → `<source>`), `test_source_error_token_code_coerced_to_unknown`.
- **Updated:** `test_source_error_unknown_code_is_coerced_to_unknown` (was
  `…_unknown_code_uses_fallback_message`; now asserts `.code == "unknown"`
  under the allowlist).

### Public interfaces preserved

`SourceFilter`, `SourceHandle`, `SourceStatus`, `ReloadResult`,
`QueryBinding`, and `SourceError` constructors and stored fields are unchanged
— sanitization is repr-only. `SourceError(...)` signature unchanged. The only
behavioural change is that an invalid `SourceFilter` operator now raises
`ValueError` at construction (documenting the closed-set constraint the
`Literal` alias already implied).

---

## Follow-up 3: final filter-contract fix — structured scan requirements

**Status: Complete.** The final re-review found that the Follow-up 2 repr
sanitizers redacted hostile values but still *stored* them: a SQL fragment
(`"id; DROP TABLE users--"`) was accepted as a `SourceFilter.column` / scan
requirement column and only placeholder-ed in the repr, and a raw string was
accepted as a scan-requirement filter. A token-shaped uppercase secret
(`TOKENONLYSECRET`) was likewise accepted as a column and merely redacted.
This final fix enforces the structured contract at **construction time** so
no arbitrary SQL can ever be carried as a planned column or filter, and
tightens the column repr to the catalog source-name shape.

### Changes

**`src/selayer/sources/base.py`**

- **`SourceFilter.__post_init__`** now validates `column` is a string SQL
  identifier (via `_SQL_IDENT_RE`) at construction; a non-string or a SQL
  fragment (`"id; DROP TABLE users--"`) raises `ValueError("invalid
  SourceFilter column")`. The check runs against an untyped local so the
type checker does not narrow it away as impossible.
- **`SourceScanRequirement.__post_init__`** now validates every column is a
  string SQL identifier (raises `ValueError`) and every filter is an actual
  `SourceFilter` instance (a raw SQL string raises a clean `TypeError`), so
  arbitrary SQL can never be stored as a planned column or filter.
- **`_repr_source_name`** switched from the SQL-identifier regex to the
  stricter catalog source-name regex (`_SOURCE_NAME_RE`, lowercase
  `[a-z][a-z0-9_]*`). A token-shaped uppercase secret column such as
  `TOKENONLYSECRET` is a syntactically valid identifier and therefore
  accepted at construction, yet it is redacted in the repr so it can never
  surface in diagnostics. Legitimate lowercase columns (`id`, `amount`)
  render unchanged.
- **`_repr_literal`** now also redacts `bytes`, `dict`, `set`, and
  `frozenset` wholesale (their members may each be a secret); ordered
  collections (`tuple`/`list`) are still projected element-wise so bare
  numeric literals stay visible.

**`tests/sources/test_adapter_contract.py`**

- 6 new hostile regression tests; 2 existing tests rewritten from
  "placeholdered" to "rejected".

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **75 passed** |
| `pytest -q` (full suite) | **1037 passed** |
| `ruff check src/selayer/sources tests/sources` | **All checks passed!** |
| `ruff format --check src/selayer/sources tests/sources` | **12 files already formatted** |
| `pyright src/selayer/sources tests/sources` | **0 errors, 0 warnings, 0 infos** |

### New / updated hostile regression tests

- **`SourceFilter` rejects hostile / non-string column (construction):**
  `test_source_filter_rejects_hostile_column`,
  `test_source_filter_rejects_non_string_column`.
- **`SourceFilter` token-shaped column redacted in repr:**
  `test_source_filter_token_shaped_column_is_redacted` (replaces the old
  `_column_sql_is_placeholdered`).
- **`SourceScanRequirement` rejects hostile column (construction):**
  `test_scan_requirement_rejects_hostile_column` (replaces the old
  `_hostile_columns_are_placeholdered`).
- **`SourceScanRequirement` token-shaped column redacted in repr:**
  `test_scan_requirement_token_shaped_column_is_redacted`.
- **`SourceScanRequirement` rejects raw-string filter (construction):**
  `test_scan_requirement_rejects_raw_string_filter`.
- **Collection-value repr hardening:**
  `test_source_filter_dict_value_is_redacted`,
  `test_source_filter_set_value_is_redacted`,
  `test_source_filter_numeric_tuple_value_renders_safely`.

### Public interfaces preserved

`SourceFilter` and `SourceScanRequirement` constructors and stored fields are
unchanged. The only behavioural change is that an invalid column or
non-`SourceFilter` filter now raises at construction (`ValueError` /
`TypeError`), documenting the structured-input contract the types already
implied. All other lifecycle objects are untouched.

### Commit

```
fix(sources): enforce structured scan requirements
```

Files staged: `src/selayer/sources/base.py`,
`tests/sources/test_adapter_contract.py`.

---

## Follow-up 4: harden scan repr containers and generator materialization

**Status: Complete.** A final re-review found two residual gaps in the
Follow-up 2/3 sanitizers:

1. **`_repr_literal` only redacted concrete `dict`/`set`/`frozenset`.** A
   `types.MappingProxyType`, `collections.UserDict`, or any hand-rolled
   `collections.abc.Mapping`/`Set` subclass is *not* a `dict`/`set`/`frozenset`,
   so it fell through to `return value` and surfaced its own (possibly leaky)
   `__repr__`. Likewise any opaque object type not in the allowlist
   (`int`/`float`/`None`/`tuple`/`list`/`str`/`bytes`/dict-family) reached the
   bare `return value`, so an arbitrary handle's `__repr__` could leak.
2. **`SourceScanRequirement.__post_init__` could silently store an empty
   tuple.** The validation loop iterated `self.columns`/`self.filters`, then a
   *separate* `tuple(self.columns)` coerced the same iterable. A single-pass
   generator was exhausted by the validation pass, so the coercion stored
   `()` — silently dropping a hostile column that the loop had already
   validated against (the rejection raised, but for an accepted generator the
   stored tuple was wrong).

### Changes

**`src/selayer/sources/base.py`**

- **`_repr_literal`** now redacts every `collections.abc.Mapping` and
  `collections.abc.Set` implementation (ABC checks catch `MappingProxyType`,
  `UserDict`, and hand-rolled mappings/sets in addition to concrete
  `dict`/`set`/`frozenset`). It keeps passing through non-string scalars
  (`int`, `float`, `None`) and `enum.Enum` members, projects `tuple`/`list`
  element-wise, redacts `str`/`bytes` to `<redacted>`, and — as the final
  default — redacts **every other object type** so an opaque handle's own
  `__repr__` can never leak a secret.
- **`SourceScanRequirement.__post_init__`** materializes the caller's
  `columns`/`filters` iterables into immutable local tuples *once*, before
  validation, then validates and stores those locals. A single-pass generator
  is now consumed exactly once: validation and storage share the same items,
  and a hostile generator is rejected before any storage happens.

**`tests/sources/test_adapter_contract.py`**

- 9 new regression tests (3 generator-materialization, 6 repr-container).

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **84 passed** |
| `pytest -q` (full suite) | **1046 passed** |
| `ruff check src/selayer/sources tests/sources` | **All checks passed!** |
| `ruff format --check src/selayer/sources tests/sources` | **12 files already formatted** |
| `pyright src/selayer/sources tests/sources` | **0 errors, 0 warnings, 0 infos** |

### New regression tests

- **Generator materialization (scan requirement):**
  `test_scan_requirement_accepts_column_generator`,
  `test_scan_requirement_accepts_filter_generator`,
  `test_scan_requirement_generator_validation_runs_before_storage` (a hostile
  generator is rejected and the materialized tuple discarded rather than
  stored as `()`).
- **Custom Mapping/Set and unknown objects redacted in repr:**
  `test_source_filter_mappingproxy_value_is_redacted`,
  `test_source_filter_userdict_value_is_redacted`,
  `test_source_filter_custom_mapping_value_is_redacted`,
  `test_source_filter_custom_set_value_is_redacted`,
  `test_source_filter_unknown_object_value_is_redacted`,
  `test_source_filter_scalar_and_tuple_still_render` (regression guard that
  safe scalars and ordered collections still project).

### Public interfaces preserved

`SourceFilter` and `SourceScanRequirement` constructors and stored fields are
unchanged — the only changes are repr-only redaction widening and
pre-storage generator materialization. No other lifecycle object is touched.

### Commit

```
fix(sources): harden scan repr containers
```

Files staged: `src/selayer/sources/base.py`,
`tests/sources/test_adapter_contract.py`.

---

## Follow-up 5: close scalar repr bypass — exact builtin types only

**Status: Complete.** A final re-review found two residual repr leaks in
`_repr_literal`:

1. **`Enum` members passed through verbatim.** The Follow-up 4 hardening
   redacted every unknown object type, but it still explicitly returned
   `Enum` members unchanged (`if isinstance(value, Enum): return value`).
   An Enum member whose own `__repr__` leaks a secret surfaced in
   diagnostics.
2. **`isinstance(value, (int, float))` accepted subclasses.** An
   `int`/`float` subclass with a custom leaky `__repr__` satisfied the
   `isinstance` guard and rendered its own string — a subclass scalar
   bypass identical in shape to the Follow-up 4 unknown-object bypass it
   was meant to close.

### Root cause

`isinstance` cannot distinguish a benign builtin scalar from a subclass whose
`__repr__` is hostile. The only way to permit a known-safe scalar while
rejecting all subclasses is an **exact-type** identity check (`type(value)
is int`), not an `isinstance` membership test.

### Changes

**`src/selayer/sources/base.py`**

- **`_repr_literal`** no longer passes `Enum` members through; the
  `isinstance(value, Enum)` branch is removed, so an Enum member whose own
  `__repr__` leaks a secret is redacted by the final default. (The unused
  `Enum` import is dropped; `StrEnum` remains for `SourceHealth`.)
- **Scalar pass-through** switched from `isinstance(value, (int, float))
  or value is None` to `value is None or type(value) in {int, float, bool}`.
  Exact-type identity (`type(value)`) defeats any `int`/`float` subclass
  whose custom `__repr__` would leak. `bool` is included explicitly because
  `type(True) is bool` (a distinct type from `int`), so it must be covered
  by the set membership; `isinstance(True, int)` is `True` but the exact-type
  check would otherwise reject bare booleans.
- Everything else is unchanged: `str`/`bytes`, `Mapping`/`AbstractSet`, and
  all other object types are redacted; `tuple`/`list` are projected
  element-wise (so a tuple containing a leaky Enum/scalar-subclass member is
  element-redacted through the same exact-type guard).

**`tests/sources/test_adapter_contract.py`**

- 5 new regression tests (2 hostile-Enum, 2 scalar-subclass, 1
  regression guard for exact scalars).

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **89 passed** |
| `pytest -q` (full suite) | **1051 passed** |
| `ruff check src/selayer/sources/base.py tests/sources/test_adapter_contract.py` | **All checks passed!** |
| `ruff format --check src/selayer/sources/base.py tests/sources/test_adapter_contract.py` | **2 files already formatted** |
| `pyright src/selayer/sources/base.py tests/sources/test_adapter_contract.py` | **0 errors, 0 warnings, 0 infos** |

### New regression tests

- **Enum member bypass:** `test_source_filter_enum_member_value_is_redacted`
  (a `_LeakyEnum` whose `__repr__` interpolates `TOKENONLYSECRET` is
  redacted), `test_source_filter_enum_member_in_tuple_is_redacted`
  (element-wise projection of a tuple also redacts the Enum member while the
  safe numeric element still renders).
- **Scalar-subclass bypass:**
  `test_source_filter_int_subclass_value_is_redacted`,
  `test_source_filter_float_subclass_value_is_redacted` (`_LeakyInt` /
  `_LeakyFloat` with hostile `__repr__` are redacted under the exact-type
  guard).
- **Exact-scalar regression guard:**
  `test_source_filter_exact_scalars_still_render` (bare `int`, `float`,
  `bool`, and `None` still render unchanged — `bool` covered explicitly
  since `type(True) is bool`, not `int`).

### Public interfaces preserved

`_repr_literal` is an internal helper; no public constructor, stored field,
or signature changed. `SourceFilter.value` is still stored verbatim — the
fix is repr-only, identical in scope to the prior follow-ups. `SourceHealth`
continues to be a `StrEnum`; it is rendered through other repr paths
(`SourceStatus.health`), not `_repr_literal`, so its display is unaffected.

### Commit

```
fix(sources): close scalar repr bypass
```

Files staged: `src/selayer/sources/base.py`,
`tests/sources/test_adapter_contract.py`.

---

## Follow-up 6: normalize string contract values — exact builtin str only

**Status: Complete.** The final re-review found that every identifier render
helper and SourceError storage helper used `isinstance(value, str)`, which a
hostile `str` subclass satisfies. A `str` subclass with a custom `__repr__`
(for example one that interpolates `TOKENONLYSECRET`) therefore leaked through
`_repr_source_name`/`_repr_column` (returned the subclass instance, rendered by
`_render`'s `!r`), through `SourceFilter.operator` (passed directly to
`_render`), and through `SourceError` (`_safe_source_id`/`_safe_code` stored the
subclass, then `__repr__` invoked it). This is the same class of bypass
Follow-up 5 closed for `int`/`float`/`bool` scalars — now closed for strings.

### Root cause

`isinstance(value, str)` cannot distinguish a benign builtin `str` from a
subclass whose `__repr__` is hostile. The only safe guard is an exact-type
identity check (`type(value) is str`).

### Changes

**`src/selayer/sources/base.py`**

- **`_repr_source_name` / `_repr_column`**: `isinstance(value, str)` →
  `type(value) is str`. A hostile subclass no longer matches, so it is
  placeholder-ed (`<redacted>`) rather than returned for `_render` to render
  via its leaky `__repr__`.
- **`SourceFilter.__post_init__`**: `column` and `operator` checks switched to
  `type(...) is str`. A hostile subclass column/operator is rejected at
  construction (`ValueError`) so it can never be stored or rendered.
- **`SourceScanRequirement.__post_init__`**: column check switched to
  `type(column) is str`; a hostile subclass column is rejected at construction.
- Module-level and per-helper docstrings updated to document the exact-builtin-
  str rationale.

**`src/selayer/sources/errors.py`**

- **`_safe_code` / `_safe_source_id`**: `isinstance(..., str)` → `type(...) is
  str`. A hostile subclass code/source_id is coerced to `"unknown"` / `"<source>"`
  (a plain builtin `str`) rather than stored as the subclass and later rendered
  via its leaky `__repr__`.
- `_validated_operation_id` was already safe: `str(parsed)` always returns a
  plain builtin `str`; no change needed (covered by a new regression test).

**`tests/sources/test_adapter_contract.py`**

- 15 new hostile regression tests; 1 regression guard added.

### Validation gates

| Gate | Result |
| --- | --- |
| `pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py` | **103 passed** |
| `pytest -q` (full suite) | **1065 passed** |
| `ruff check src/selayer/sources/base.py src/selayer/sources/errors.py tests/sources/test_adapter_contract.py` | **All checks passed!** |
| `ruff format --check ...` (same scope) | **3 files already formatted** |
| `pyright ...` (same scope) | **0 errors, 0 warnings, 0 informations** |

### New regression tests

- **SourceFilter rejects hostile subclass column/operator (construction):**
  `test_source_filter_rejects_str_subclass_column`,
  `test_source_filter_rejects_str_subclass_operator`.
- **SourceScanRequirement rejects hostile subclass column (construction):**
  `test_scan_requirement_rejects_str_subclass_column`.
- **SourceFilter value hostile subclass redacted:**
  `test_source_filter_value_str_subclass_is_redacted`.
- **Identifier hostile subclass redacted in repr:** SourceHandle
  source_id/connector, SourceStatus source_id, ReloadResult source_id,
  QueryBinding source_id.
- **SourceError hostile subclass coerced:** source_id → `<source>`, code →
  `unknown`, operation_id → plain builtin str (canonical UUIDv4).
- **Regression guards:** exact-builtin-string identifiers and error fields
  still render / are retained unchanged.

### Public interfaces preserved

No public constructor, stored field, or signature changed. Sanitization is
repr-only for identifier fields (SourceHandle/Status/ReloadResult/QueryBinding);
construction now rejects hostile subclass columns/operators (SourceFilter/
SourceScanRequirement) and coerces hostile subclass error args (SourceError).

### Commit

```
fix(sources): normalize string contract values
```

Files staged: `src/selayer/sources/base.py`,
`src/selayer/sources/errors.py`,
`tests/sources/test_adapter_contract.py`,
`.superpowers/sdd/task-3-report.md`.
