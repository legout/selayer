# Agent-assisted semantic discovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic `selayer-discovery` workspace package and Agent Skill that turn bounded evidence and interviews into verified, dependency-grouped semantic proposals with named approval and recoverable explicit apply.

**Architecture:** Core selayer gains only deterministic deprecation metadata and a bounded source scan-session seam. The standalone workspace member owns event-sourced sessions, evidence, profiling, providers, interviews, typed proposals, approval, and transactions. The host Agent Skill supplies reasoning but can mutate state only through validated companion commands.

**Tech Stack:** Python 3.13, uv workspace, dataclasses, argparse, PyArrow, DuckDB, PyYAML, ruamel.yaml, filelock, selayer verification and OKF APIs, pytest, Ruff, Pyright.

## Prerequisites

Complete these plans first:

1. `docs/superpowers/plans/2026-07-31-selayer-verification.md`, all stages.
2. `docs/superpowers/plans/2026-07-31-shopfloor-example-hardening.md`, Tasks 1 through 9.

This plan consumes:

```python
validate_catalog(path) -> CatalogValidationResult
verify(layer, StaticCheck()) -> VerificationReport
verify(layer, PhysicalCheck(...)) -> VerificationReport
verify(layer, CompatibilityCheck(...)) -> VerificationReport
OkfBundle.build(...)
```

Do not begin proposal verification before these interfaces exist. Workspace scaffolding, deprecation metadata, and scan sessions may be implemented independently after confirming the prerequisite plans have not changed their public contracts.

## Global constraints

- Core `selayer` must remain installable without `selayer-discovery`.
- Core must not import the discovery package.
- `selayer-discovery` contains no LLM SDK, model API key, prompt runtime, SQL supplied by an agent, or wiki write adapter.
- The Agent Skill is the only reasoning layer; package tests use replayed structured drafts.
- Catalog YAML remains execution authority. OKF and discovery artifacts remain advisory until typed operations are approved and applied.
- Version 1 accepts normalized Markdown and plain text only.
- External knowledge providers are read-only Python entry points. No subprocess provider is allowed.
- Raw sessions, source-derived values, full transcripts, provider bodies, backups, and journals remain ignored.
- Only approved summaries, catalog source, authored References, and curated overlays enter Git.
- Agents may add, edit, or deprecate. They may not hard-delete or rename IDs.
- Every data-dependent executable proposal requires a reopenable source snapshot.
- Every approval actor must match the charter's normalized named approver. This is workflow enforcement, not authentication.
- Apply uses typed operations, never a preview patch.
- Apply does not invoke Git or edit generated OKF output.
- Mandatory `failed`, `skipped`, or `unavailable` verification outcomes block readiness.
- Use stable codes and sorted deterministic JSON output.
- Never echo credentials, DSNs, authenticated locations, raw values, evidence bodies, interview answers, or driver errors.
- Follow TDD for every behavior change.
- Use `uv` for every command.
- Preserve unrelated worktree and staged changes.

## File structure

### Root and core files

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.gitignore`
- Modify: `src/selayer/model.py`
- Modify: `src/selayer/catalog.py`
- Modify: `src/selayer/sources/base.py`
- Create: `src/selayer/sources/scan.py`
- Modify: `src/selayer/sources/registry.py`
- Modify: `src/selayer/sources/adapters/arrow.py`
- Modify: `src/selayer/sources/adapters/database.py`
- Modify: `src/selayer/sources/adapters/delta.py`
- Modify: `src/selayer/sources/adapters/iceberg.py`
- Modify: `src/selayer/sources/__init__.py`
- Modify: `src/selayer/verification/static.py`
- Modify: `src/selayer/verification/compatibility.py`
- Modify: `src/selayer/okf/generation.py`
- Modify: `src/selayer/okf/validation.py`
- Modify: `tests/test_catalog.py`
- Modify: `tests/verification/test_static.py`
- Modify: `tests/verification/test_compatibility.py`
- Modify: `tests/okf/test_generation.py`
- Create: `tests/sources/test_scan_session.py`

### Companion package

```text
packages/selayer-discovery/
├── pyproject.toml
├── README.md
├── skills/semantic-discovery/SKILL.md
├── src/selayer_discovery/
│   ├── __init__.py
│   ├── approval.py
│   ├── canonical.py
│   ├── cli.py
│   ├── diagnostics.py
│   ├── evidence.py
│   ├── interview.py
│   ├── knowledge.py
│   ├── model.py
│   ├── profiling.py
│   ├── proposal.py
│   ├── session.py
│   └── transaction.py
└── tests/
    ├── conftest.py
    ├── test_approval.py
    ├── test_canonical.py
    ├── test_cli.py
    ├── test_evidence.py
    ├── test_interview.py
    ├── test_knowledge.py
    ├── test_profiling.py
    ├── test_proposal.py
    ├── test_session.py
    ├── test_skill.py
    └── test_transaction.py
```

### Skill and shopfloor files

- Create: `.agents/skills/semantic-discovery/SKILL.md`
- Create: `examples/shopfloor/discovery/README.md`
- Create: `examples/shopfloor/discovery/charter.yaml`
- Create: `examples/shopfloor/discovery/replay/interview.jsonl`
- Create: `examples/shopfloor/discovery/replay/policy.yaml`
- Create: `examples/shopfloor/discovery/replay/proposal.yaml`
- Create: `examples/shopfloor/discovery/replay/wiki-query.json`
- Create: `examples/shopfloor/discovery/replay/defect.yaml`
- Create: `packages/selayer-discovery/tests/test_shopfloor_replay.py`
- Modify: `README.md`
- Modify: `.github/copilot-instructions.md`

---

## Stage 0: workspace and deterministic core seams

### Task 1: Convert the repository to a uv workspace and scaffold the companion

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `packages/selayer-discovery/pyproject.toml`
- Create: `packages/selayer-discovery/README.md`
- Create: `packages/selayer-discovery/src/selayer_discovery/__init__.py`
- Create: `packages/selayer-discovery/src/selayer_discovery/cli.py`
- Create: `packages/selayer-discovery/tests/test_cli.py`

**Interfaces:**
- Produces: independently buildable `selayer-discovery` package and executable.
- Preserves: root `selayer` installation and existing console scripts.

- [ ] **Step 1: Write failing workspace smoke tests**

```python
# packages/selayer-discovery/tests/test_cli.py
from selayer_discovery import __version__
from selayer_discovery.cli import main


