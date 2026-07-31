# Selayer verification design

## Purpose

Add deterministic, machine-readable verification to selayer without creating a second catalog validator, relationship planner, source adapter stack, or OKF authority.

The design introduces one verification report contract over existing modules, exact opt-in physical audits, planner-derived compatibility reports, catalog-aware OKF integrity checks, restricted fresh-bundle composition, a runtime profile file for the command line, and a unified `selayer` CLI.

The approved shopfloor hardening design supplies the first acceptance case.

## Context

Selayer already has strong checks at several points:

- `SemanticLayer.load()` validates schema-version-1 YAML catalogs.
- The expression modules parse a restricted engine-neutral DSL.
- The planner rejects mixed grains, row-expanding paths, disconnected sources, and ambiguous safe paths.
- Source adapters compare declared and observed schemas before registration.
- `OkfBundle` generates, synchronizes, validates, and retrieves advisory Markdown.

The checks are not presented through one evidence contract. Important gaps remain:

- `CatalogIssue` has no stable machine-readable code.
- Direct `SemanticLayer` construction can bypass YAML catalog checks, and `QueryEngine` trusts the object it receives.
- Declared grain uniqueness and relationship cardinality are not checked against physical rows.
- Join-column type compatibility and measure aggregation compatibility are underchecked.
- Authors cannot ask the planner for a bounded compatibility report.
- Catalog-aware OKF validation does not prove generated-region integrity or exact catalog projection.
- Curated OKF overlays have no restricted fresh-build interface.
- The CLI has `selayer-okf` but no catalog validation, audit, or compatibility commands.
- Runtime profiles have a safe in-memory model but no command-line file format.

## Goals

- Reuse existing validation, planning, source, and OKF implementations.
- Give CI, users, and agents stable diagnostics and explicit check outcomes.
- Keep static validation fast and deterministic.
- Validate programmatically constructed layers at the execution boundary.
- Verify declared grains and relationships with exact scans when explicitly requested.
- Report compatibility by delegating to the existing planner.
- Detect stale or modified generated OKF content against the current catalog.
- Compose fresh OKF bundles from generated concepts, authored references, and restricted overlays.
- Add a unified standard-library CLI while preserving `selayer-okf` compatibility.
- Keep credentials, source rows, and offending key values out of reports and errors.
- Deliver the work in stages that each leave useful, tested behavior.

## Non-goals

- Sampling or approximate proof of grain or referential integrity.
- Auditing every query at normal engine startup.
- A distributed transaction across heterogeneous sources.
- Full expression result-type and nullability inference.
- A general business-constraint language.
- Data profiling, histograms, representative values, or semantic inference.
- A check plugin registry.
- Persisting compatibility matrices in catalogs.
- In-place application of curated overlays to existing bundles.
- Agent prompts, interviews, proposal schemas, or autonomous correction.
- Making OKF content executable or authoritative.
- Replacing existing source adapters or adding an execution engine.

## Architectural decision

Use a thin verification orchestrator over existing deep modules.

The orchestrator owns:

- one report and diagnostic model;
- normalization of existing catalog and planner failures;
- exact physical proof queries;
- deterministic result ordering and JSON conversion;
- CLI behavior.

It delegates:

- catalog rules to shared catalog validation functions;
- relationship and grain-path decisions to `plan_query()`;
- source opening and schema comparison to `SourceRegistry` and adapters;
- SQL execution to the existing DuckDB connection;
- OKF parsing, rendering, generation, and validation to `selayer.okf`.

No second graph, expression parser, schema comparator, or Markdown renderer is introduced.

## Proposed module structure

```text
src/selayer/
├── cli.py
├── verification/
│   ├── __init__.py
│   ├── audit.py
│   ├── compatibility.py
│   ├── model.py
│   └── static.py
├── sources/
│   └── profile_file.py
└── okf/
    └── composition.py
```

