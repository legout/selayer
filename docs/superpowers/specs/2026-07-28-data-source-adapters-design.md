# Data Source Adapters, Schema Contracts, and Reload Design

## Purpose

Replace selayer's eager, Polars-only source loading with a schema-validated source subsystem that can register local files, object-store data, lakehouse tables, databases, and programmatic Arrow datasets in DuckDB without requiring callers to rebuild `QueryEngine` when a source changes.

This design rewrites the unreleased schema-version-1 data-source contract. It supersedes only the source declaration and loading portions of the grain-aware query-planning design. Grain semantics, expression validation, query planning, and DuckDB compilation remain unchanged.

## Goals

1. Require a complete, executable physical schema for every source.
2. Permit the schema inline or through a reusable `schema_ref`, resolving both forms into one immutable model.
3. Preserve DuckDB projection and filter pushdown into reusable PyArrow Datasets.
4. Support Parquet, CSV, Delta Lake, Apache Iceberg, SQLite, DuckDB files, PostgreSQL, S3-backed locations, and programmatic PyArrow sources.
5. Use `deltalake` and `pyiceberg` for lakehouse snapshot semantics rather than DuckDB's evolving Delta and Iceberg extensions.
6. Register and reload sources without eager full-table Polars materialization.
7. Provide explicit, atomic `reload_source()` and all-or-nothing `reload_all()` operations.
8. Keep credentials outside catalogs, schemas, OKF bundles, logs, plans, and errors.
9. Keep planning connection-free and source-adapter implementation details private.
10. Generate advisory source-schema summaries for OKF without making OKF executable authority.

## Non-goals

- Writing to any source.
- Automatic polling, filesystem watching, or snapshot subscriptions.
- Arbitrary SQL as a source declaration.
- A public third-party adapter plugin protocol in the first implementation.
- Persistent runtime schema or data caches.
- Replacing the DuckDB execution engine.
- Letting OKF override source schemas, connector settings, or registration behavior.
- Embedding secrets or environment-variable interpolation in catalog YAML.
- Preserving compatibility with the current unreleased `type`/`path`/`grain`-only source declarations.

## Capability assumptions

The design relies on documented integration behavior:

- DuckDB can query registered Arrow tables, datasets, scanners, and record-batch readers; projection and filter pushdown are supported for Arrow Dataset scans: <https://duckdb.org/docs/current/guides/python/sql_on_arrow>.
- `deltalake.DeltaTable.to_pyarrow_dataset()` exposes a Delta snapshot as a PyArrow Dataset: <https://delta-io.github.io/delta-rs/api/delta_table/>.
- PyIceberg tables support metadata refresh and scans with selected fields, row filters, and Arrow batch readers: <https://py.iceberg.apache.org/reference/pyiceberg/table/>.
- DuckDB provides mature SQLite and PostgreSQL attach/scan integrations: <https://duckdb.org/docs/current/guides/database_integration/sqlite> and <https://duckdb.org/docs/current/core_extensions/postgres/overview>.

These are implementation inputs, not public selayer guarantees. Connector integration tests define selayer's actual compatibility contract.

## Authority model

The resolved selayer catalog is the sole executable authority for:

- source connector configuration;
- complete logical schemas;
- source grains;
- semantic fields and relationships;
- runtime profile names.

Runtime introspection verifies catalog declarations but never silently replaces them. Inference is a tooling aid that may generate a candidate schema document for review.

OKF may contain generated, bounded schema and grain summaries plus provenance, explanations, and caveats. Such content is advisory. A conflict is reported, and the catalog remains authoritative.

## Module architecture

Add an internal `selayer.sources` module between the catalog and `QueryEngine`.

```text
Validated SemanticLayer
        |
        v
  SourceRegistry <---- RuntimeProfileResolver
        |
        +---- ArrowDatasetAdapter: parquet, csv, delta, pyarrow
        +---- IcebergAdapter: pyiceberg metadata + query-scoped readers
        +---- NativeDatabaseAdapter: sqlite, duckdb, postgres
        |
        v
 In-memory DuckDB connection
```

### Module responsibilities

- `catalog` parses connector declarations and schema documents into immutable objects.
- `sources.schema` parses recursive logical types, fingerprints schemas, and compares declared and observed schemas.
- `sources.registry` owns active handles, DuckDB registrations, generations, reload coordination, and cleanup.
- `sources.adapters` contains private connector adapters.
- `QueryEngine` delegates source lifecycle operations to `SourceRegistry`; it continues to orchestrate plan, compile, execute, and return Polars results.
- `planning` receives only the resolved `SemanticLayer` and remains independent of DuckDB, Arrow, credentials, and connector libraries.