def test_package_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_is_a_usage_exit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert "selayer-discovery" in capsys.readouterr().out
```

Add a root subprocess test that imports `selayer` without importing `selayer_discovery`.

- [ ] **Step 2: Run the tests and confirm the member does not exist**

```bash
uv run pytest packages/selayer-discovery/tests/test_cli.py -q
```

Expected: collection fails because the package is absent.

- [ ] **Step 3: Add the workspace declaration**

```toml
[tool.uv.workspace]
members = ["packages/selayer-discovery"]
```

Extend root pytest paths and Pyright paths to include the member tests and source. Do not move the root project into a subdirectory.

- [ ] **Step 4: Create the member `pyproject.toml`**

Use:

```toml
[project]
name = "selayer-discovery"
version = "0.1.0"
description = "Deterministic agent-assisted semantic discovery for selayer."
requires-python = ">=3.13"
dependencies = [
  "filelock>=3.20,<4",
  "ruamel-yaml>=0.18,<0.19",
  "selayer>=0.1.0",
]

[project.scripts]
selayer-discovery = "selayer_discovery.cli:run"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/selayer_discovery"]

[tool.uv.sources]
selayer = { workspace = true }
```

Do not add a model, HTTP, OCR, or vector dependency.

- [ ] **Step 5: Implement the minimal CLI**

```python
# packages/selayer-discovery/src/selayer_discovery/cli.py
from collections.abc import Sequence
import argparse


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="selayer-discovery")


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    return 0


def run() -> None:
    raise SystemExit(main())
```

- [ ] **Step 6: Lock and verify both packages**

```bash
uv lock
uv sync --all-packages --extra delta
uv run python -c "import selayer; print(selayer.__name__)"
uv run --package selayer-discovery python -c "import selayer_discovery; print(selayer_discovery.__version__)"
uv run --package selayer-discovery selayer-discovery --help
uv build
uv build --package selayer-discovery
```

Expected: all commands succeed; the root wheel contains no `selayer_discovery` package.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock packages/selayer-discovery
git commit -m "build(discovery): add workspace package"
```

### Task 2: Add first-class deprecation metadata to catalog objects

**Files:**
- Modify: `src/selayer/model.py`
- Modify: `src/selayer/catalog.py`
- Modify: `tests/test_catalog.py`

**Interfaces:**
- Produces: `SemanticStatus`, `status`, and `replaced_by` on all semantic objects.
- Preserves: unchanged behavior for catalogs without these fields.

- [ ] **Step 1: Write failing model and parsing tests**

Cover all six kinds: source, dimension, fact, measure, metric, and relationship.

```python
def test_catalog_parses_deprecation_metadata(tmp_path: Path) -> None:
    layer = SemanticLayer.load(_catalog_with_deprecations(tmp_path))
    assert layer.metric("old_rate").status is SemanticStatus.DEPRECATED
    assert layer.metric("old_rate").replaced_by == "metric.new_rate"
    assert layer.metric("new_rate").status is SemanticStatus.ACTIVE
    assert layer.metric("new_rate").replaced_by is None
```

Add failures for unknown status, non-string replacement, and `replaced_by` on an active object.

- [ ] **Step 2: Run focused tests**

```bash
uv run pytest tests/test_catalog.py -k "deprecat or semantic_status" -q
```

Expected: failures because the fields do not exist.

- [ ] **Step 3: Add immutable model fields**

```python
from enum import StrEnum


class SemanticStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
```

Append these defaults to `DataSource`, `Dimension`, `Fact`, `Measure`, `Metric`, and `Relationship` so existing positional construction remains valid:

```python
status: SemanticStatus = SemanticStatus.ACTIVE
replaced_by: str | None = None
```

- [ ] **Step 4: Validate and construct metadata in the catalog parser**

Add one shared helper that:

- accepts only `active` and `deprecated`;
- defaults missing status to active;
- rejects replacement on active objects;
- returns typed values;
- never silently ignores a malformed field.

Pass metadata by keyword at every object constructor.

- [ ] **Step 5: Run catalog and model tests**

```bash
uv run pytest tests/test_catalog.py tests/test_model.py -q
uv run pyright src/selayer/model.py src/selayer/catalog.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/model.py src/selayer/catalog.py tests/test_catalog.py
git commit -m "feat(catalog): add semantic deprecation metadata"
```

### Task 3: Validate and report deprecation replacement graphs

**Files:**
- Modify: `src/selayer/verification/static.py`
- Modify: `src/selayer/verification/compatibility.py`
- Modify: `src/selayer/okf/generation.py`
- Modify: `src/selayer/okf/validation.py`
- Modify: `tests/verification/test_static.py`
- Modify: `tests/verification/test_compatibility.py`
- Modify: `tests/okf/test_generation.py`

**Interfaces:**
- Produces: replacement validation, compatibility notices, and generated OKF migration metadata.
- Preserves: planning and execution of deprecated IDs.

- [ ] **Step 1: Write failing replacement-graph tests**

Use stable codes:

```text
catalog.deprecation.replacement_missing
catalog.deprecation.replacement_kind
catalog.deprecation.self_replacement
catalog.deprecation.cycle
catalog.deprecation.notice
```

Assert missing, cross-kind, self, and cyclic replacements fail static validation. Assert valid same-kind chains pass.

- [ ] **Step 2: Write failing behavior tests**

Assert:

- a deprecated metric still plans and executes;
- compatibility reports `compatible: true` plus deprecated ID and replacement;
- generated OKF uses `status: deprecated` and links the replacement concept;
- active generated concepts retain current status behavior.

- [ ] **Step 3: Run focused tests**

```bash
uv run pytest \
  tests/verification/test_static.py \
  tests/verification/test_compatibility.py \
  tests/okf/test_generation.py \
  -k "deprecat or replacement" -q
```

Expected: failures for missing graph behavior.

- [ ] **Step 4: Implement graph validation**

Traverse fully qualified semantic IDs from `SemanticLayer.semantic_objects()`. Validate kind before cycle detection. Emit sorted diagnostics and one non-blocking notice per deprecated object.

Do not auto-rewrite requests or remove deprecated objects from indexes.

- [ ] **Step 5: Add compatibility and OKF reporting**

Compatibility outcomes list directly requested deprecated metrics or dimensions and transitively used deprecated measures, facts, sources, and relationships. Generated concepts link `replaced_by` through the existing concept-path function.

- [ ] **Step 6: Run regression tests**

```bash
uv run pytest tests/verification tests/okf tests/planning -q
```

Expected: all tests pass; existing catalogs remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add \
  src/selayer/verification/static.py \
  src/selayer/verification/compatibility.py \
  src/selayer/okf/generation.py \
  src/selayer/okf/validation.py \
  tests/verification/test_static.py \
  tests/verification/test_compatibility.py \
  tests/okf/test_generation.py