`verification.__init__` is the public verification interface. Internal modules remain private implementation details.

`okf.composition` is called through `OkfBundle.build()`. It is not a second public OKF object model.

## Public verification interface

### Check requests

```python
@dataclass(frozen=True, slots=True)
class StaticCheck:
    pass


@dataclass(frozen=True, slots=True)
class PhysicalCheck:
    profiles: RuntimeProfileResolver | None = None
    arrow_providers: ArrowProviderResolver | None = None


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    metrics: tuple[str, ...] | None = None
    dimensions: tuple[str, ...] | None = None
    query_cases: tuple[QueryRequest, ...] = ()


VerificationCheck = StaticCheck | PhysicalCheck | CompatibilityCheck
```

`None` for metrics or dimensions means all declared values in stable identifier order.

### Operations

```python
@dataclass(frozen=True, slots=True)
class CatalogValidationResult:
    layer: SemanticLayer | None
    report: VerificationReport


def validate_catalog(path: str | Path) -> CatalogValidationResult:
    ...


def verify(
    layer: SemanticLayer,
    check: VerificationCheck,
) -> VerificationReport:
    ...
```

`validate_catalog()` returns a layer only when every required static check passes. It adapts YAML, schema-document, and catalog-domain failures into the shared report without changing `SemanticLayer.load()` exception behavior.

`verify()` accepts an already constructed layer. `StaticCheck` checks model-level invariants, `PhysicalCheck` inspects registered source data, and `CompatibilityCheck` delegates to the planner.

## Report model

```python
Severity = Literal["error", "warning", "info"]
OutcomeStatus = Literal["passed", "failed", "skipped", "unavailable"]
EvidenceScope = Literal["declaration", "full_scan", "planner"]


@dataclass(frozen=True, slots=True)
class VerificationDiagnostic:
    code: str
    severity: Severity
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    check_id: str
    status: OutcomeStatus
    scope: EvidenceScope
    path: str
    evidence: Mapping[str, bool | int | float | str | None]
    diagnostics: tuple[VerificationDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: Literal[1]
    subject: str
    check_kind: Literal["static", "physical", "compatibility", "okf"]
    complete: bool
    outcomes: tuple[VerificationOutcome, ...]
    diagnostics: tuple[VerificationDiagnostic, ...]

    @property
    def passed(self) -> bool:
        ...

    def to_dict(self) -> dict[str, object]:
        ...
```

`passed` is true only when the report is complete, every required outcome passed, and no error diagnostic exists.

Outcomes and diagnostics use stable sorting keys. Mappings returned by public immutable models are defensively copied and frozen.

Evidence may include source IDs, semantic IDs, field names, counts, declared types, observed logical types, schema fingerprints, planner codes, source generations, and connector snapshot identifiers. It may not contain source values, duplicate key values, orphan key values, credentials, DSNs, authenticated locations, profile values, or raw driver errors.

## Diagnostic codes

Diagnostic codes use lowercase dotted names. The first segment identifies the subsystem.

Examples:

```text
catalog.grain.duplicate_column
catalog.grain.nullable_column
catalog.relationship.join_type_mismatch
catalog.measure.invalid_aggregation_type
catalog.layer.invalid_programmatic_model
source.profile.missing_environment
source.audit.unavailable
source.grain.null_value
source.grain.duplicate_value
source.relationship.one_side_duplicate
source.relationship.orphan_target
planner.mixed_grain
planner.row_expanding_path
okf.generated.fingerprint_mismatch
okf.generated.missing_concept
okf.overlay.forbidden_frontmatter
okf.overlay.forbidden_section
okf.link.missing_fragment
```

`CatalogIssue` gains a stable `code` while retaining its current `path` and `message` attributes. Existing construction remains source-compatible by giving the new field a default during the transition. Catalog validation emits specific codes rather than the default.

Existing `QueryPlanningError.code` values remain unchanged. Compatibility reports prefix or classify them without inventing different planner meanings.