The deletion test for `SourceRegistry` is deliberate: without it, registration, schema verification, reload rollback, profile resolution, and cleanup would spread through `QueryEngine` and every connector.

## Catalog schema-version-1 rewrite

Catalog `version` remains exactly built-in integer `1`. Because no catalog release exists, the source shape is replaced directly rather than introducing compatibility behavior or version 2.

Each source declares:

- stable semantic source identifier;
- connector discriminator `type`;
- connector-specific, non-secret configuration;
- exactly one of `schema` or `schema_ref`;
- non-empty `grain`.

### File and Delta example

```yaml
version: 1
name: analytics

data_sources:
  orders:
    type: parquet
    location: s3://analytics-prod/orders/
    credential_profile: analytics_s3
    schema_ref: schemas/orders.yaml
    grain: [id]

  order_items:
    type: delta
    location: s3://analytics-prod/order_items/
    credential_profile: analytics_s3
    schema:
      fields:
        - name: order_id
          type: int64
          nullable: false
        - name: product_id
          type: int64
          nullable: false
        - name: quantity
          type: int32
          nullable: false
        - name: total
          type:
            decimal:
              precision: 18
              scale: 2
          nullable: false
    grain: [order_id, product_id]
```

### Database example

```yaml
data_sources:
  customers:
    type: postgres
    connection_profile: warehouse_readonly
    relation: analytics.customers
    schema_ref: schemas/customers.yaml
    grain: [id]

  reference_codes:
    type: sqlite
    location: data/reference.sqlite
    relation: codes
    schema_ref: schemas/reference_codes.yaml
    grain: [code]
```

### Iceberg example

```yaml
data_sources:
  events:
    type: iceberg
    catalog_profile: production_iceberg
    namespace: [analytics]
    table: events
    schema_ref: schemas/events.yaml
    grain: [event_id]
```

### Programmatic Arrow example

```yaml
data_sources:
  live_features:
    type: pyarrow
    handle: live_features_provider
    schema_ref: schemas/live_features.yaml
    grain: [entity_id]
```

The runtime `handle` resolves to a provider or factory, not a one-time Python object, so reload can obtain a fresh dataset.

## Connector declaration rules

The initial connector union is closed and validated strictly.

| Type | Required fields | Optional non-secret fields |
|---|---|---|
| `parquet` | `location` | `credential_profile`, allowlisted Parquet options |
| `csv` | `location` | `credential_profile`, allowlisted CSV options |
| `delta` | `location` | `credential_profile`, allowlisted delta-rs options |
| `iceberg` | `catalog_profile`, `namespace`, `table` | allowlisted scan options |
| `sqlite` | `location`, `relation` | extension profile |
| `duckdb` | `location`, `relation` | read-only flag |
| `postgres` | `connection_profile`, `relation` | allowlisted scan options |
| `pyarrow` | `handle` | none |

Unknown fields are catalog errors. Connector options are typed and allowlisted; they are not arbitrary keyword mappings.

S3 is a transport, not a source type. `s3://` locations are supported by Parquet, CSV, and Delta adapters. Iceberg object-store access is configured through its catalog profile.

## Recursive logical schema

`schema` and referenced schema files share one format:

```yaml
fields:
  - name: id
    type: int64
    nullable: false
  - name: amount
    type:
      decimal:
        precision: 18
        scale: 2
    nullable: false
  - name: observed_at
    type:
      timestamp:
        unit: us
        timezone: UTC
    nullable: false
  - name: attributes
    type:
      map:
        key:
          type: utf8
          nullable: false
        value:
          type: utf8
          nullable: true
    nullable: true
  - name: items
    type:
      list:
        element:
          type:
            struct:
              fields:
                - name: sku
                  type: utf8
                  nullable: false
                - name: quantity
                  type: int32
                  nullable: false
          nullable: false
    nullable: true
```

The type model is Arrow-compatible and recursive. It includes:

- null, boolean, signed and unsigned integers, floating point, UTF-8, large UTF-8, binary, and large binary;
- fixed-size binary;
- decimal precision and scale;
- date, time, timestamp unit/timezone, duration, and interval;
- list, large list, fixed-size list, struct, and map;
- dictionary types when their index and value types are representable.