git commit -m "feat(verification): report semantic deprecations"
```

### Task 4: Add the bounded public source scan-session contract

**Files:**
- Modify: `src/selayer/sources/base.py`
- Create: `src/selayer/sources/scan.py`
- Modify: `src/selayer/sources/registry.py`
- Modify: `src/selayer/sources/__init__.py`
- Create: `tests/sources/test_scan_session.py`

**Interfaces:**
- Produces: `SourceConsistency`, `SourceScanSession`, and `SourceRegistry.open_scan_session()`.
- Consumes: existing adapter handles, registry locking, and query-scoped binding.

- [ ] **Step 1: Write failing contract tests with a fake adapter**

Test:

- known-column and batch-size validation;
- typed `pyarrow.RecordBatch` iteration;
- one active iterator per session;
- cancellation;
- session cleanup after success and failure;
- registry reload, close, query binding, and execution block while a scan session owns the same registry lock;
- discovery profiling uses a dedicated registry so application queries and verification use a separate lock;
- a scan uses the environment-backed `RuntimeProfileResolver` supplied at registry construction and exposes no resolved profile values;
- no public raw connection or handle attribute;
- sanitized error codes.

- [ ] **Step 2: Run the contract test**

```bash
uv run pytest tests/sources/test_scan_session.py -q
```

Expected: collection failure because the interface is absent.

- [ ] **Step 3: Define public immutable scan types**

```python
class SourceConsistency(StrEnum):
    REOPENABLE_SNAPSHOT = "reopenable_snapshot"
    TRANSACTION_SNAPSHOT = "transaction_snapshot"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    consistency: SourceConsistency
    snapshot_id: str | None
    schema_fingerprint: str
```

`SourceScanSession` exposes only `source_id`, `schema`, `consistency`, `snapshot_id`, `iter_batches()`, `recheck_snapshot()`, `cancel()`, and context-manager methods. `SourceSnapshot` is a derived public view over the existing canonical `SourceHandle.snapshot` token plus consistency and schema fingerprint; it is not a second snapshot authority.

- [ ] **Step 4: Extend internal handles honestly**

Append `consistency: SourceConsistency = SourceConsistency.LIVE` to `SourceHandle`. Reuse the existing `SourceHandle.snapshot` field as the one canonical adapter token. Derive `SourceSnapshot.snapshot_id` from it and derive schema fingerprint through the existing schema helper. Keep resources and callbacks private and excluded from repr. Add a test proving status, reload, and scan sessions report the same token.

- [ ] **Step 5: Extract registry requirement binding**

Refactor `SourceRegistry.bind()` so query plans and scan sessions share one private requirement-binding context. Do not copy query-scoped adapter preparation logic.

`open_scan_session(source_id)` uses the `RuntimeProfileResolver` already bound when the registry was created; it accepts no credential override. It must:

- acquire the registry lifecycle lock for the full session;
- bind only the requested source;
- materialize query-scoped sources through the same private requirement-binding path used by query plans, then stream every connector from its registered stable name;
- validate selected columns against the observed schema;
- quote identifiers internally;
- stream Arrow batches from the stable name after binding;
- sanitize DuckDB and adapter failures;
- release bindings and lock exactly once.

Document that the lock blocks queries on that same registry. `selayer-discovery` must construct a dedicated registry and connection from the charter's runtime profile resolver for profiling. Proposal verification constructs another fresh registry and never runs inside an open profile session.

No caller supplies SQL.

- [ ] **Step 6: Implement cancellation and snapshot recheck hooks**

Cancellation interrupts the active internal cursor and marks the session unusable. Recheck prepares a fresh candidate through the same adapter and compares consistency, snapshot ID, and schema fingerprint before closing it.

- [ ] **Step 7: Run source lifecycle tests**

```bash
uv run pytest tests/sources/test_scan_session.py tests/sources/test_registry.py tests/sources/test_public_lifecycle_api.py -q
uv run pyright src/selayer/sources
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  src/selayer/sources/base.py \
  src/selayer/sources/scan.py \
  src/selayer/sources/registry.py \
  src/selayer/sources/__init__.py \
  tests/sources/test_scan_session.py
git commit -m "feat(sources): add bounded scan sessions"
```

### Task 5: Implement truthful consistency modes for built-in adapters

**Files:**
- Modify: `src/selayer/sources/adapters/arrow.py`
- Modify: `src/selayer/sources/adapters/database.py`
- Modify: `src/selayer/sources/adapters/delta.py`
- Modify: `src/selayer/sources/adapters/iceberg.py`
- Modify: `tests/sources/test_arrow_adapter.py`
- Modify: `tests/sources/test_database_adapters.py`
- Modify: `tests/sources/test_delta_adapter.py`
- Modify: `tests/sources/test_iceberg_adapter.py`
- Modify: `tests/sources/test_scan_session.py`

**Interfaces:**
- Produces: adapter-specific consistency and safe snapshot tokens.
- Rejects: claiming reopenability when the same revision cannot be reacquired.

- [ ] **Step 1: Write failing consistency matrix tests**

Expected matrix:

| Connector | Reopenable condition | Otherwise |
|---|---|---|
| local CSV/Parquet | content digest over sorted physical files | live for unversioned remote objects |
| local read-only DuckDB/SQLite | file digest unchanged before and after session | transaction or live |
| Delta | table version can be reopened | live if version cannot be pinned |
| Iceberg | snapshot ID can be selected explicitly | live if adapter cannot pin it |
| Postgres | none in version 1 | live |
| PyArrow provider | none in version 1 | live |

- [ ] **Step 2: Run adapter tests and confirm current handles lack consistency**

```bash
uv run pytest tests/sources -k "consistency or snapshot_recheck" -q
```

Expected: failures.

- [ ] **Step 3: Add safe local content fingerprints**

Hash file contents, not authenticated locations. For glob and directory inputs, hash sorted relative paths plus each content digest. Never put the location into repr, errors, or reports.

For local database files, require read-only access and compare the file digest at session entry and exit. A changed file makes recheck fail.

- [ ] **Step 4: Pin Delta and supported Iceberg revisions**

Reopen using the recorded version or snapshot ID. Add mutation tests that append a new version and prove the old token still opens the approved revision.

If the installed library cannot reopen an Iceberg snapshot through the current adapter, report `live`; do not simulate support.

- [ ] **Step 5: Mark transaction and live adapters**

Postgres remains `live` in version 1 because the DuckDB-backed adapter does not expose one pinned repeatable-read transaction through the scan session. Provider-backed Arrow also remains `live`. Add no stronger mode until an adapter test proves one internally consistent snapshot and, for executable evidence, a reopenable token.

- [ ] **Step 6: Run the full adapter matrix**

```bash
uv run pytest tests/sources -q
```

Expected: all tests pass and every adapter reports an explicit mode.

- [ ] **Step 7: Commit**

```bash
git add src/selayer/sources/adapters tests/sources
git commit -m "feat(sources): classify scan snapshot consistency"
```

---

## Stage 1: companion session and evidence foundation

### Task 6: Add canonical artifacts and safe diagnostics

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/canonical.py`
- Create: `packages/selayer-discovery/src/selayer_discovery/diagnostics.py`
- Create: `packages/selayer-discovery/src/selayer_discovery/model.py`
- Create: `packages/selayer-discovery/tests/test_canonical.py`