## Shared static validation

### Existing behavior

`SemanticLayer.load()` remains the strict YAML entry point. It continues to aggregate sorted issues and constructs no partial layer.

Existing checks remain in force:

- exact schema version;
- identifier syntax;
- source declaration shape;
- contained schema references;
- source grain columns;
- dimension source, column, and type;
- fact expression syntax, symbols, columns, and safe reachability;
- measure fact and aggregation references;
- metric measure declarations and common grain;
- relationship endpoints, columns, and supported cardinality.

### Shared model rules

Rule implementations that apply to both parsed catalogs and `SemanticLayer` objects move behind internal catalog validation helpers. The YAML loader and `verification.static` call the same helpers.

The refactor must not create a second list of equivalent rules. YAML-only checks, such as duplicate mapping keys and contained path handling, stay in the loader. Model checks, such as references and graph consistency, are shared.

### New static checks

Reject duplicate column names inside one source grain.

Require every grain field to be declared non-nullable. Observed stricter nullability remains acceptable under the existing schema comparison rule.

Require relationship join columns to have compatible logical types. Exact matching is required except where the existing schema type system already defines safe equivalence.

Use the declared fact data type to enforce aggregation compatibility:

- `sum` and `avg` require integer or decimal facts;
- `min` and `max` require string, integer, decimal, boolean, date, timestamp, or another type already classified as orderable by the schema model;
- `count` and `count_distinct` accept every supported fact type.

This design does not infer result types for arbitrary expression trees.

Validate filter values against the selected dimension data type before compilation. The query error must name the dimension and expected logical type without rendering the supplied value.

### Programmatic layers

`QueryEngine.__init__()` runs `verify(layer, StaticCheck())` before opening DuckDB or creating a source registry.

A failing report is converted to `CatalogValidationError` with stable `CatalogIssue` values. No connector opens for an invalid layer.

Direct model construction remains available for tests and advanced users. It no longer bypasses execution-boundary validation.

## Exact physical audit

### Execution contract

Physical audits are explicit. They never run during `SemanticLayer.load()` or normal `QueryEngine` startup.

A physical audit opens sources through the existing registry and adapter contracts, holds the registry's internal execution lock while each proof query runs, and closes all owned resources when the report is complete or an error occurs.

The verifier generates its own quoted DuckDB SQL from validated identifiers. It accepts no user SQL.

The internal registry inspection seam may expose a connection and generated aliases only to `verification.audit`. It is not public API.

### Source checks

For every source:

1. Prepare and inspect the source through its adapter.
2. Compare observed and declared schemas using `compare_schemas()`.
3. Count rows with any null grain field.
4. Count duplicate composite grain groups.
5. Record total row count and distinct grain count.

A source grain passes only when the schema matches, no grain field is null, and no duplicate grain group exists.

### Relationship checks

For each relationship, determine the declared one and many sides from its cardinality.

For `one_to_one`:

- both join columns must be non-null and unique;
- non-null keys on each side must resolve on the other side.

For `one_to_many` and `many_to_one`:

- the one-side join column must be non-null and unique;
- null keys on the many side are allowed;
- every non-null many-side key must resolve on the one side;
- zero-child one-side rows are counted and reported as information;
- maximum observed child multiplicity is recorded.

For `many_to_many`:

- uniqueness is not asserted;
- non-null unmatched keys on both sides are counted;
- the report states that no grain-preserving traversal follows from the declaration.

The audit reports observed counts. It never includes the key values that caused a failure.

### Exactness and snapshots

Every proof query scans the complete source relation visible to the connector. The report marks the evidence scope as `full_scan`.

Selayer prevents its own registry reloads during an individual query. It cannot provide a distributed transaction across files, SQLite, DuckDB, PostgreSQL, Delta, Iceberg, and external object stores. The report records per-source connector generations and snapshot or version identifiers when adapters expose them. Missing snapshot metadata is represented as `None`, not as proof of cross-source consistency.