Fields are ordered. A field contains `name`, recursive `type`, `nullable`, and optional non-executable metadata. Connector-native field IDs may be preserved as metadata but are not semantic identifiers.

`schema_ref` is resolved relative to the catalog file unless an explicit catalog root is supplied. References cannot escape the configured catalog root. Cycles, duplicate fields, unsupported types, malformed metadata, and conflicting inline/reference declarations are deterministic catalog issues.

## Schema verification

Registration and reload compare the observed schema against the resolved declaration before publication.

Verification checks:

1. exact ordered field names;
2. normalized logical type compatibility;
3. nullability safety;
4. declared connector field IDs when present;
5. existence and type validity of every grain column;
6. dimension, relationship, and fact-reference fields against the declared schema.

Observed non-nullable data may satisfy a nullable declaration. An observed nullable field cannot satisfy `nullable: false`. Other type differences fail unless a narrowly specified, lossless normalization rule exists. No implicit lossy cast is added during registration.

A verified schema has a deterministic fingerprint derived from normalized fields and relevant metadata. Reload results expose this fingerprint but not source locations or credentials.

## Runtime profiles and secrets

Catalogs contain profile names only. `QueryEngine` receives a `RuntimeProfileResolver` that resolves profiles at runtime.

Profiles may describe:

- S3 endpoint, region, role/provider strategy, and TLS behavior;
- PyIceberg catalog implementation and non-secret catalog settings;
- PostgreSQL connection details;
- DuckDB extension installation/loading policy.

Secret values may come from environment/default chains, a caller-provided resolver, or an external secret manager adapter. They are never copied into model reprs, plans, OKF output, logs, schema fingerprints, or exception messages.

Profile resolution is lazy per connector and returns opaque adapter configuration. Missing profiles and missing optional dependencies have stable, sanitized errors.

## Registration strategies

### Parquet and CSV

Create a reusable PyArrow Dataset from the local or remote location and register it directly with DuckDB. Keep a strong reference in `SourceRegistry` for the registration lifetime. DuckDB performs projection and filter pushdown into the dataset.

CSV options are explicit and schema-driven. Runtime inference cannot override declared field types.

### Delta Lake

Use the `deltalake` Python package. Create a fresh `DeltaTable`, convert its selected snapshot to a PyArrow Dataset, verify the schema, and register the dataset with DuckDB. Reload creates a separate table/dataset candidate rather than mutating the live handle.

The adapter records a safe snapshot version in source status and reload results.

### Apache Iceberg

Use the `pyiceberg` Python package for catalog resolution, metadata refresh, snapshots, partition pruning, schema evolution, and delete semantics. Do not use DuckDB's Iceberg extension in this implementation.

The registry retains a PyIceberg table/snapshot handle. Before each query that requires the source, it creates a fresh query-scoped scan and Arrow reader. Required physical columns and source-local filters are derived from the resolved `QueryPlan` and supplied to PyIceberg. DuckDB retains the same filters in compiled SQL for correctness; pushdown is an optimization.

Query-scoped readers are registered for one execution and disposed afterward. Query execution and registration changes share the registry lock because DuckDB Python connections and stable source names are not treated as concurrently mutable.

### SQLite, DuckDB files, and PostgreSQL

Use DuckDB's mature native attach/scan facilities behind private adapters. Each adapter exposes one configured relation under the stable semantic source name. Raw SQL is never accepted from the catalog; relation identifiers are validated and quoted.

Database relations read current rows on each query. Reload reconnects or reattaches, re-introspects schema, and publishes a new registration generation. This refreshes connection and metadata state rather than materializing the table.

### Programmatic PyArrow

The runtime handle registry maps a catalog handle name to a provider callable. The callable returns a fresh supported Arrow object, preferably a `pyarrow.dataset.Dataset`. Tables, scanners, and record-batch readers may be supported with documented lifecycle constraints.

One-shot readers are query-scoped. Reload invokes the provider again and never assumes an old reader can be rewound.

## SourceRegistry interface

The public `QueryEngine` additions are intentionally small:

```python
class QueryEngine:
    def reload_source(self, source_id: str) -> ReloadResult: ...
    def reload_all(self) -> tuple[ReloadResult, ...]: ...
    def source_status(self, source_id: str) -> SourceStatus: ...
```

The internal adapter seam is richer but private:

```python
class SourceAdapter(Protocol):
    def prepare(self, source: DataSource, profiles: RuntimeProfileResolver) -> SourceHandle: ...
    def inspect_schema(self, handle: SourceHandle) -> TableSchema: ...
    def register(self, connection: DuckDBPyConnection, name: str, handle: SourceHandle) -> None: ...
    def bind_query(self, handle: SourceHandle, plan: QueryPlan) -> QueryBinding | None: ...
    def close(self, handle: SourceHandle) -> None: ...
```

Adapters may have persistent or query-scoped registration modes. Callers do not branch on those modes.

`SourceStatus` contains source ID, connector type, registration generation, schema fingerprint, safe connector snapshot/version when available, and health state. It contains no handle, profile contents, path credentials, or connection string.

## Initial registration

`QueryEngine.__init__` constructs `SourceRegistry`, which:

1. resolves each source adapter;
2. prepares all candidate handles;
3. introspects and verifies all schemas;
4. registers candidates under stable, quoted semantic names;
5. publishes the registry only after every source succeeds.

Initialization is all-or-nothing. A failure closes all candidates and the DuckDB connection and raises a sanitized source error.

## Atomic reload

### `reload_source(source_id)`

1. Resolve the existing source definition and adapter.
2. Prepare a candidate handle without modifying the active handle.
3. Introspect and verify the candidate schema.
4. Acquire the registry execution lock.
5. Replace the stable DuckDB registration or view.
6. Publish the candidate handle and increment its generation.
7. Release the lock and close the previous handle.

Candidate preparation and schema inspection happen before the critical section. Queries see either the old or new generation, never an intentionally half-configured candidate.

If replacement fails, the registry restores or preserves the previous registration before releasing the lock. The old handle remains active. Reload never requires reparsing the catalog or rebuilding `QueryEngine`.

### `reload_all()`

Prepare and verify every candidate first. Under one registry lock, replace registrations in deterministic source-ID order. If any replacement fails, restore every previous registration before releasing the lock, close all candidates, and raise one aggregate reload error. Callers observe all old generations or all new generations.

### Connector-specific refresh

- Parquet/CSV: recreate the Dataset so newly added, removed, or replaced files are discovered.
- Delta: create a new `DeltaTable` and Dataset at the latest selected snapshot.
- Iceberg: refresh/load a new table metadata handle; future queries create readers from the new snapshot.
- Databases: reconnect/reattach and revalidate relation metadata.
- Programmatic Arrow: invoke the provider factory again.

Automatic refresh remains out of scope.

## Query execution flow

1. `QueryEngine.plan()` produces the existing immutable `QueryPlan`.
2. `SourceRegistry.bind(plan)` identifies persistent and query-scoped source bindings.
3. The registry creates fresh Iceberg or one-shot Arrow readers with required projections and eligible source-local filters.
4. The DuckDB compiler produces the same parameterized SQL from the plan.
5. Under the execution lock, DuckDB executes against one coherent registration generation.
6. Query-scoped bindings are removed and closed in `finally`.
7. Results are returned as Polars, preserving the existing caller interface.

Pushdown must not alter semantics. Compiled DuckDB filters remain authoritative and bound as parameters.

## Error model

Catalog errors remain aggregated `CatalogValidationError` issues. Source runtime errors use stable domain types:

- `SourceDependencyError`: required optional package or DuckDB extension unavailable;
- `SourceProfileError`: runtime profile missing or invalid;
- `SourceConnectionError`: connector could not open or register;
- `SourceSchemaError`: observed schema violates the declaration;
- `SourceReloadError`: candidate preparation or atomic swap failed.

Errors include a random operation ID, source ID when safe, stable category/code, and actionable non-secret context. They exclude profile values, authenticated URLs, connection strings, bound filter values, raw driver exceptions that may contain secrets, and Python object reprs. Exceptions are raised without leaking unsafe causes or contexts.

## Packaging

Connector dependencies use optional extras:

- core: DuckDB, PyArrow, Parquet, CSV, programmatic Arrow, and DuckDB-file support;
- `selayer[delta]`: `deltalake`;
- `selayer[iceberg]`: `pyiceberg` with documented catalog extras;
- `selayer[postgres]`: PostgreSQL integration requirements;
- `selayer[s3]`: object-store requirements not already provided by PyArrow;
- `selayer[all]`: every supported connector dependency.

SQLite uses DuckDB's extension with explicit installation/loading policy and offline diagnostics. Exact dependency bounds are fixed by the implementation plan after compatibility probes.