**Interfaces:**
- Produces: versioned artifact base types, canonical JSON, SHA-256 fingerprints, and safe diagnostics.

- [ ] **Step 1: Write failing canonicalization tests**

Cover mapping-order independence, semantic list-order preservation, normalized enums, dataclasses, dates, numbers, forbidden NaN/infinity, maximum nesting, maximum items, and timestamps excluded from semantic payloads.

- [ ] **Step 2: Write failing diagnostic secrecy tests**

Create exceptions containing a DSN, token, source row, and document text. Assert `repr`, `str`, JSON, stdout, and stderr contain only stable code and safe IDs.

- [ ] **Step 3: Run tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_canonical.py -q
```

Expected: collection failure.

- [ ] **Step 4: Implement strict canonical JSON**

```python
def canonical_bytes(value: object) -> bytes:
    normalized = normalize_artifact(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
```

Reject unsupported objects rather than calling `str()`.

- [ ] **Step 5: Define shared artifact enums and IDs**

Include evidence class, group status, gate disposition, actor identity normalization, schema version, and bounded text/collection validators. Do not put behavior-heavy session logic in `model.py`.

- [ ] **Step 6: Run quality checks**

```bash
uv run pytest packages/selayer-discovery/tests/test_canonical.py -q
uv run ruff check packages/selayer-discovery/src/selayer_discovery
uv run pyright packages/selayer-discovery/src
```

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add canonical artifact model"
```

### Task 7: Add the append-only session store and state machine

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/session.py`
- Create: `packages/selayer-discovery/tests/conftest.py`
- Create: `packages/selayer-discovery/tests/test_session.py`

**Interfaces:**
- Produces: `SessionCharter`, hash-chained events, state reconstruction, session locks, and stale propagation primitives.

- [ ] **Step 1: Write failing state-transition tests**

Test every allowed transition and reject direct skips, duplicate terminal events, mutation after close, and state revived by editing a cache.

- [ ] **Step 2: Write failing event-integrity tests**

Every event contains schema version, event ID, previous hash, event hash, actor, UTC timestamp, type, and bounded payload. Tampering, truncation, reordering, and duplicate IDs must fail reconstruction.

- [ ] **Step 3: Write failing concurrency and permissions tests**

Two writers serialize through `filelock`; timeout reports safe owner metadata. Session directories use `0700` and files use `0600` where supported.

- [ ] **Step 4: Implement `SessionStore`**

Use append, flush, and `os.fsync()` before publishing materialized state. Rebuild state from the journal on every test path; a cache may optimize reads but is never authority.

- [ ] **Step 5: Add dependency invalidation events**

Store an explicit directed dependency index. A changed charter, approver, evidence revision, policy, answer, claim, conflict, proposal, or verification hash emits sorted stale targets transitively.

- [ ] **Step 6: Run tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_session.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery/src/selayer_discovery/session.py packages/selayer-discovery/tests
git commit -m "feat(discovery): add event-sourced sessions"
```

### Task 8: Add workspace policy and session CLI commands

**Files:**
- Modify: `.gitignore`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`
- Modify: `packages/selayer-discovery/tests/test_cli.py`

**Interfaces:**
- Produces: `session init|status|close` with deterministic JSON and ignored raw workspace.

- [ ] **Step 1: Write failing CLI tests**

Assert:

- charter requires business question, inclusions, exclusions, acceptance questions, catalog, and approver;
- catalog path and summary root are project-contained;
- `session init` creates one ignored workspace;
- repeated init with same explicit ID fails;
- status rebuilds from events;
- JSON keys and diagnostics are sorted;
- exit codes are `0`, `1`, and `2` as specified.

- [ ] **Step 2: Add targeted ignores**

```gitignore
.selayer/discovery/sessions/
.selayer/discovery/transactions/
```

Do not ignore `.selayer/discovery.yaml` or `semantic_changes/`.

- [ ] **Step 3: Implement command groups**

Use argparse subparsers and handler functions. Parse structured inputs before acquiring mutation locks. Errors return safe diagnostics and never a traceback by default.

- [ ] **Step 4: Verify Git visibility**

```bash
git check-ignore .selayer/discovery/sessions/example
git check-ignore .selayer/discovery/transactions/example
```

Expected: both paths are ignored. Use a pytest temporary Git repository for session-init assertions; do not create fixture sessions in the real repository.

- [ ] **Step 5: Run CLI tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_cli.py -q
```

Expected: all session CLI tests pass.

- [ ] **Step 6: Commit**

```bash
git add .gitignore packages/selayer-discovery
git commit -m "feat(discovery): add session CLI"
```

### Task 9: Add normalized document intake and evidence manifests

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/evidence.py`
- Create: `packages/selayer-discovery/tests/test_evidence.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: content-addressed Markdown/text snapshots and immutable evidence records.

- [ ] **Step 1: Write failing intake tests**

Cover:

- `.md` and `.txt` only;
- UTF-8 and normalized newlines;
- document, total-byte, path-depth, and item limits;
- duplicate content reuse with distinct source records;
- changed file creates a new revision;
- special files, symlinks, path escapes, NUL, and invalid encoding rejected;
- no body in repr or diagnostic JSON.

- [ ] **Step 2: Implement preflight before reads**

Resolve allowed roots, use `lstat`, reject symbolic links and non-regular files, enforce size before reading, then hash bytes and write with exclusive creation and fsync.

- [ ] **Step 3: Add evidence records and selectors**

Implement typed selectors for document line ranges, catalog JSON paths, source fields, provider sections, interview event IDs, and verification outcomes. Selectors validate against the recorded revision.

- [ ] **Step 4: Add `intake add-document|snapshot` commands**

Commands return record ID, safe source label, media type, size, and hash. They never return content unless an explicit bounded evidence-read command is added later.

- [ ] **Step 5: Run evidence tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_evidence.py packages/selayer-discovery/tests/test_cli.py -q
```

Expected: all intake, path-confinement, and secrecy tests pass.

- [ ] **Step 6: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): capture normalized evidence"
```

---

## Stage 2: profiling, redaction, and external knowledge

### Task 10: Add exact bounded aggregate profiling

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/profiling.py`
- Create: `packages/selayer-discovery/tests/test_profiling.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: exact aggregate profiles from `SourceScanSession` without model-visible values.

- [ ] **Step 1: Write failing profile tests**

Assert exact rows, nulls, distinct counts, numeric/date ranges, grain duplicates, consistency mode, snapshot ID, and schema fingerprint across multiple batches.

Add timeout, cancellation, unsupported type, partial iterator failure, resume, and cleanup tests. Partial outputs must never produce claims.

- [ ] **Step 2: Implement one-pass local accumulation with bounded spill**

Use restrictive temporary storage under the ignored session. Spill typed Arrow batches and run exact DuckDB aggregates in the companion process. Delete spill files after a committed aggregate artifact. Never include their paths or values in diagnostics.

- [ ] **Step 3: Enforce consistency rules**

- Reopenable profiles may resume only with the same snapshot token and batch hashes.
- Transaction and live profiles restart.
- The profile records its mode, but only reopenable evidence may later authorize data-dependent executable operations.

- [ ] **Step 4: Implement timeout and cancellation**

Default to 900 seconds per source. On deadline, call `SourceScanSession.cancel()`, wait for cleanup, discard partial aggregates, and record `unavailable` with a stable code.

- [ ] **Step 5: Add `profile scan`**

Return counts and outcome metadata only. Do not return top values, example rows, spill paths, or source locations.

- [ ] **Step 6: Run profile tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_profiling.py -q
```

Expected: exact profiles pass; timeout and partial results remain unavailable.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add exact aggregate profiles"
```

### Task 11: Add sample-policy activation and deterministic redaction

**Files:**
- Modify: `packages/selayer-discovery/src/selayer_discovery/profiling.py`
- Modify: `packages/selayer-discovery/tests/test_profiling.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: conservative policy suggestions, named activation, bounded transformed samples, and model-context export.

- [ ] **Step 1: Write failing policy tests**

Cover exact policy schema, default omit, unknown fields, hard-denied credential classes, reveal restrictions, caps that may only decrease, actor mismatch, changed profile, changed approver, and changed policy fingerprint.

- [ ] **Step 2: Write canary leakage tests**

Seed credentials, private keys, emails, names, serials, free text, and prompt-injection strings. Assert they do not appear in policy suggestions, hashes, diagnostics, context export, reprs, or exception chains unless a non-hard-denied field has explicit reveal approval.

- [ ] **Step 3: Implement local conservative classification**

Use field name/type patterns and local value tests. Return classes and reasons, never matching values. Credential and private-key classifications are non-overridable hard deny.

- [ ] **Step 4: Implement named activation**

Activation binds normalized approver, policy hash, profile hash, and snapshot IDs. Any mismatch stales activation and downstream artifacts.

- [ ] **Step 5: Implement deterministic sample selection**

Keep the 20 smallest session-salted hashes of source ID plus declared grain values. Require valid grain. Apply omit, redact, salted hash, bucket, or reveal before writing output.

Enforce hard limits before publication:

```text
20 rows/source
50 fields/source
64 KiB/source
256 KiB/session
100 revealed distinct values/approved low-cardinality field
```

- [ ] **Step 6: Add CLI commands**

```text
profile propose-policy
profile activate-policy
profile export-context
```

`export-context` runs a final canary and hard-deny scan and emits only a path/hash summary to stdout.

- [ ] **Step 7: Run policy and privacy tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_profiling.py packages/selayer-discovery/tests/test_cli.py -q
```

Expected: all tests pass and no canary value escapes.

- [ ] **Step 8: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): gate redacted source context"
```

### Task 12: Add read-only knowledge providers and filesystem OKF

**Files:**
- Modify: `packages/selayer-discovery/pyproject.toml`
- Create: `packages/selayer-discovery/src/selayer_discovery/knowledge.py`
- Create: `packages/selayer-discovery/tests/test_knowledge.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: `KnowledgeProvider.search|get` and the `okf-filesystem` adapter.