No sampled mode exists in this design.

### Outcome behavior

A declared invariant violation produces `failed`.

A missing optional connector dependency, missing runtime profile, unresolved Arrow provider, inaccessible source, or unsupported proof operation produces `unavailable` with a stable diagnostic.

A check that does not apply to a cardinality or connector produces `skipped` and explains why.

The overall audit is incomplete when any required check is unavailable or skipped unexpectedly. The CLI exits nonzero for incomplete or failed audits.

## Runtime profile file

### Format

The CLI accepts `--profiles PATH` with this schema:

```yaml
version: 1
profiles:
  warehouse:
    dsn:
      env: SELAYER_WAREHOUSE_DSN
    allow_extension_install:
      literal: false
```

The top-level mapping accepts only `version` and `profiles`. Version is exactly integer `1`.

Profile names use the existing runtime profile name rules. Each profile value key maps to exactly one source:

```yaml
key_name:
  env: ENVIRONMENT_VARIABLE_NAME
```

or:

```yaml
key_name:
  literal: true
```

Environment names must be non-empty exact strings and match `[A-Za-z_][A-Za-z0-9_]*`. Their resolved values are exact strings.

Literal values are exact booleans. String, numeric, null, sequence, and mapping literals are rejected. Non-secret strings such as regions and endpoints must still come through environment variables. This keeps all string values out of the file and removes the need to classify which adapter options are secret.

Duplicate YAML keys, unknown fields, empty profiles, and entries containing both or neither `env` and `literal` fail validation.

### Resolution

`load_profile_file(path, environ=os.environ)` returns a `MappingProfileResolver`.

Missing environment variables produce `source.profile.missing_environment`. The diagnostic may name the missing environment variable and profile key, but never includes another environment value.

The parser never includes profile values in reprs, reports, exceptions, chained causes, or CLI output. It uses the existing `RuntimeProfile` defensive-copy and secret-safe representation.

Programmatic callers may continue supplying any `RuntimeProfileResolver`. The file format is a CLI adapter, not a replacement for the protocol.

Programmatic Arrow providers remain supported by `PhysicalCheck`. The CLI cannot serialize callables and reports a required unresolved provider as unavailable.

## Planner compatibility report

`CompatibilityCheck` calls the existing planner with deterministic requests.

It evaluates:

1. every selected metric alone;
2. every selected metric with every selected dimension;
3. every unordered pair of selected metrics;
4. caller-supplied explicit `QueryRequest` values.

The default selected set is every declared metric and dimension.

A successful request records the anchor source, required sources, selected dimensions, and join relationship IDs. A failed request records the existing planner error code and no SQL.

The report does not evaluate all dimension pairs or every larger combination. It states its coverage explicitly. Consumers that need a particular multi-metric or multi-dimension query add it as an explicit query case.

The compatibility implementation may construct `QueryRequest` and call `plan_query()`. It must not initialize sources, compile SQL, or reproduce graph traversal.

## Catalog-aware OKF integrity

When a layer is supplied, strict OKF validation adds generated-integrity checks.

For every catalog-backed concept:

- require exactly one matching generated concept;
- require the expected semantic kind and path;
- require exactly one `Catalog Definition` section;
- recompute the controlled-region fingerprint and compare it with the stored authoritative fingerprint;
- compare the generated definition with a fresh in-memory projection of the current layer.

At bundle level:

- report missing generated concepts;
- report generated concepts whose `selayer_id` no longer exists as orphans;
- validate generated root and per-kind index membership, link target, and displayed title;
- validate kind-directory and `selayer_id` agreement;
- validate internal link targets and heading fragments.

Authored concepts without `selayer_id`, including Reference concepts, remain valid and are not compared with the catalog projection.

Generic OKF validation without a layer retains its current structural behavior.

## Fresh OKF composition

### Public operation