## OKF integration

Catalog-backed OKF source concepts may include a generated section with:

- connector category without secret settings;
- schema fingerprint;
- grain;
- bounded field summaries;
- safe snapshot/version and freshness metadata when explicitly exported;
- provenance and owner links.

Full large schemas need not be copied into every retrieved context. Generation may write a dedicated catalog-derived schema artifact and link to it. Curated OKF text remains preserved across regeneration.

The planner and `SourceRegistry` never read OKF to decide schemas, connectors, profiles, reloads, or query validity.

## Testing strategy

### Catalog and schema tests

- every connector's discriminated required/forbidden fields;
- exactly one of inline schema or `schema_ref`;
- reference-root containment, cycles, malformed files, and deterministic errors;
- every recursive logical type and malformed nested variants;
- schema fingerprint determinism;
- grain, dimension, relationship, and fact-field validation against schema;
- no secret-bearing fields accepted in catalog YAML.

### Adapter contract tests

A shared adapter suite verifies prepare, schema inspection, registration, reload, cleanup, and sanitized failures. Connector-specific tests add snapshot and profile behavior.

### Pushdown tests

- DuckDB projection/filter pushdown into registered PyArrow Datasets;
- Delta Dataset pruning through delta-rs and Arrow;
- PyIceberg selected fields and source-local row filters passed into fresh scans;
- residual DuckDB filtering retained for correctness.

### Integration tests

- local Parquet and CSV directories, including newly added files after reload;
- local Delta table snapshot advancement;
- local PyIceberg catalog and warehouse snapshot refresh;
- SQLite and DuckDB files;
- PostgreSQL through a CI service container;
- S3 paths through MinIO with named profile resolution;
- programmatic Arrow provider generations.

### Reload tests

- successful source replacement changes query results without recreating the engine;
- failed candidate preparation leaves the old source queryable;
- schema mismatch leaves the old generation active;
- failed `reload_all()` restores all previous registrations;
- deterministic generation and result metadata;
- concurrent query/reload serialization;
- one-shot reader recreation and cleanup.

### Security and packaging tests

- credentials and authenticated locations absent from every error surface;
- optional dependency failures are actionable and sanitized;
- connector extras install independently;
- the wheel includes source adapters and schema resources but no credentials, test services, or legacy package;
- OKF summaries are derived, bounded, and non-authoritative.

## Migration

This is a direct rewrite of the unreleased schema-version-1 source contract.

1. Replace `DataSource(type, path, grain)` with the discriminated connector definition plus resolved `TableSchema`.
2. Update the catalog loader and all fixtures to require inline schema or `schema_ref`.
3. Replace eager Polars reads in `QueryEngine` with `SourceRegistry`.
4. Migrate the e-commerce catalog and example schemas.
5. Update OKF catalog generation to include safe schema summaries.
6. Remove unused eager-loading and PyArrow dependency assumptions only after all adapters are active.

No compatibility loader or automatic v1 source-shape migration is added. Invalid old catalogs receive deterministic validation errors and must be rewritten.

## Delivery sequence

1. Recursive schema model, schema documents, resolution, and semantic-field validation.
2. Source adapter seam, registry, profile resolver, and eager-loader replacement.
3. Parquet, CSV, programmatic Arrow, and S3 transport.
4. Delta adapter and snapshot reload.
5. Iceberg query-scoped adapter and scan pushdown.
6. SQLite, DuckDB-file, and PostgreSQL adapters.
7. Atomic reload, rollback, status, and concurrency hardening.
8. OKF schema-summary synchronization.
9. Full connector matrix, packaging extras, examples, and documentation.

Each slice is implemented test-first and independently reviewed. The active suite must remain green after every slice.

## Acceptance criteria

The design is complete when:

1. every catalog source has one complete authoritative schema;
2. all listed connectors register and execute through one private registry seam;
3. file and Delta Dataset scans preserve DuckDB-to-Arrow pushdown;
4. Iceberg semantics are owned by PyIceberg and use fresh scans after refresh;
5. no supported connector eagerly materializes a complete source merely to register it;
6. reload changes results without rebuilding `QueryEngine` and rolls back safely on failure;
7. source schemas are verified before initial publication and every reload;
8. credentials remain outside executable catalogs and every observable error surface;
9. OKF exposes only generated advisory schema context;
10. connector-specific integration and package-extra tests pass.