- [ ] **Step 1: Write failing protocol tests**

Test namespaced IDs, immutable revisions, duplicate provider names, zero or multiple providers, entry-point loading, result/item/byte caps, malformed provider output, timeout, and sanitized provider failures.

- [ ] **Step 2: Write prompt-injection inertness tests**

A concept body containing commands, tool requests, policy changes, approval text, or apply instructions must be returned only as quoted evidence content. It cannot create events or CLI invocations.

- [ ] **Step 3: Implement the protocol and registry**

Use immutable `KnowledgeSearchRequest`, `KnowledgeHit`, `KnowledgeGetRequest`, and `KnowledgeDocument`. Provider configuration stores non-secret values and environment references only.

Reject entry points outside `selayer_discovery.knowledge_providers`. Do not support executable paths or subprocesses.

- [ ] **Step 4: Implement filesystem OKF and register its entry point**

Load with `OkfBundle`, derive revision from the strict bundle/concept hash, delegate bounded retrieval, and preserve effective source attribution. The adapter is read-only and exposes no write method.

Only now add:

```toml
[project.entry-points."selayer_discovery.knowledge_providers"]
okf-filesystem = "selayer_discovery.knowledge:FilesystemOkfProvider"
```

Test entry-point resolution from the built wheel so no earlier task contains a dangling target.

- [ ] **Step 5: Add `intake add-provider`**

Validate provider name, registered type, non-secret options, and environment-variable references. Reject duplicate names, unknown providers, credential/token/password literals, executable paths, and subprocess configuration. Store only the provider configuration fingerprint and safe metadata in the session.

- [ ] **Step 6: Snapshot provider results before model use**

`intake snapshot` stores provider name, resource ID, revision, selector, size, and content hash. Changed revisions create new evidence records and stale dependents.

- [ ] **Step 7: Run provider and CLI tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_knowledge.py packages/selayer-discovery/tests/test_evidence.py packages/selayer-discovery/tests/test_cli.py -q
uv build --package selayer-discovery
```

Expected: provider registration, add-provider validation, bounds, snapshot, wheel resolution, and inert-content tests pass.

- [ ] **Step 8: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add read-only knowledge providers"
```

---

## Stage 3: interviews, claims, conflicts, and skill workflow

### Task 13: Add adaptive interview gates and append-only corrections

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/interview.py`
- Create: `packages/selayer-discovery/tests/test_interview.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: required gates, one open question, answers, corrections, and gate dispositions.

- [ ] **Step 1: Write failing gate tests**

Encode all fifteen design gates as stable IDs. Assert every group declares affecting gates and no group becomes ready with an undisposed affecting gate.

- [ ] **Step 2: Write failing question tests**

A question must cite one gate, motivating evidence IDs, and affected subjects. Reject a second open question, oversized text, evidence-body interpolation, and out-of-charter subject expansion.

- [ ] **Step 3: Write failing correction tests**

A correction cites one current answer, author, reason, and replacement. It preserves history, makes the old answer superseded, and emits stale targets for dependent claims, conflicts, proposals, reports, and attestations.

- [ ] **Step 4: Implement gate dispositions**

Support `answered`, `not_applicable`, and `blocked`. `not_applicable` requires a reason. `blocked` requires conflict IDs and affected group IDs.

- [ ] **Step 5: Add CLI commands**

```text
interview ask
interview answer
interview correct
interview set-gate
```

Commands import structured files; they do not ask an LLM.