```python
@classmethod
def OkfBundle.build(
    cls,
    layer: SemanticLayer,
    output_dir: str | Path,
    *,
    references_dir: str | Path | None = None,
    overlays_dir: str | Path | None = None,
    include_descriptive: bool = False,
) -> OkfBundle:
    ...
```

`generate()` and `sync()` retain their current contracts.

### Reference inputs

`references_dir` contains authored Markdown concepts. Every file must:

- have valid YAML frontmatter;
- have a non-empty type and title;
- omit `selayer_id`;
- avoid reserved root and per-directory `index.md` and root `log.md` paths;
- stay within the provided root;
- contain no symbolic links.

Reference paths are preserved relative to `references_dir` and copied below bundle `references/`.

### Overlay inputs

`overlays_dir` contains sparse Markdown overlays. The relative path mirrors the generated concept path, and frontmatter must contain the matching typed `selayer_id`.

Allowed overlay frontmatter is exactly:

- `selayer_id`
- `sources`
- `stale_after`

Allowed top-level sections are exactly:

- `Usage Guidance`
- `Examples`
- `Caveats`
- `Related Concepts`

Every heading may occur at most once. `Catalog Definition`, `verified`, generated metadata, titles, descriptions, arbitrary preamble, unknown sections, and unknown frontmatter are rejected.

An overlay may omit any allowed section. Coverage policies such as "all metric sections are required" belong to the consuming example or agent workflow, not generic selayer OKF conformance.

The composer rejects unknown IDs, path and ID mismatches, duplicate IDs, self-links, duplicate related links, broken internal links, and overlays for authored Reference concepts.

### Input limits

Fresh composition enforces these fixed limits across reference and overlay roots:

- at most 1,000 Markdown files;
- at most 1,048,576 bytes per file;
- at most 16,777,216 total bytes;
- at most 1,000 extracted links per file.

The composer rejects symbolic links, path escapes, non-UTF-8 content, and special files.

### Staging and publication

Build uses a sibling temporary directory on the destination filesystem.

It performs these steps:

1. Preflight the destination and all lexical ancestors using the existing mutation safety rules.
2. Generate the catalog projection in staging.
3. Copy validated Reference concepts.
4. Apply validated overlays to curated sections and allowed frontmatter.
5. Regenerate indexes for the complete composed bundle where applicable.
6. Load the staged bundle in strict mode with the layer.
7. Run catalog-aware integrity and link validation.
8. Rename staging to the absent or empty destination.

If the destination is a file, symbolic link, or non-empty directory, build fails before staging.

If any staging step fails, the destination remains absent or empty and the temporary directory is removed.

Build-time composition never patches an existing populated bundle. Callers rebuild disposable output from versioned inputs.

## Unified CLI

Add this console entry point:

```toml
selayer = "selayer.cli:run"
```

Commands:

```text
selayer catalog validate CATALOG
selayer catalog audit CATALOG [--profiles FILE]
selayer catalog compatibility CATALOG
selayer okf build CATALOG DEST [--references DIR] [--overlays DIR]
selayer okf generate CATALOG DEST
selayer okf sync CATALOG BUNDLE [--dry-run]
selayer okf validate BUNDLE [--catalog CATALOG]
selayer okf retrieve BUNDLE SEMANTIC_ID... [existing options]
```

The command layer uses `argparse` and the Python standard library for presentation, matching the existing OKF CLI.

`selayer-okf` remains installed and keeps its current argument shape, JSON output, and exit codes. Its implementation delegates to shared OKF command handlers rather than copying logic.

### JSON and exit codes

Every verification command writes one deterministic JSON object to standard output. Reports use `schema_version: 1`.

Exit codes are:

- `0` when every required outcome passes;
- `1` for validation failure, failed audit, unavailable required check, build conflict, domain error, or I/O error;
- `2` for invalid command-line usage.

Invalid usage follows `argparse`. Domain failures retain their report on standard output when a report exists and write a short secret-safe summary to standard error. No command silently changes catalog or bundle authority.

## QueryEngine boundary behavior

`QueryEngine` validates the supplied layer before opening DuckDB.

The order is:

1. Run model-level `StaticCheck`.
2. Raise `CatalogValidationError` if it fails.
3. Open the in-memory DuckDB connection.
4. Create the source registry and compare observed schemas.

This order ensures an invalid programmatic model opens no connectors and allocates no registry resources.

Physical grain and relationship audits are not part of startup. Existing observed-schema checks remain part of source registration.

## Security and privacy

Verification outputs use aggregate counts and metadata only.

No physical diagnostic includes row values, duplicate grain tuples, orphan foreign keys, profile values, credentials, DSNs, authenticated locations, or raw connector messages.

Profile parsing resolves environment values only after structural validation. Resolved mappings are passed directly into `RuntimeProfile` and are never serialized.

Audit SQL uses validated, quoted source and column identifiers. Users cannot provide expressions or SQL to the audit command.

OKF composition treats reference and overlay text as untrusted data. It does not execute code blocks, follow external links, retrieve URLs, or run Attested Computations.

Read and write paths use lexical containment and symbolic-link rejection. Fresh composition limits file count, size, total bytes, and link count before parsing or copying.

The existing syntactic `verified.by: human:*` trust tier is not an authenticated approval mechanism. Restricted overlays cannot write `verified`. Authenticated review belongs to repository controls and the later agent-assistance design.

## Delivery stages

### Stage 1: shared diagnostics and static verification

- Add the verification report model.
- Add stable catalog diagnostic codes.
- Refactor shared model-level catalog rules.
- Add duplicate grain, grain nullability, join type, and aggregation type checks.
- Validate filter values before compilation.
- Validate programmatic layers in `QueryEngine`.
- Add `selayer catalog validate` and the unified CLI shell.

### Stage 2: OKF integrity and fresh composition

- Add catalog-aware generated-integrity checks.
- Validate link fragments and generated indexes.
- Add restricted Reference and overlay parsing.
- Add `OkfBundle.build()` with staged atomic publication.
- Add `selayer okf build`.
- Route existing OKF commands through the unified CLI while preserving `selayer-okf`.

This stage unblocks the shopfloor on-demand knowledge build.

### Stage 3: compatibility reporting

- Add `CompatibilityCheck`.
- Add deterministic metric, dimension, metric-pair, and explicit-case evaluation.
- Add `selayer catalog compatibility`.

### Stage 4: runtime profiles and exact physical audits

- Add the version-1 runtime profile file parser.
- Add private registry inspection support.
- Add exact grain and relationship proof queries.
- Add connector snapshot metadata where already available.
- Add `selayer catalog audit`.

Each stage must pass the full existing test suite and leave the public interface usable without later stages.

## Test strategy

### Report and diagnostic tests

- Reports and nested mappings are immutable.
- Ordering is stable across input mapping order and hash seeds.
- `passed` is false for failed, unavailable required, incomplete, or error-bearing reports.
- JSON contains no implementation object reprs.
- Existing catalog messages remain understandable while codes are stable.

### Static validation tests

- Duplicate grain fields fail.
- Nullable grain fields fail.
- Relationship type mismatches fail.
- Invalid aggregation and fact-type pairs fail.
- Valid count and ordering aggregations continue to pass.
- Filter type mismatches fail without rendering values.
- Existing YAML catalog fixtures retain their expected diagnostics.
- Invalid direct layers fail before DuckDB or adapters open.

### Profile file tests

- Valid environment and boolean entries resolve.
- Duplicate keys, unknown fields, invalid names, missing environment variables, scalar strings, numeric literals, nulls, sequences, and mappings fail.
- Environment values never appear in reprs, reports, exceptions, causes, contexts, stdout, or stderr.
- Existing programmatic resolvers remain unchanged.