- [ ] **Step 6: Run interview tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_interview.py -q
```

Expected: all gate, one-question, disposition, and correction tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add auditable interviews"
```

### Task 14: Add typed claims, conflicts, and transitive invalidation

**Files:**
- Modify: `packages/selayer-discovery/src/selayer_discovery/evidence.py`
- Modify: `packages/selayer-discovery/tests/test_evidence.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: observed/asserted/inferred claims and affected-group conflict blocking.

- [ ] **Step 1: Write failing claim tests**

Require subject, declarative statement, evidence selectors, class, and creator event. Reject inferred-only evidence for executable operations and selectors against stale revisions.

- [ ] **Step 2: Write failing conflict tests**

Assert:

- unresolved conflicts block only affected groups;
- independent groups stay eligible;
- semantic resolution actor must match named approver;
- deterministic failures cannot be resolved by attestation;
- approver change stales semantic resolutions.

- [ ] **Step 3: Implement typed records**

Do not add numeric confidence. Keep source observation scope explicit in the claim statement and selector.

- [ ] **Step 4: Add evidence CLI commands**

```text
evidence add-claim
evidence add-conflict
evidence resolve-conflict
```

A resolution records statement, answer/evidence ID, actor, and timestamp; it never deletes contrary claims.

- [ ] **Step 5: Run claim and invalidation tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_evidence.py packages/selayer-discovery/tests/test_session.py -q
```

Expected: all claim, conflict, actor, and transitive-staleness tests pass.

- [ ] **Step 6: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): track claims and conflicts"
```

### Task 15: Add the canonical Agent Skill for intake and interviews

**Files:**
- Create: `packages/selayer-discovery/skills/semantic-discovery/SKILL.md`
- Create: `.agents/skills/semantic-discovery/SKILL.md`
- Create: `packages/selayer-discovery/tests/test_skill.py`
- Modify: `packages/selayer-discovery/pyproject.toml`

**Interfaces:**
- Produces: host-agent workflow that calls deterministic companion commands.
- Excludes: direct state-file edits and model-provider code.

- [ ] **Step 1: Write failing skill-contract tests**

Assert the canonical skill requires:

- charter before intake;
- one question at a time;
- untrusted evidence treatment;
- sample-policy approval before values;
- companion CLI for every mutation;
- no direct wiki writes;
- no apply without group and batch attestation;
- no Git operations;
- no `verified` claims from agent reasoning.

- [ ] **Step 2: Write the canonical packaged skill**

Follow the repository skill format. Include exact command flow and error behavior. Tell the agent to stop and report blocked/unavailable checks rather than bypass them.

- [ ] **Step 3: Add the repository forwarding skill**

The root skill contains metadata and an instruction to read the canonical package skill by relative path. Do not duplicate the workflow body.

- [ ] **Step 4: Package and inspect the skill**

Add Hatch `force-include` so the wheel contains one canonical `skills/semantic-discovery/SKILL.md`. Test the wheel archive and root forwarder target.

- [ ] **Step 5: Run skill and package tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_skill.py -q
uv build --package selayer-discovery
```

Expected: the skill contract passes and the wheel contains one canonical skill body.

- [ ] **Step 6: Commit**

```bash
git add packages/selayer-discovery .agents/skills/semantic-discovery
git commit -m "feat(discovery): add semantic discovery skill"
```

---

## Stage 4: typed proposals and deterministic verification

### Task 16: Add typed catalog and knowledge operations

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/proposal.py`
- Create: `packages/selayer-discovery/tests/test_proposal.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: add/edit/deprecate operations, Reference/overlay operations, candidate reconstruction, and derived impacts.

- [ ] **Step 1: Write failing operation-schema tests**

Test complete before/after state, target ID, before hash, claim IDs, dependencies, unknown keys, missing fields, oversized prose, and stable operation ordering.

Reject:

- delete;
- rename;
- target kind change;
- edit outside the target object;
- arbitrary patch input;
- generated OKF target;
- path escape;
- generated frontmatter or `Catalog Definition` overlay edit.

- [ ] **Step 2: Write failing candidate reconstruction tests**

Use ruamel.yaml round-trip parsing. Preserve comments, key order outside changed objects, quoting where untouched, and newline style. Assert the reconstructed catalog loads to the exact expected `SemanticLayer`.

- [ ] **Step 3: Implement normalized operations**

```text
catalog.add
catalog.edit
catalog.deprecate
reference.create
reference.update
overlay.create
overlay.update
```

The companion derives changed fields and impact flags from normalized before/after state. It ignores any agent-supplied impact list.

- [ ] **Step 4: Enforce atomic dependency groups**

Reject dependency cycles. A group contains rationale, current non-inferred claims, affecting gates, conflict IDs, query cases, operations, and dependencies.

- [ ] **Step 5: Render deterministic review previews**

Render `catalog.patch` and knowledge diffs from reconstructed files. Never parse these previews during verify or apply.

- [ ] **Step 6: Add proposal commands**

```text
proposal import
proposal show
```

- [ ] **Step 7: Run operation and reconstruction tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_proposal.py -q
```

Expected: typed operations reconstruct candidates and every prohibited mutation is rejected.

- [ ] **Step 8: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add typed semantic proposals"
```

### Task 17: Add impact-derived verification readiness

**Files:**
- Modify: `packages/selayer-discovery/src/selayer_discovery/proposal.py`
- Modify: `packages/selayer-discovery/tests/test_proposal.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: `VerificationBundle`, mandatory-check matrix, semantic query cases, and review-ready groups.

- [ ] **Step 1: Write failing mandatory-check matrix tests**

Map impacts exactly:

| Impact | Required evidence |
|---|---|
| any catalog operation | static check |
| source/schema/grain | reopenable snapshot plus exact source audit |
| relationship | reopenable snapshot plus cardinality and RI audit |
| type/expression | static source-column/type checks; reopenable data evidence when cited |
| measure/metric formula | static, compatibility, and acceptance cases |
| deprecation | replacement graph and affected compatibility |
| Reference/overlay | fresh OKF build, strict integrity, curated policy |

- [ ] **Step 2: Write failing query-case tests**

Support expected compatible plans, expected stable planner rejection codes, and optional bounded execution assertions. Reject SQL, callable assertions, unrestricted row capture, and unknown result operators.

- [ ] **Step 3: Implement verification delegation**

Call only core public verification, planner, QueryEngine, and `OkfBundle.build()` APIs. Store safe outcomes and result digests. Never duplicate grain or relationship queries.

- [ ] **Step 4: Enforce readiness**

A group becomes ready only when all affecting gates are disposed, current non-inferred claims exist, conflicts are resolved, dependencies are ready/accepted, snapshots are reopenable where required, and every mandatory check is complete and passed.

- [ ] **Step 5: Add `proposal verify`**

Verification reconstructs a fresh candidate and writes an immutable report bound to all input hashes. A second run with unchanged inputs has the same semantic fingerprint.

- [ ] **Step 6: Run readiness tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_proposal.py -q
```

Expected: every impact triggers its mandatory checks and incomplete evidence blocks readiness.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): verify proposal readiness"
```

### Task 18: Add group decisions, apply-batch preparation, and approved-summary preview

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/approval.py`
- Create: `packages/selayer-discovery/tests/test_approval.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`

**Interfaces:**
- Produces: group attestations, non-overlapping dependency-closed batches, batch attestations, and safe ignored summary previews.

- [ ] **Step 1: Write failing group-attestation tests**

Reject actor mismatch, blocked/stale/incomplete groups, changed charter, policy, evidence, candidate, or verification. Record the fixed statement that attestation is not a digital signature.

- [ ] **Step 2: Write failing batch tests**

Require an explicit ordered group list, common base, dependency closure, accepted status, and no overlap on semantic targets or curated sections. Applying a prior batch must stale unapplied groups based on the old catalog.

- [ ] **Step 3: Implement `prepare-apply`**

Reconstruct the combined candidate, run the union of mandatory checks, and hash the group list, attestations, base, candidate, verification, and approved summary.

- [ ] **Step 4: Implement batch attestation**

Require the current named approver and exact prepared-batch hash. Changing selection requires a new prepare and attestation.

- [ ] **Step 5: Render approved-summary previews safely**

Write the attested preview under `.selayer/discovery/sessions/<id>/exports/<batch-hash>/` with only:

```text
decision.md
proposal.yaml
evidence.lock.json
catalog.patch
references/
overlays/
verification.json
approval.json
```

Scan previews to prove they contain no document bodies, sample values, full interview answers, salts, credentials, runtime values, or backup paths. Task 20 publishes the identical hash-bound summary to `semantic_changes/` inside the apply transaction; Task 18 must not write a Git-visible summary.

- [ ] **Step 6: Add commands**

```text
proposal attest
proposal prepare-apply
proposal attest-apply
proposal export-preview
```

- [ ] **Step 7: Run approval and batch tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_approval.py packages/selayer-discovery/tests/test_proposal.py -q
```

Expected: actor, dependency, overlap, fingerprint, batch, and ignored-preview tests pass.

- [ ] **Step 8: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): attest approved change batches"
```

---

## Stage 5: recoverable explicit apply

### Task 19: Add fsynced write-ahead journals and idempotent recovery

**Files:**
- Create: `packages/selayer-discovery/src/selayer_discovery/transaction.py`
- Create: `packages/selayer-discovery/tests/test_transaction.py`

**Interfaces:**
- Produces: `ApplyJournal`, project lock, backups, rollback, and `recover()`.

- [ ] **Step 1: Write failing journal tests**

Assert every target records path, old hash or absent marker, backup path, staged path, new hash, and state. No mutation may occur before backups and the complete journal are fsynced.

- [ ] **Step 2: Add failure injection around every durability step**

Inject before and after:

- staged-file fsync;
- backup write/fsync;
- initial journal fsync;
- `next_target` fsync;
- replace;
- target and directory fsync;
- `replaced` fsync;
- success-marker fsync;
- applied-event fsync.

- [ ] **Step 3: Implement project locking**

Use `filelock` with a separate safe metadata record containing transaction/session ID and normalized actor. A crashed process releases the OS lock; the remaining journal forces recovery before mutation.

- [ ] **Step 4: Implement rollback rules**

In reverse order:

- current new hash: restore backup or remove newly created target;
- current old hash: leave unchanged;
- neither: stop with recovery conflict and retain every backup.

Verify old hashes and write a durable `rolled_back` marker.

- [ ] **Step 5: Implement crash recovery**

Without a valid success marker, always rollback. With a valid marker, verify all new hashes and finalize a missing applied event. Repeated recovery must be idempotent in every state.

- [ ] **Step 6: Run journal and recovery tests**

```bash
uv run pytest packages/selayer-discovery/tests/test_transaction.py -q
```

Expected: every injected failure rolls back or leaves a deterministic recoverable state.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): add recoverable file transactions"
```

### Task 20: Add explicit apply, recovery CLI, and security regression tests

**Files:**
- Modify: `packages/selayer-discovery/src/selayer_discovery/approval.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/transaction.py`
- Modify: `packages/selayer-discovery/src/selayer_discovery/cli.py`
- Modify: `packages/selayer-discovery/tests/test_approval.py`
- Modify: `packages/selayer-discovery/tests/test_transaction.py`
- Modify: `packages/selayer-discovery/tests/test_cli.py`

**Interfaces:**
- Produces: `proposal apply` and `recover` with fresh recheck and no Git behavior.

- [ ] **Step 1: Write failing apply integration tests**

Assert apply:

- accepts one current batch attestation;
- rejects target drift and snapshot drift;
- reacquires reopenable versions;
- reconstructs from typed operations, not patch text;
- reruns mandatory verification;
- requires matching canonical fingerprints;
- writes catalog, authored References, overlays, and approved summary only;
- leaves generated OKF untouched;
- stales remaining old-base groups;
- changes no unrelated dirty file.

- [ ] **Step 2: Prove no Git invocation**

Run with `PATH` containing a failing fake `git` executable and assert apply succeeds without executing it. Also scan package source for subprocess imports and shell calls.

- [ ] **Step 3: Prove evidence is inert**

Feed documents, provider content, interviews, and proposal prose containing tool, shell, approval, scope-change, secret-exfiltration, and apply instructions. Assert no state transition occurs without the corresponding typed CLI command and current attestation.

- [ ] **Step 4: Implement apply orchestration**

Perform preflight, fresh reconstruction, snapshot recheck, verification, batch-hash check, staging, journal creation, replacement, applied event, and cleanup in the documented order.

- [ ] **Step 5: Add CLI commands**

```text
proposal apply
recover
```

A pending journal makes every mutation command return a recovery-required diagnostic.

- [ ] **Step 6: Run all companion tests**

```bash
uv run pytest packages/selayer-discovery/tests -q
uv run ruff check packages/selayer-discovery
uv run pyright packages/selayer-discovery/src packages/selayer-discovery/tests
```

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add packages/selayer-discovery
git commit -m "feat(discovery): apply approved semantic changes"
```

---

## Stage 6: shopfloor replay, documentation, and completion

### Task 21: Add the deterministic shopfloor discovery replay

**Files:**
- Create: `examples/shopfloor/discovery/README.md`
- Create: `examples/shopfloor/discovery/charter.yaml`
- Create: `examples/shopfloor/discovery/replay/interview.jsonl`
- Create: `examples/shopfloor/discovery/replay/policy.yaml`
- Create: `examples/shopfloor/discovery/replay/proposal.yaml`
- Create: `examples/shopfloor/discovery/replay/wiki-query.json`
- Create: `examples/shopfloor/discovery/replay/defect.yaml`
- Create: `packages/selayer-discovery/tests/test_shopfloor_replay.py`