### Physical audit tests

For CSV, Parquet, SQLite, DuckDB file, Delta, Iceberg, PostgreSQL, and programmatic Arrow where their existing test harness supports them:

- valid grains pass;
- null grain rows fail;
- duplicate composite grains fail;
- one-side duplicates fail;
- allowed nullable many-side keys do not fail;
- non-null target orphans fail;
- zero-child parents produce information only;
- one-to-one checks both directions;
- many-to-many reports no safe traversal claim;
- unavailable dependencies and profiles produce unavailable outcomes;
- every resource closes on success and failure;
- reports contain counts but no offending values.

Remote connector tests use existing integration markers and containers. Unit tests use local adapters and deterministic fixtures.

### Compatibility tests

- Every result matches a direct call to `plan_query()`.
- Successful outcomes record the same anchor, sources, and relationships.
- Failures preserve current planner codes.
- Metric and dimension selection is validated and sorted.
- Explicit query cases support multi-dimension acceptance examples.
- No source adapter or compiler runs.

### OKF integrity tests

- Modified controlled regions fail fingerprint integrity.
- A valid-looking stale fingerprint fails.
- Missing, orphaned, duplicate, wrong-kind, and wrong-path concepts fail.
- Generated index omissions, wrong titles, and wrong links fail.
- Missing heading fragments fail.
- Authored concepts without `selayer_id` remain valid.
- Generic layer-free validation remains backward compatible.

### OKF composition tests

- A fresh generated-only build matches `generate()` semantics.
- Valid Reference concepts and overlays compose successfully.
- Controlled frontmatter, forbidden sections, preambles, verification claims, duplicate headings, path mismatches, unknown IDs, self-links, duplicate related links, and broken links fail.
- Symbolic links, path escapes, oversized files, excessive file counts, total-size overflow, and excessive links fail.
- A failed build publishes no partial destination.
- A successful build loads strictly against the layer.
- A non-empty destination is rejected.

### CLI compatibility tests

- Unified commands emit deterministic JSON and documented exit codes.
- Invalid usage exits `2`.
- Existing `selayer-okf` command tests remain unchanged or are duplicated against shared handlers.
- `selayer-okf` and `selayer okf` produce equivalent results for existing commands.
- Secret-bearing environment values do not appear in captured output.

### Shopfloor acceptance

The hardened shopfloor example verifies:

- static catalog validation;
- conformed drive dimensions;
- deliberate telemetry isolation;
- exact grain and relationship audits;
- planner compatibility reporting;
- fresh OKF composition with four Reference concepts and curated overlays;
- catalog-aware generated integrity;
- baseline and temporary Delta retest behavior.

## Documentation

Update the root README with:

- validation versus verification terminology;
- unified CLI examples;
- exact audit cost and snapshot limitations;
- profile file format and secret-handling rules;
- compatibility report coverage limits;
- generated versus authored OKF ownership;
- fresh build behavior;
- `selayer-okf` compatibility status.

Update `.github/copilot-instructions.md` so the active-module list includes `src/selayer/okf/` and `src/selayer/verification/`. Replace the stale wording that can be read as forbidding OKF with the precise rule that OKF remains advisory and cannot change planning or execution.

A later root `AGENTS.md` will summarize the stable commands and authority rules after the interfaces in this design are implemented.

## Relationship to other designs

The approved shopfloor hardening design is the first consumer. Its implementation plan may assume the Stage 2 `OkfBundle.build()` interface and use Stage 3 and Stage 4 as verification acceptance tests.

The later agent-assisted semantic discovery design will consume reports, profile-file behavior, compatibility results, and restricted overlays. It will not add LLM behavior to this package.

Implementation planning order is:

1. Write and approve this verification design.
2. Write the selayer verification implementation plan by delivery stage.
3. Write the complete shopfloor implementation plan against the exact library interfaces.
4. Design the separate agent-assisted semantic discovery workflow.