**Interfaces:**
- Produces: no-model end-to-end replay against the hardened shopfloor fixture.

- [ ] **Step 1: Write the failing end-to-end test**

In a temporary project:

1. copy the shopfloor catalog and apply the exact defect declared in `replay/defect.yaml`;
2. generate temporary source data;
3. build the shopfloor OKF bundle;
4. initialize the bounded session;
5. ingest four business documents and filesystem OKF evidence;
6. scan reopenable source snapshots;
7. activate the approved redacted policy;
8. replay interview answers and one correction;
9. import observed, asserted, and inferred claims;
10. leave an operation-to-telemetry relationship conflict unresolved;
11. import the independent conformed-drive correction group;
12. verify, attest, prepare, batch-attest, and apply that group;
13. validate the corrected catalog and fresh OKF bundle;
14. assert the blocked relationship group remains unapplied;
15. assert Git-visible output contains no raw session values or transcript.

- [ ] **Step 2: Run the test and confirm replay files are absent**

```bash
uv run pytest packages/selayer-discovery/tests/test_shopfloor_replay.py -q
```

Expected: failure.

- [ ] **Step 3: Write the bounded charter and replay artifacts**

The business question is limited to safe drive-level component, operation, and EOL analysis. Explicitly exclude operation-to-telemetry event joins.

`defect.yaml` must contain this exact reversible mutation:

```yaml
version: 1
target: dimension.drive_serial_number
replace:
  source: operation_executions
  column: serial_number
```

The test applies it to a temporary catalog only and asserts the resulting dimension differs from the hardened source `serialized_drives.serial_number`. No Git-history lookup is allowed.

The policy reveals no serial numbers, customer names, order IDs, or free text. Use salted hashes for conformed-identity equality and buckets for approved numeric values.

- [ ] **Step 4: Add the correction and blocked conflict**

The correction must supersede an earlier answer and stale a draft before it is regenerated. The unresolved telemetry conflict blocks only its group.

- [ ] **Step 5: Run acceptance tests**

```bash
uv run pytest \
  packages/selayer-discovery/tests/test_shopfloor_replay.py \
  tests/integration/test_shopfloor.py -q
```

Expected: all tests pass without a model or network call.

- [ ] **Step 6: Commit**

```bash
git add examples/shopfloor/discovery packages/selayer-discovery/tests/test_shopfloor_replay.py
git commit -m "test(discovery): add shopfloor semantic replay"
```

### Task 22: Document and audit the complete workflow

**Files:**
- Modify: `README.md`
- Modify: `packages/selayer-discovery/README.md`
- Modify: `.github/copilot-instructions.md`

**Interfaces:**
- Produces: installation, authority, privacy, interview, proposal, approval, apply, and recovery documentation.

- [ ] **Step 1: Document workspace installation**

```bash
uv sync
uv sync --all-packages --extra delta
uv run --package selayer-discovery selayer-discovery --help
```

Explain that installing `selayer` alone does not install discovery.

- [ ] **Step 2: Document the complete authority path**

State:

```text
Evidence and interviews -> agent draft -> typed proposal -> deterministic checks
-> named group decision -> prepared batch -> named batch attestation
-> explicit recoverable apply -> Git review
```

Catalog remains execution authority; OKF and unapproved discovery artifacts remain advisory.

- [ ] **Step 3: Document privacy and source consistency**

Explain default omit, hard-denied fields, approved transformations, caps, reopenable snapshots, blocked live/transaction evidence, and why exact profiles do not replace physical audits.

- [ ] **Step 4: Document correction, conflict, and migration behavior**

Explain append-only corrections, stale propagation, affected-group blocking, non-destructive deprecation, and no automatic request rewriting.

- [ ] **Step 5: Document apply and recovery**

Explain local attestation versus authentication, target drift, write-ahead journal, rollback rules, recovery conflict, and lack of Git operations.

- [ ] **Step 6: Run the complete verification suite**

```bash
uv lock --check
uv sync --all-packages --extra delta
uv run pytest -q
uv run ruff check src tests packages examples
uv run pyright src tests packages examples
uv build
uv build --package selayer-discovery
```

Expected: every command exits `0`.

- [ ] **Step 7: Inspect package boundaries**

```bash
python - <<'PY'
from pathlib import Path
core = "\n".join(
    path.read_text(encoding="utf-8")
    for path in Path("src/selayer").rglob("*.py")
)
for forbidden in ("selayer_discovery", "openai", "anthropic", "pydantic_ai"):
    assert forbidden not in core
PY
```

Inspect built wheels and confirm the root wheel excludes discovery while the discovery wheel contains its canonical skill.

- [ ] **Step 8: Inspect privacy and Git state**

```bash
git diff --check
git status --short
git check-ignore .selayer/discovery/sessions/example
git check-ignore .selayer/discovery/transactions/example
```

Confirm raw sessions and transactions are ignored, while approved summaries would be visible.

- [ ] **Step 9: Commit documentation**

```bash
git add README.md packages/selayer-discovery/README.md .github/copilot-instructions.md
git commit -m "docs(discovery): document semantic discovery workflow"
```

## Completion mapping

Before declaring implementation complete, map every approved requirement to evidence:

- uv workspace and independent package: Task 1 wheel and import tests.
- first-class non-destructive deprecation: Tasks 2 and 3.
- bounded public source scan seam: Tasks 4 and 5.
- no core agent dependency: Tasks 1 and 22 boundary scan.
- canonical event-sourced sessions: Tasks 6 through 8.
- ignored raw artifacts, ignored summary previews, and apply-time committed summaries: Tasks 8, 18, 20, and 22.
- normalized text intake: Task 9.
- exact aggregate profiles and reopenable evidence: Tasks 10 and 5.
- named sample-policy activation and redacted samples: Task 11.
- read-only pluggable external wiki: Task 12.
- adaptive required-gate interview: Task 13.
- append-only corrections: Task 13.
- observed/asserted/inferred claims and affected-group conflicts: Task 14.
- no LLM SDK and one canonical Agent Skill: Task 15.
- typed add/edit/deprecate and knowledge operations: Task 16.
- mandatory deterministic readiness checks: Task 17.
- atomic dependency-group decisions and exact batch attestation: Task 18.
- fsynced write-ahead apply and idempotent recovery: Tasks 19 and 20.
- no Git invocation or generated OKF edit: Task 20.
- no-model shopfloor acceptance: Task 21.
- complete documentation and project health: Task 22.

Do not mark the work complete if a mandatory verification outcome is skipped or unavailable, a live source authorizes an executable data-dependent change, an actor mismatch is accepted, a patch is executable authority, recovery is ambiguous, source values appear in committed summaries, or core selayer imports discovery.
