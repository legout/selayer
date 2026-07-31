# Selayer verification implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic static, physical, compatibility, and OKF verification with a unified CLI and fresh restricted OKF composition.

**Architecture:** A small `selayer.verification` interface normalizes evidence from the existing catalog validator, planner, and source registry. `OkfBundle.build()` composes generated definitions, authored Reference concepts, and curated overlays in a staging directory. Existing planning, adapters, generation, sync, and execution remain authoritative.

**Tech Stack:** Python 3.13, dataclasses, pathlib, argparse, PyYAML, DuckDB, PyArrow, pytest, Ruff, Pyright, uv.

## Global constraints

- Catalog schema version remains exactly integer `1`.
- Verification report schema version is exactly integer `1`.
- Runtime profile file version is exactly integer `1`.
- Physical audits perform exact full scans only. Do not add sampling.
- Normal catalog loading and QueryEngine startup do not run physical audits.
- The existing planner remains the only compatibility and safe-path authority.
- The existing source adapters remain the only source lifecycle and schema-inspection authority.
- OKF remains advisory and cannot alter planning, compilation, or execution.
- Do not add a plugin registry, business-constraint language, full expression type system, or agent behavior.
- Never place source rows, offending key values, credentials, DSNs, authenticated locations, profile values, or raw driver errors in reports, reprs, exceptions, stdout, or stderr.
- Profile files allow environment-backed strings and boolean literals only.
- `selayer-okf` must retain its current command shape, output, and exit codes.
- Use only existing runtime dependencies.
- Run commands with `uv`.
- Follow TDD for every behavior change.

---

## File structure

### New library files

- `src/selayer/verification/__init__.py`: public verification imports and `verify()` dispatch.
- `src/selayer/verification/model.py`: immutable checks, diagnostics, outcomes, reports, and catalog result.
- `src/selayer/verification/static.py`: catalog loading adapter and model-level static validation.
- `src/selayer/verification/compatibility.py`: planner-derived compatibility outcomes.
- `src/selayer/verification/audit.py`: exact source grain and relationship proof queries.
- `src/selayer/sources/profile_file.py`: version-1 runtime profile YAML parser.
- `src/selayer/okf/composition.py`: authored input validation and fresh bundle composition.
- `src/selayer/cli.py`: unified `selayer` command parser and dispatch.

### New test files

- `tests/verification/test_model.py`
- `tests/verification/test_static.py`
- `tests/verification/test_compatibility.py`
- `tests/verification/test_audit.py`
- `tests/sources/test_profile_file.py`
- `tests/okf/test_composition.py`
- `tests/test_cli.py`

### Existing files changed

- `src/selayer/catalog.py`: stable issue codes, shared model rules, and new static checks.
- `src/selayer/model.py`: no public shape change beyond validation assumptions documented in docstrings.
- `src/selayer/query.py`: execution-boundary static validation.
- `src/selayer/planning/planner.py`: pre-compilation filter-value type checks.
- `src/selayer/sources/registry.py`: private requirement-binding context for audits.
- `src/selayer/okf/model.py`: stable issue codes.
- `src/selayer/okf/validation.py`: generated-integrity, index, and fragment checks.
- `src/selayer/okf/bundle.py`: `OkfBundle.build()` and catalog-aware loading.
- `src/selayer/okf/cli.py`: reusable OKF parser and command handlers.
- `src/selayer/okf/__init__.py`: preserve exports and expose no parser internals.
- `src/selayer/__init__.py`: preserve current root API unless an approved verification type is intentionally added.
- `pyproject.toml`: add the `selayer` console script.
- `README.md`: validation, verification, profiles, compatibility, audit, and OKF build documentation.
- `.github/copilot-instructions.md`: active verification and OKF modules plus corrected authority rule.

---

## Stage 1: shared diagnostics and static verification

### Task 1: Add immutable verification report types

**Files:**
- Create: `src/selayer/verification/model.py`
- Create: `src/selayer/verification/__init__.py`
- Create: `tests/verification/test_model.py`

**Interfaces:**
- Produces: `StaticCheck`, `PhysicalCheck`, `CompatibilityCheck`, `VerificationDiagnostic`, `VerificationOutcome`, `VerificationReport`, `CatalogValidationResult`, and `VerificationCheck`.
- Consumes: existing `QueryRequest`, `RuntimeProfileResolver`, `ArrowProviderResolver`, and `SemanticLayer` types.

- [ ] **Step 1: Write failing immutability and pass-state tests**

```python
from types import MappingProxyType

import pytest

from selayer.verification.model import (
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)


def test_verification_report_freezes_nested_evidence() -> None:
    evidence = {"row_count": 3}
    outcome = VerificationOutcome(
        check_id="source.orders.grain",
        status="passed",
        scope="full_scan",
        path="data_sources.orders.grain",
        evidence=evidence,
        diagnostics=(),
    )
    evidence["row_count"] = 99
    assert outcome.evidence == MappingProxyType({"row_count": 3})
    with pytest.raises(TypeError):
        outcome.evidence["row_count"] = 4  # type: ignore[index]


def test_report_passed_requires_complete_success() -> None:
    passed = VerificationOutcome(
        "catalog.static", "passed", "declaration", "catalog", {}, ()
    )
    unavailable = VerificationOutcome(
        "source.orders", "unavailable", "full_scan", "data_sources.orders", {}, ()
    )
    assert VerificationReport(1, "shopfloor", "static", True, (passed,), ()).passed
    assert not VerificationReport(
        1, "shopfloor", "physical", False, (passed, unavailable), ()
    ).passed


def test_report_error_diagnostic_prevents_pass() -> None:
    diagnostic = VerificationDiagnostic(
        "catalog.grain.duplicate_column",
        "error",
        "data_sources.orders.grain",
        "grain columns must be unique",
    )
    outcome = VerificationOutcome(
        "catalog.static", "failed", "declaration", "catalog", {}, (diagnostic,)
    )
    report = VerificationReport(1, "shopfloor", "static", True, (outcome,), (diagnostic,))
    assert not report.passed
    assert report.to_dict()["schema_version"] == 1
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run:

```bash
uv run pytest tests/verification/test_model.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'selayer.verification'`.

- [ ] **Step 3: Implement the immutable model**

```python
# src/selayer/verification/model.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from selayer.model import SemanticLayer
from selayer.planning.types import QueryRequest
from selayer.sources.profiles import ArrowProviderResolver, RuntimeProfileResolver

Severity = Literal["error", "warning", "info"]
OutcomeStatus = Literal["passed", "failed", "skipped", "unavailable"]
EvidenceScope = Literal["declaration", "full_scan", "planner"]
CheckKind = Literal["static", "physical", "compatibility", "okf"]
EvidenceValue = bool | int | float | str | None


@dataclass(frozen=True, slots=True)
class StaticCheck:
    pass


@dataclass(frozen=True, slots=True)
class PhysicalCheck:
    profiles: RuntimeProfileResolver | None = field(default=None, repr=False)
    arrow_providers: ArrowProviderResolver | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    metrics: tuple[str, ...] | None = None
    dimensions: tuple[str, ...] | None = None
    query_cases: tuple[QueryRequest, ...] = ()

    def __post_init__(self) -> None:
        if self.metrics is not None:
            object.__setattr__(self, "metrics", tuple(self.metrics))
        if self.dimensions is not None:
            object.__setattr__(self, "dimensions", tuple(self.dimensions))
        object.__setattr__(self, "query_cases", tuple(self.query_cases))


VerificationCheck = StaticCheck | PhysicalCheck | CompatibilityCheck


@dataclass(frozen=True, slots=True, order=True)
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
    evidence: Mapping[str, EvidenceValue]
    diagnostics: tuple[VerificationDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "diagnostics", tuple(sorted(self.diagnostics)))


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: Literal[1]
    subject: str
    check_kind: CheckKind
    complete: bool
    outcomes: tuple[VerificationOutcome, ...]
    diagnostics: tuple[VerificationDiagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "outcomes",
            tuple(sorted(self.outcomes, key=lambda item: (item.path, item.check_id))),
        )
        object.__setattr__(self, "diagnostics", tuple(sorted(self.diagnostics)))

    @property
    def passed(self) -> bool:
        return (
            self.complete
            and all(outcome.status == "passed" for outcome in self.outcomes)
            and not any(item.severity == "error" for item in self.diagnostics)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "check_kind": self.check_kind,
            "complete": self.complete,
            "passed": self.passed,
            "outcomes": [
                {
                    "check_id": item.check_id,
                    "status": item.status,
                    "scope": item.scope,
                    "path": item.path,
                    "evidence": dict(item.evidence),
                    "diagnostics": [
                        {
                            "code": diagnostic.code,
                            "severity": diagnostic.severity,
                            "path": diagnostic.path,
                            "message": diagnostic.message,
                        }
                        for diagnostic in item.diagnostics
                    ],
                }
                for item in self.outcomes
            ],
            "diagnostics": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "path": item.path,
                    "message": item.message,
                }
                for item in self.diagnostics
            ],
        }


@dataclass(frozen=True, slots=True)
class CatalogValidationResult:
    layer: SemanticLayer | None
    report: VerificationReport
```

Export these names from `verification/__init__.py`; do not add them to root `selayer.__all__` yet.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/verification/test_model.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run type and lint checks for the new package**

Run:

```bash
uv run ruff check src/selayer/verification tests/verification
uv run pyright src/selayer/verification tests/verification
```

Expected: both commands exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/verification tests/verification/test_model.py
git commit -m "feat(verification): add immutable report model"
```

### Task 2: Add stable catalog codes and `validate_catalog()`

**Files:**
- Modify: `src/selayer/catalog.py:118-157`
- Create: `src/selayer/verification/static.py`
- Modify: `src/selayer/verification/__init__.py`
- Create: `tests/verification/test_static.py`
- Modify: `tests/test_catalog.py`

**Interfaces:**
- Consumes: Task 1 report types.
- Produces: `validate_catalog(path) -> CatalogValidationResult` and coded `CatalogIssue` values.

- [ ] **Step 1: Write failing compatibility and result tests**

```python
from pathlib import Path

from selayer.catalog import CatalogIssue
from selayer.verification import validate_catalog


def test_catalog_issue_keeps_old_positional_construction() -> None:
    issue = CatalogIssue("metrics.margin", "unknown measure")
    assert issue.path == "metrics.margin"
    assert issue.message == "unknown measure"
    assert issue.code == "catalog.invalid"


def test_validate_catalog_returns_layer_and_passed_report(
    valid_catalog_path: Path,
) -> None:
    result = validate_catalog(valid_catalog_path)
    assert result.layer is not None
    assert result.report.passed
    assert result.report.outcomes[0].check_id == "catalog.static"


def test_validate_catalog_returns_coded_failure(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    result = validate_catalog(path)
    assert result.layer is None
    assert not result.report.passed
    assert result.report.diagnostics[0].code == "catalog.version.unsupported"
```

- [ ] **Step 2: Run tests and verify the missing-code failure**

Run:

```bash
uv run pytest \
  tests/verification/test_static.py \
  tests/test_catalog.py::test_catalog_requires_schema_version_one -q
```

Expected: failure because `CatalogIssue` has no `code` and `validate_catalog` does not exist.

- [ ] **Step 3: Extend `CatalogIssue` and `_Collector` compatibly**

```python
# src/selayer/catalog.py
@dataclass(frozen=True, slots=True)
class CatalogIssue:
    path: str
    message: str
    code: str = "catalog.invalid"


class _Collector:
    def __init__(self) -> None:
        self.issues: list[CatalogIssue] = []

    def add(
        self,
        path: str,
        message: str,
        code: str = "catalog.invalid",
    ) -> None:
        self.issues.append(CatalogIssue(path, message, code))
```

Update `_Collector.raise_if_any()` and `CatalogValidationError` sorting so codes do not change existing message order. Give the existing version checks specific codes, including `catalog.version.unsupported`, while leaving unconverted checks on `catalog.invalid` for this task.

- [ ] **Step 4: Implement `validate_catalog()` as an adapter**

```python
# src/selayer/verification/static.py
from pathlib import Path

from selayer.catalog import CatalogValidationError
from selayer.model import SemanticLayer
from selayer.verification.model import (
    CatalogValidationResult,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)


def validate_catalog(path: str | Path) -> CatalogValidationResult:
    subject = str(Path(path))
    try:
        layer = SemanticLayer.load(path)
    except CatalogValidationError as error:
        diagnostics = tuple(
            VerificationDiagnostic(issue.code, "error", issue.path, issue.message)
            for issue in error.issues
        )
        outcome = VerificationOutcome(
            "catalog.static", "failed", "declaration", "catalog", {}, diagnostics
        )
        return CatalogValidationResult(
            None,
            VerificationReport(1, subject, "static", True, (outcome,), diagnostics),
        )
    outcome = VerificationOutcome(
        "catalog.static", "passed", "declaration", "catalog", {}, ()
    )
    return CatalogValidationResult(
        layer,
        VerificationReport(1, layer.name, "static", True, (outcome,), ()),
    )
```

Preserve sanitized handling for malformed YAML and I/O errors by mapping existing catalog-domain errors. Do not catch programmer exceptions such as `AssertionError`.

- [ ] **Step 5: Run focused and regression tests**

Run:

```bash
uv run pytest tests/verification/test_static.py tests/test_catalog.py -q
```

Expected: all tests pass, including existing exact message assertions.

- [ ] **Step 6: Commit**

```bash
git add \
  src/selayer/catalog.py \
  src/selayer/verification \
  tests/verification/test_static.py \
  tests/test_catalog.py
git commit -m "feat(catalog): add coded validation reports"
```

### Task 3: Share model rules and add static invariants

**Files:**
- Modify: `src/selayer/catalog.py:276-767,865-930`
- Modify: `src/selayer/verification/static.py`
- Modify: `src/selayer/verification/__init__.py`
- Modify: `tests/verification/test_static.py`
- Modify: `tests/test_catalog.py`
- Modify: `tests/conftest.py`
- Modify: `tests/okf/test_mlfb_scenario.py`
- Modify: `examples/e_commerce/gen_data.py`
- Modify: `examples/e_commerce/schemas/orders.yaml`
- Modify: `examples/e_commerce/schemas/order_items.yaml`
- Modify: `examples/e_commerce/schemas/customers.yaml`
- Modify: `examples/e_commerce/schemas/products.yaml`

**Interfaces:**
- Produces: `collect_model_issues(layer) -> tuple[CatalogIssue, ...]` as an internal catalog helper and `verify(layer, StaticCheck())`.
- Consumes: Task 1 checks and reports; Task 2 coded issues.

- [ ] **Step 1: Write failing tests for the four new declaration rules**

Use `dataclasses.replace()` and the current valid layer fixture to construct invalid layers without YAML parsing:

```python
from dataclasses import replace

from selayer import TableSchema
from selayer.verification import StaticCheck, verify


def test_static_check_rejects_duplicate_grain(valid_layer) -> None:  # type: ignore[no-untyped-def]
    source = valid_layer.data_sources["orders"]
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, grain=(source.grain[0], source.grain[0])),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.grain.duplicate_column" in {
        item.code for item in report.diagnostics
    }


def test_static_check_rejects_nullable_grain(valid_layer) -> None:  # type: ignore[no-untyped-def]
    source = valid_layer.data_sources["orders"]
    grain_column = source.grain[0]
    fields = tuple(
        replace(field, nullable=True) if field.name == grain_column else field
        for field in source.schema.fields
    )
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, schema=TableSchema(fields)),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.grain.nullable_column" in {
        item.code for item in report.diagnostics
    }


def test_static_check_rejects_relationship_type_mismatch(valid_layer) -> None:  # type: ignore[no-untyped-def]
    relationship = valid_layer.relationships["product_order_items"]
    bad = replace(
        valid_layer,
        relationships={
            **valid_layer.relationships,
            "product_order_items": replace(
                relationship,
                target_column="quantity",
            ),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.relationship.join_type_mismatch" in {
        item.code for item in report.diagnostics
    }


def test_static_check_rejects_sum_of_string_fact(valid_layer) -> None:  # type: ignore[no-untyped-def]
    fact = valid_layer.facts["item_revenue"]
    bad = replace(
        valid_layer,
        facts={
            **valid_layer.facts,
            "item_revenue": replace(fact, data_type="string"),
        },
    )
    report = verify(bad, StaticCheck())
    assert "catalog.measure.invalid_aggregation_type" in {
        item.code for item in report.diagnostics
    }
```

Add YAML variants to `tests/test_catalog.py` so loaded and programmatic layers produce the same code for each rule.

- [ ] **Step 2: Run tests and verify all four rules are missing**

Run:

```bash
uv run pytest \
  tests/verification/test_static.py \
  tests/test_catalog.py -q
```

Expected: the four new tests fail because the declarations currently pass.

- [ ] **Step 3: Extract model-oriented rule helpers**

In `catalog.py`, add helpers that accept typed model values rather than raw YAML:

```python
_NUMERIC_DATA_TYPES = frozenset({"integer", "decimal", "float", "double"})
_ORDERABLE_DATA_TYPES = frozenset(
    {"string", "integer", "decimal", "float", "double", "boolean", "timestamp", "date"}
)


def _validate_source_model(source: DataSource, collector: _Collector) -> None:
    path = f"data_sources.{source.name}.grain"
    if len(source.grain) != len(set(source.grain)):
        collector.add(path, "grain columns must be unique", "catalog.grain.duplicate_column")
    for column in source.grain:
        field = source.schema.field(column)
        if field.nullable:
            collector.add(
                path,
                f"grain column {column!r} must be non-nullable",
                "catalog.grain.nullable_column",
            )


def _validate_measure_model(
    measure: Measure,
    facts: Mapping[str, Fact],
    collector: _Collector,
) -> None:
    fact = facts.get(measure.fact)
    if fact is None:
        return
    if measure.aggregation in {"sum", "avg"} and fact.data_type not in _NUMERIC_DATA_TYPES:
        collector.add(
            f"measures.{measure.name}.aggregation",
            "sum and avg require a numeric fact",
            "catalog.measure.invalid_aggregation_type",
        )
    if measure.aggregation in {"min", "max"} and fact.data_type not in _ORDERABLE_DATA_TYPES:
        collector.add(
            f"measures.{measure.name}.aggregation",
            "min and max require an orderable fact",
            "catalog.measure.invalid_aggregation_type",
        )
```

Add relationship type comparison using `_logical_type_kind()` and the existing safe-equivalence table. Do not add coercion.

Refactor safe graph, fact reachability, and metric grain helpers so both raw catalog construction and `SemanticLayer` validation call the same typed helper implementations. YAML-only shape checks stay in the raw loader.

- [ ] **Step 4: Implement `collect_model_issues()` and static dispatch**

```python
# src/selayer/catalog.py
def collect_model_issues(layer: SemanticLayer) -> tuple[CatalogIssue, ...]:
    collector = _Collector()
    _validate_layer_identity_model(layer, collector)
    _validate_named_models("data_sources", layer.data_sources, DataSource, collector)
    _validate_named_models("dimensions", layer.dimensions, Dimension, collector)
    _validate_named_models("facts", layer.facts, Fact, collector)
    _validate_named_models("measures", layer.measures, Measure, collector)
    _validate_named_models("metrics", layer.metrics, Metric, collector)
    _validate_named_models("relationships", layer.relationships, Relationship, collector)
    for source in layer.data_sources.values():
        _validate_source_model(source, collector)
    for dimension in layer.dimensions.values():
        _validate_dimension_model(dimension, layer.data_sources, collector)
    for fact in layer.facts.values():
        _validate_fact_model(fact, layer, collector)
    for measure in layer.measures.values():
        _validate_measure_model(measure, layer.facts, collector)
    for metric in layer.metrics.values():
        _validate_metric_model(metric, layer, collector)
    for relationship in layer.relationships.values():
        _validate_relationship_model(relationship, layer.data_sources, collector)
    _validate_fact_reachability_model(layer, collector)
    _validate_metric_grains_model(layer, collector)
    return tuple(sorted(collector.issues, key=lambda issue: (issue.path, issue.message)))
```

Define every helper named above in `catalog.py` during this task. Each helper accepts typed model mappings and uses the existing expression-reference, schema-field, safe-graph, and grain functions rather than parsing YAML again.

```python
# src/selayer/verification/static.py
def verify_static(layer: SemanticLayer) -> VerificationReport:
    issues = collect_model_issues(layer)
    diagnostics = tuple(
        VerificationDiagnostic(issue.code, "error", issue.path, issue.message)
        for issue in issues
    )
    status = "passed" if not diagnostics else "failed"
    outcome = VerificationOutcome(
        "catalog.static", status, "declaration", "catalog", {}, diagnostics
    )
    return VerificationReport(1, layer.name, "static", True, (outcome,), diagnostics)
```

In `verification/__init__.py`, implement exact-type dispatch for `StaticCheck` and raise `TypeError("unsupported verification check")` for unknown objects. Physical and compatibility branches are added in later tasks.

- [ ] **Step 5: Migrate existing valid grain declarations to non-nullable fields**

Change only declared grain fields to non-nullable:

- `orders.id`, `order_items.order_id`, `order_items.product_id`, `customers.id`, and `products.id` in `examples/e_commerce/schemas/`;
- the matching Arrow fields in `examples/e_commerce/gen_data.py` by passing `nullable=False`;
- the matching inline fields in `tests/conftest.py`;
- valid inline catalog fixtures in `tests/test_catalog.py` and `tests/okf/test_mlfb_scenario.py`.

Keep intentionally invalid nullable-grain fixtures unchanged. Regenerate e-commerce data in a temporary directory and assert the observed schemas satisfy the stricter declarations.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest \
  tests/verification/test_static.py \
  tests/test_catalog.py \
  tests/okf/test_mlfb_scenario.py \
  tests/integration/test_ecommerce.py -q
```

Expected: all tests pass and existing catalog messages remain stable except for tests intentionally updated to the new grain rule.

- [ ] **Step 7: Run all catalog, model, expression, and planner tests**

Run:

```bash
uv run pytest \
  tests/test_catalog.py \
  tests/test_model.py \
  tests/expressions \
  tests/planning -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  src/selayer/catalog.py \
  src/selayer/verification \
  tests/verification/test_static.py \
  tests/test_catalog.py \
  tests/conftest.py \
  tests/okf/test_mlfb_scenario.py \
  examples/e_commerce/gen_data.py \
  examples/e_commerce/schemas/orders.yaml \
  examples/e_commerce/schemas/order_items.yaml \
  examples/e_commerce/schemas/customers.yaml \
  examples/e_commerce/schemas/products.yaml
git commit -m "feat(catalog): verify model invariants"
```

### Task 4: Validate QueryEngine layers and filter values

**Files:**
- Modify: `src/selayer/query.py:53-80`
- Modify: `src/selayer/planning/planner.py:133-297`
- Modify: `tests/test_query.py`
- Modify: `tests/planning/test_planner.py`

**Interfaces:**
- Consumes: `verify(layer, StaticCheck())`.
- Produces: execution-boundary model validation and stable `invalid_filter_type` planning errors.

- [ ] **Step 1: Write a failing no-resource-open test**

```python
from dataclasses import replace
from unittest.mock import patch

import pytest

from selayer import CatalogValidationError, QueryEngine


def test_query_engine_rejects_invalid_direct_layer_before_duckdb(valid_layer) -> None:  # type: ignore[no-untyped-def]
    source = valid_layer.data_sources["orders"]
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, grain=("id", "id")),
        },
    )
    with patch("selayer.query.duckdb.connect") as connect:
        with pytest.raises(CatalogValidationError) as raised:
            QueryEngine(bad)
    connect.assert_not_called()
    assert raised.value.issues[0].code == "catalog.grain.duplicate_column"
```

- [ ] **Step 2: Write failing filter-type planner tests**

```python
@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("product_category", 42),
        ("order_date", "2026-01-01"),
    ],
)
def test_planner_rejects_filter_value_type(valid_catalog_path, dimension, value) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    with pytest.raises(QueryPlanningError) as raised:
        plan_query(layer, QueryRequest(["gross_revenue"], filters={dimension: value}))
    assert raised.value.code == "invalid_filter_type"
    assert repr(value) not in str(raised.value)
```

Use dimensions that exist in the fixture and concrete incorrect values. Add scalar, list, and range cases for every supported semantic data type.

- [ ] **Step 3: Run tests and confirm the boundary gaps**

Run:

```bash
uv run pytest \
  tests/test_query.py::test_query_engine_rejects_invalid_direct_layer_before_duckdb \
  tests/planning/test_planner.py -q
```

Expected: the direct layer reaches DuckDB and filter mismatches do not produce `invalid_filter_type`.

- [ ] **Step 4: Validate the layer before creating DuckDB**

```python
# src/selayer/query.py
from selayer.catalog import CatalogIssue, CatalogValidationError
from selayer.verification import StaticCheck, verify

# At the start of QueryEngine.__init__:
report = verify(semantic_layer, StaticCheck())
if not report.passed:
    raise CatalogValidationError(
        tuple(
            CatalogIssue(item.path, item.message, item.code)
            for item in report.diagnostics
            if item.severity == "error"
        )
    )
self.semantic_layer = semantic_layer
self._connection = duckdb.connect(":memory:")
```

Do not catch `CatalogValidationError` in `QueryEngine`.

- [ ] **Step 5: Add secret-safe filter type checks in the planner**

Implement a private predicate based on exact runtime types:

```python
from datetime import date, datetime
from decimal import Decimal


def _matches_data_type(data_type: str, value: object) -> bool:
    if data_type == "string":
        return type(value) is str
    if data_type == "integer":
        return type(value) is int
    if data_type in {"decimal", "float", "double"}:
        return type(value) in {int, float, Decimal}
    if data_type == "boolean":
        return type(value) is bool
    if data_type == "date":
        return type(value) is date
    if data_type == "timestamp":
        return type(value) is datetime
    return False
```

Validate each scalar, list member, and non-`None` range bound after resolving the dimension. Raise:

```python
raise QueryPlanningError(
    "invalid_filter_type",
    f"filter for dimension {dimension_name!r} requires {dimension.data_type}",
)
```

Never format the value or its type repr.

- [ ] **Step 6: Run query and planner tests**

Run:

```bash
uv run pytest tests/test_query.py tests/planning/test_planner.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/selayer/query.py \
  src/selayer/planning/planner.py \
  tests/test_query.py \
  tests/planning/test_planner.py
git commit -m "feat(query): validate layer and filter types"
```

### Task 5: Add the unified CLI and catalog validation command

**Files:**
- Create: `src/selayer/cli.py`
- Modify: `pyproject.toml:15-17`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `validate_catalog()` and `VerificationReport.to_dict()`.
- Produces: `selayer catalog validate CATALOG` and `selayer.cli.main(argv) -> int`.

- [ ] **Step 1: Write failing parser and output tests**

```python
import json
from pathlib import Path

from selayer.cli import main


def test_project_registers_unified_console_script(root: Path) -> None:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'selayer = "selayer.cli:run"' in pyproject


def test_catalog_validate_emits_report(
    valid_catalog_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    assert main(["catalog", "validate", str(valid_catalog_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["passed"] is True


def test_invalid_catalog_exits_one(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    assert main(["catalog", "validate", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
```

- [ ] **Step 2: Run tests and confirm the CLI is missing**

Run:

```bash
uv run pytest tests/test_cli.py -q
```

Expected: collection fails because `selayer.cli` does not exist.

- [ ] **Step 3: Implement the Stage 1 parser and handler**

```python
# src/selayer/cli.py
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from selayer.verification import validate_catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selayer")
    commands = parser.add_subparsers(dest="area", required=True)
    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="command", required=True)
    validate = catalog_commands.add_parser("validate")
    validate.add_argument("catalog")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.area == "catalog" and args.command == "validate":
        result = validate_catalog(args.catalog)
        print(json.dumps(result.report.to_dict(), sort_keys=True))
        return 0 if result.report.passed else 1
    raise AssertionError("unhandled command")


def run() -> None:
    raise SystemExit(main())
```

Catch only expected I/O and domain errors and emit a secret-safe JSON-shaped failure. Do not catch `AssertionError` or `TypeError` caused by programming mistakes.

- [ ] **Step 4: Register the command**

```toml
[project.scripts]
selayer = "selayer.cli:run"
selayer-okf = "selayer.okf.cli:run"
```

- [ ] **Step 5: Run CLI and packaging tests**

Run:

```bash
uv run pytest tests/test_cli.py tests/okf/test_cli.py -q
uv run selayer catalog validate ecommerce_semantic_layer.yaml
```

Expected: tests pass; the command prints one JSON object with `"passed": true` and exits `0`.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/cli.py pyproject.toml tests/test_cli.py
git commit -m "feat(cli): add catalog validation command"
```

## Stage 2: OKF integrity and fresh composition

### Task 6: Add coded OKF generated-integrity validation

**Files:**
- Modify: `src/selayer/okf/model.py`
- Modify: `src/selayer/okf/validation.py`
- Modify: `src/selayer/okf/bundle.py:566-607`
- Modify: `tests/okf/test_validation.py`
- Modify: `tests/okf/test_document.py`

**Interfaces:**
- Produces: coded `OkfIssue` values and catalog-aware generated-integrity checks invoked by `OkfBundle.load(path, layer=layer)`.
- Consumes: `concepts_from_layer()`, `generated_fingerprint()`, existing parsers, and existing index validation.

- [ ] **Step 1: Write failing integrity tests**

Add tests that generate a bundle, then modify one condition at a time:

```python
def test_catalog_aware_load_rejects_stale_valid_looking_fingerprint(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "metrics" / "gross_margin.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Expression: `gross_margin`", "Expression: `wrong`"), encoding="utf-8")
    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)
    assert "okf.generated.fingerprint_mismatch" in {
        issue.code for issue in raised.value.issues
    }
```

Also add concrete tests for:

- missing generated concept;
- orphan generated `selayer_id`;
- wrong semantic kind directory;
- generated index missing one member;
- generated index with wrong displayed title;
- a link whose file exists but fragment heading does not.

- [ ] **Step 2: Run tests and observe false acceptance**

Run:

```bash
uv run pytest tests/okf/test_validation.py -q
```

Expected: the new tests fail because current validation checks fingerprint shape and file targets only.

- [ ] **Step 3: Add an optional stable code to `OkfIssue`**

Preserve the current positional constructor:

```python
@dataclass(frozen=True, slots=True)
class OkfIssue:
    path: str
    message: str
    severity: Severity = "error"
    code: str = "okf.invalid"
```

Update `_issue()` and `_optional_issue()` to accept a keyword-only `code` default. Preserve current path, message, and severity sorting.

- [ ] **Step 4: Implement catalog projection comparison**

Add a bundle-level function in `okf/validation.py`:

```python
def validate_generated_integrity(
    root: Path,
    concepts: Mapping[str, OkfConcept],
    layer: SemanticLayer,
) -> tuple[OkfIssue, ...]:
    expected = concepts_from_layer(layer)
    issues: list[OkfIssue] = []
    # Compare expected IDs, paths, kinds, unique Catalog Definition sections,
    # controlled fingerprints, generated definitions, and generated indexes.
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
```

Use the current authoritative fingerprint location. If the separate generation-interoperability migration has landed, use its helper instead of branching independently.

Recompute every current generated document's fingerprint from controlled frontmatter and the `Catalog Definition` body. Compare catalog definition content to the fresh in-memory generated concept.

- [ ] **Step 5: Add fragment validation**

Parse the URL-decoded fragment from internal Markdown links, compute the same GitHub-style lowercase hyphenated heading slug for loaded sections, and emit `okf.link.missing_fragment` when the file exists but the heading does not.

Keep external URLs out of scope. Do not fetch them.

- [ ] **Step 6: Integrate with catalog-aware bundle loading**

In `OkfBundle.load()`, run generated integrity only when `layer` is not `None`. Generic layer-free loading retains current behavior. Add issues before strict-mode error selection so lenient loading can expose them as diagnostics where appropriate.

- [ ] **Step 7: Run OKF validation and compatibility tests**

Run:

```bash
uv run pytest \
  tests/okf/test_validation.py \
  tests/okf/test_document.py \
  tests/okf/test_compatibility.py \
  tests/okf/test_generation.py \
  tests/okf/test_sync.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  src/selayer/okf/model.py \
  src/selayer/okf/validation.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_validation.py \
  tests/okf/test_document.py
git commit -m "feat(okf): verify generated catalog integrity"
```

### Task 7: Parse and validate authored References and overlays

**Files:**
- Create: `src/selayer/okf/composition.py`
- Create: `tests/okf/test_composition.py`

**Interfaces:**
- Produces: private `load_references(root)` and `load_overlays(root, layer)` helpers plus immutable `OkfOverlay` values.
- Consumes: `parse_concept()`, `split_sections()`, `validate_concept()`, and `SemanticLayer.resolve()`.

- [ ] **Step 1: Write failing valid-input tests**

```python
def test_loads_valid_reference_and_overlay(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references = tmp_path / "references"
    references.mkdir()
    (references / "guide.md").write_text(
        "---\ntype: Reference\ntitle: Guide\nstatus: stable\n---\n\n# Guidance\nText.\n",
        encoding="utf-8",
    )
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    (overlays / "gross_margin.md").write_text(
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Usage Guidance\nUse at item grain.\n\n"
        "# Caveats\nDo not mix grains.\n",
        encoding="utf-8",
    )
    loaded_references = load_references(references)
    loaded_overlays = load_overlays(tmp_path / "overlays", valid_layer)
    assert tuple(loaded_references) == ("references/guide.md",)
    assert loaded_overlays[0].selayer_id == "metric.gross_margin"
```

- [ ] **Step 2: Add parameterized rejection tests**

Cover exact cases from the spec:

- reference has `selayer_id`;
- reserved `index.md` or `log.md` path;
- symbolic link or path escape;
- invalid UTF-8 or special file;
- overlay missing or unknown ID;
- overlay path and ID mismatch;
- duplicate IDs or headings;
- unknown frontmatter;
- `verified`, title, description, generated metadata, or `Catalog Definition`;
- preamble text;
- self-link, duplicate Related Concepts link, or broken link;
- more than 1,000 files;
- a file over 1,048,576 bytes;
- total input over 16,777,216 bytes;
- more than 1,000 links in one file.

Use small monkeypatched constants for count and size boundary tests rather than creating 16 MiB fixtures.

- [ ] **Step 3: Run tests and confirm the module is missing**

Run:

```bash
uv run pytest tests/okf/test_composition.py -q
```

Expected: collection fails because `selayer.okf.composition` does not exist.

- [ ] **Step 4: Implement immutable overlay parsing**

```python
_ALLOWED_OVERLAY_FIELDS = frozenset({"selayer_id", "sources", "stale_after"})
_ALLOWED_OVERLAY_SECTIONS = (
    "Usage Guidance",
    "Examples",
    "Caveats",
    "Related Concepts",
)
_MAX_FILES = 1_000
_MAX_FILE_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 16_777_216
_MAX_LINKS_PER_FILE = 1_000


@dataclass(frozen=True, slots=True)
class OkfOverlay:
    relative_path: Path
    selayer_id: str
    frontmatter: Mapping[str, object]
    sections: tuple[OkfSection, ...]
```

Use safe YAML composition with duplicate-key detection before `safe_load`. Reject any top-level content outside frontmatter and allowed `#` sections. Validate `sources` and `stale_after` by constructing a temporary concept and reusing existing OKF field validators.

- [ ] **Step 5: Implement bounded input walking**

Use `Path.rglob("*.md")`, `lstat()`, lexical containment, and exact regular-file checks. Reject symbolic links before reading. Accumulate file count and byte totals before parsing. Sort paths by POSIX relative path.

References are parsed as ordinary `OkfConcept` objects and validated strictly. Require non-empty `type` and `title`, and reject `selayer_id`.

- [ ] **Step 6: Validate paths and links**

For each overlay, derive the expected generated path from `selayer_id` using `concept_path()`. Require exact relative-path equality. Resolve internal links against the future composed bundle root, reject self-links and duplicates in Related Concepts, and defer cross-input existence validation to Task 8 when generated and Reference concept sets are known together.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run pytest tests/okf/test_composition.py -q
uv run ruff check src/selayer/okf/composition.py tests/okf/test_composition.py
uv run pyright src/selayer/okf/composition.py tests/okf/test_composition.py
```

Expected: all commands pass.

- [ ] **Step 8: Commit**

```bash
git add src/selayer/okf/composition.py tests/okf/test_composition.py
git commit -m "feat(okf): validate authored bundle inputs"
```

### Task 8: Build fresh composed OKF bundles atomically

**Files:**
- Modify: `src/selayer/okf/composition.py`
- Modify: `src/selayer/okf/bundle.py:270-332`
- Modify: `tests/okf/test_composition.py`
- Modify: `tests/okf/test_generation.py`

**Interfaces:**
- Produces: `OkfBundle.build(layer, output_dir, references_dir=None, overlays_dir=None, include_descriptive=False)`.
- Consumes: Task 6 integrity validation and Task 7 input loaders.

- [ ] **Step 1: Write a failing successful-build test**

```python
def test_build_composes_reference_and_overlay(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references, overlays = authored_inputs(tmp_path)
    output = tmp_path / "knowledge"
    bundle = OkfBundle.build(
        valid_layer,
        output,
        references_dir=references,
        overlays_dir=overlays,
    )
    metric = bundle.concepts["metrics/gross_margin.md"]
    assert metric.section("Usage Guidance").content == "Use at item grain."
    assert "references/guide.md" in bundle.concepts
    assert OkfBundle.load(output, layer=valid_layer).diagnostics == ()
```

Use a concrete test helper that writes the valid files shown in Task 7.

- [ ] **Step 2: Write failing atomicity tests**

Cover:

- populated destination rejected before staging;
- existing empty destination accepted;
- overlay failure leaves destination absent or empty;
- strict final-load failure removes staging;
- simulated `Path.replace()` failure leaves destination unchanged;
- no sibling temporary directory remains after success or failure.

- [ ] **Step 3: Run tests and confirm `build()` is missing**

Run:

```bash
uv run pytest tests/okf/test_composition.py tests/okf/test_generation.py -q
```

Expected: new tests fail because `OkfBundle.build` does not exist.

- [ ] **Step 4: Implement fresh composition**

```python
# src/selayer/okf/composition.py
def build_bundle(
    layer: SemanticLayer,
    output_dir: str | Path,
    *,
    references_dir: str | Path | None,
    overlays_dir: str | Path | None,
    include_descriptive: bool,
) -> OkfBundle:
    destination = Path(output_dir)
    _preflight_empty_destination(destination)
    references = load_references(Path(references_dir)) if references_dir else {}
    overlays = load_overlays(Path(overlays_dir), layer) if overlays_dir else ()
    staging = _sibling_staging_path(destination)
    published = False
    try:
        generated = OkfBundle.from_layer(layer, include_descriptive=include_descriptive)
        generated.write(staging)
        _write_references(staging, references)
        _apply_overlays(staging, overlays)
        _validate_composed_links(staging, generated, references, overlays)
        OkfBundle.load(staging, layer=layer, strict=True)
        _publish_staging(staging, destination)
        published = True
        return OkfBundle.load(destination, layer=layer, strict=True)
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
```

The `finally` block cleans staging for domain errors, I/O errors, and process interrupts without suppressing or wrapping the original exception.

- [ ] **Step 5: Merge overlays without touching generated ownership**

For a fresh build, parse the generated concept, merge only allowed frontmatter and allowed curated sections, construct a new immutable `OkfConcept`, and render it. Preserve the generated `Catalog Definition` and controlled frontmatter byte-for-byte where possible. Recompute no generated fingerprint because curated fields are outside its controlled input.

Validate all combined links after References and overlays exist.

- [ ] **Step 6: Add `OkfBundle.build()`**

```python
@classmethod
def build(
    cls,
    layer: SemanticLayer,
    output_dir: str | Path,
    *,
    references_dir: str | Path | None = None,
    overlays_dir: str | Path | None = None,
    include_descriptive: bool = False,
) -> OkfBundle:
    from .composition import build_bundle

    return build_bundle(
        layer,
        output_dir,
        references_dir=references_dir,
        overlays_dir=overlays_dir,
        include_descriptive=include_descriptive,
    )
```

- [ ] **Step 7: Run all OKF tests**

Run:

```bash
uv run pytest tests/okf -q
```

Expected: all tests pass, including existing write and sync byte-preservation tests.

- [ ] **Step 8: Commit**

```bash
git add \
  src/selayer/okf/composition.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_composition.py \
  tests/okf/test_generation.py
git commit -m "feat(okf): build composed bundles atomically"
```

### Task 9: Route OKF commands through the unified CLI

**Files:**
- Modify: `src/selayer/cli.py`
- Modify: `src/selayer/okf/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/okf/test_cli.py`

**Interfaces:**
- Produces: `selayer okf build|generate|sync|validate|retrieve` while preserving `selayer-okf`.
- Consumes: `OkfBundle.build()` and existing OKF command handlers.

- [ ] **Step 1: Write failing parity and build-command tests**

```python
def test_unified_and_legacy_okf_validate_match(
    generated_bundle: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    assert unified_main(["okf", "validate", str(generated_bundle)]) == 0
    unified = capsys.readouterr()
    assert legacy_main(["validate", str(generated_bundle)]) == 0
    legacy = capsys.readouterr()
    assert unified.out == legacy.out
    assert unified.err == legacy.err


def test_okf_build_accepts_reference_and_overlay_directories(
    valid_catalog_path: Path, authored_inputs, tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    references, overlays = authored_inputs
    output = tmp_path / "knowledge"
    assert unified_main(
        [
            "okf", "build", str(valid_catalog_path), str(output),
            "--references", str(references),
            "--overlays", str(overlays),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "build"
```

- [ ] **Step 2: Run tests and confirm missing unified OKF commands**

Run:

```bash
uv run pytest tests/test_cli.py tests/okf/test_cli.py -q
```

Expected: new unified command tests fail.

- [ ] **Step 3: Refactor reusable OKF parser and handlers**

In `okf/cli.py`, extract one command-population helper and expose private adapters for the unified CLI:

```python
def _add_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    include_build: bool,
) -> None:
    generate = commands.add_parser("generate", help="create a new bundle")
    generate.add_argument("catalog", type=Path)
    generate.add_argument("destination", type=Path)

    if include_build:
        build = commands.add_parser("build", help="compose a fresh bundle")
        build.add_argument("catalog", type=Path)
        build.add_argument("destination", type=Path)
        build.add_argument("--references", type=Path)
        build.add_argument("--overlays", type=Path)

    sync = commands.add_parser("sync", help="sync catalog definitions into a bundle")
    sync.add_argument("catalog", type=Path)
    sync.add_argument("bundle", type=Path)
    sync.add_argument("--dry-run", action="store_true")

    validate = commands.add_parser("validate", help="validate an existing bundle")
    validate.add_argument("bundle", type=Path)
    validate.add_argument("--catalog", type=Path)

    retrieve = commands.add_parser("retrieve", help="retrieve attributed context")
    retrieve.add_argument("bundle", type=Path)
    retrieve.add_argument("semantic_ids", nargs="+")
    retrieve.add_argument("--catalog", type=Path)
    retrieve.add_argument("--no-linked", action="store_true")
    retrieve.add_argument("--max-chars", type=int, default=12_000)
    retrieve.add_argument("--max-depth", type=int, default=1)


def add_okf_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    okf = subparsers.add_parser("okf")
    _add_commands(okf.add_subparsers(dest="command", required=True), include_build=True)


def execute_okf(arguments: argparse.Namespace) -> int:
    return _execute(arguments)
```

Keep `okf.cli._parser()` building the legacy root parser with `_add_commands(commands, include_build=False)` so its existing command set and help remain unchanged. `okf.cli.main(argv)` continues calling `_execute()`.

- [ ] **Step 4: Add the unified `okf` area and build handler**

Add `okf` to `selayer.cli._parser()` and call `add_okf_commands()`. Add `build` to the unified parser only. The legacy `selayer-okf` parser keeps exactly its current four commands. The unified build handler loads the catalog, calls `OkfBundle.build()`, and prints deterministic concept and diagnostic counts.

- [ ] **Step 5: Run parity tests and direct commands**

Run:

```bash
uv run pytest tests/test_cli.py tests/okf/test_cli.py -q
TMP_ROOT=$(mktemp -d)
uv run selayer okf generate \
  examples/shopfloor/shopfloor_semantic_layer.yaml \
  "$TMP_ROOT/knowledge"
uv run selayer okf validate "$TMP_ROOT/knowledge" \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer-okf validate "$TMP_ROOT/knowledge" \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
rm -rf "$TMP_ROOT"
```

Expected: tests pass; generation succeeds in a fresh temporary destination, and the two validation commands produce equivalent JSON and exit codes.

- [ ] **Step 6: Commit**

```bash
git add \
  src/selayer/cli.py \
  src/selayer/okf/cli.py \
  tests/test_cli.py \
  tests/okf/test_cli.py
git commit -m "feat(cli): unify OKF commands"
```

## Stage 3: planner-derived compatibility reporting

### Task 10: Add compatibility verification and CLI output

**Files:**
- Create: `src/selayer/verification/compatibility.py`
- Modify: `src/selayer/verification/__init__.py`
- Modify: `src/selayer/cli.py`
- Create: `tests/verification/test_compatibility.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `verify(layer, CompatibilityCheck())` and `selayer catalog compatibility CATALOG`.
- Consumes: `QueryRequest`, `plan_query()`, and stable `QueryPlanningError.code` values.

- [ ] **Step 1: Write failing planner-parity tests**

```python
def test_compatibility_matches_direct_planner(valid_layer: SemanticLayer) -> None:
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category",),
        ),
    )
    outcome = next(
        item for item in report.outcomes
        if item.check_id == "compatibility.metric_dimension.gross_margin.product_category"
    )
    plan = plan_query(
        valid_layer,
        QueryRequest(["gross_margin"], ["product_category"]),
    )
    assert outcome.status == "passed"
    assert outcome.evidence["anchor_source"] == plan.anchor_source
    assert outcome.evidence["required_sources"] == ",".join(plan.required_sources)


def test_compatibility_preserves_planner_failure_code(root: Path) -> None:
    layer = SemanticLayer.load(
        root / "examples" / "shopfloor" / "shopfloor_semantic_layer.yaml"
    )
    report = verify(
        layer,
        CompatibilityCheck(
            metrics=("average_cycle_seconds", "eol_attempt_pass_rate")
        ),
    )
    incompatible = [
        item for item in report.outcomes
        if item.evidence.get("compatible") is False
    ]
    assert incompatible[0].status == "passed"
    assert incompatible[0].evidence["planner_code"] == "mixed_grain"
    assert report.passed
```

Use metric names from existing fixtures. Add tests for unknown requested selectors, deterministic ordering, metric-alone, metric-dimension, unordered metric-pair, and explicit multi-dimension `QueryRequest` cases.

- [ ] **Step 2: Run tests and confirm dispatch is unsupported**

Run:

```bash
uv run pytest tests/verification/test_compatibility.py -q
```

Expected: `verify()` raises `TypeError("unsupported verification check")`.

- [ ] **Step 3: Implement deterministic request generation**

```python
def compatibility_requests(
    layer: SemanticLayer,
    check: CompatibilityCheck,
) -> tuple[tuple[str, QueryRequest], ...]:
    metrics = tuple(sorted(check.metrics or layer.metrics))
    dimensions = tuple(sorted(check.dimensions or layer.dimensions))
    requests: list[tuple[str, QueryRequest]] = []
    for metric in metrics:
        requests.append((f"compatibility.metric.{metric}", QueryRequest([metric])))
        for dimension in dimensions:
            requests.append((
                f"compatibility.metric_dimension.{metric}.{dimension}",
                QueryRequest([metric], [dimension]),
            ))
    for index, left in enumerate(metrics):
        for right in metrics[index + 1:]:
            requests.append((
                f"compatibility.metric_pair.{left}.{right}",
                QueryRequest([left, right]),
            ))
    for index, request in enumerate(check.query_cases):
        requests.append((f"compatibility.explicit.{index:04d}", request))
    return tuple(requests)
```

Validate selected names before generating requests. An unknown selector produces a coded failed declaration outcome rather than silently omitting it.

- [ ] **Step 4: Adapt planner results without SQL compilation**

For each request, call `plan_query()`. A compatible request records `compatible: true`, the anchor source, comma-joined required sources, comma-joined relationship IDs, and selected dimensions. A documented `QueryPlanningError` records `compatible: false`, its stable code, and a safe message. Both outcomes use `status: passed` because the verification check completed. Reserve `failed` for invalid selectors or unexpected verifier errors. Do not initialize `QueryEngine`, adapters, or DuckDB.

Add exact-type dispatch in `verification.__init__.verify()` for `CompatibilityCheck`.

- [ ] **Step 5: Add the CLI command**

Add:

```text
selayer catalog compatibility CATALOG
```

Optional repeated `--metric`, `--dimension`, and `--query-cases JSON_FILE` flags map to `CompatibilityCheck`. The JSON file contains a list of objects accepted by `QueryRequest`; reject unknown object keys. Do not accept SQL or expression text.

- [ ] **Step 6: Run focused tests and a shopfloor report**

Run:

```bash
uv run pytest tests/verification/test_compatibility.py tests/test_cli.py -q
uv run selayer catalog compatibility examples/shopfloor/shopfloor_semantic_layer.yaml
```

Expected: tests pass; output includes successful metric-dimension outcomes and stable mixed-grain failures.

- [ ] **Step 7: Commit**

```bash
git add \
  src/selayer/verification \
  src/selayer/cli.py \
  tests/verification/test_compatibility.py \
  tests/test_cli.py
git commit -m "feat(verification): report planner compatibility"
```

## Stage 4: runtime profiles and exact physical audits

### Task 11: Add the runtime profile file parser

**Files:**
- Create: `src/selayer/sources/profile_file.py`
- Create: `tests/sources/test_profile_file.py`

**Interfaces:**
- Produces: `load_profile_file(path, environ=os.environ) -> MappingProfileResolver`.
- Consumes: existing `MappingProfileResolver` and secret-safe source error conventions.

- [ ] **Step 1: Write failing valid-profile tests**

```python
def test_profile_file_resolves_environment_and_boolean(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(
        "version: 1\nprofiles:\n  warehouse:\n"
        "    dsn:\n      env: WAREHOUSE_DSN\n"
        "    allow_extension_install:\n      literal: false\n",
        encoding="utf-8",
    )
    resolver = load_profile_file(path, environ={"WAREHOUSE_DSN": "secret-dsn"})
    profile = resolver.resolve("warehouse", source_id="orders")
    assert profile.value("dsn") == "secret-dsn"
    assert profile.value("allow_extension_install") is False
    assert "secret-dsn" not in repr(profile)
```

- [ ] **Step 2: Add table-driven invalid-input and leak tests**

Cover duplicate YAML keys, wrong version, unknown top-level keys, invalid profile names, invalid environment names, both `env` and `literal`, neither source, missing environment variable, string/numeric/null/list/mapping literals, and hostile string subclasses supplied through `environ`.

For a sentinel secret, assert absence from:

- `repr(error)`;
- `str(error)`;
- `error.args`;
- formatted traceback;
- `__cause__` and `__context__`;
- captured stdout and stderr.

- [ ] **Step 3: Run tests and confirm the parser is missing**

Run:

```bash
uv run pytest tests/sources/test_profile_file.py -q
```

Expected: collection fails because `profile_file.py` does not exist.

- [ ] **Step 4: Implement safe YAML composition and resolution**

```python
def load_profile_file(
    path: str | Path,
    *,
    environ: Mapping[str, str] = os.environ,
) -> MappingProfileResolver:
    document = _compose_without_duplicate_keys(Path(path))
    _validate_document_shape(document)
    profiles: dict[str, dict[str, object]] = {}
    for profile_name, entries in document["profiles"].items():
        values: dict[str, object] = {}
        for key, source in entries.items():
            if "env" in source:
                env_name = source["env"]
                if env_name not in environ:
                    raise SourceProfileError(
                        "profile_environment_missing",
                        "a required profile environment variable is missing",
                    )
                value = environ[env_name]
                if type(value) is not str:
                    raise SourceProfileError(
                        "profile_environment_invalid",
                        "a profile environment value is invalid",
                    )
                values[key] = value
            else:
                values[key] = source["literal"]
        profiles[profile_name] = values
    return MappingProfileResolver(profiles)
```

Define `ProfileFileValidationError` in `profile_file.py` with immutable `code`, `path`, and a message selected from a fixed code-to-message mapping. Raise it as:

```python
raise ProfileFileValidationError(
    "source.profile.missing_environment",
    f"profiles.{profile_name}.{key}.env",
)
```

Do not store the missing environment value or any resolved profile value. Keep `SourceProfileError` unchanged for runtime resolver failures.

- [ ] **Step 5: Run profile tests**

Run:

```bash
uv run pytest tests/sources/test_profile_file.py tests/sources/test_profiles.py -q
uv run ruff check src/selayer/sources/profile_file.py tests/sources/test_profile_file.py
uv run pyright src/selayer/sources/profile_file.py tests/sources/test_profile_file.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/sources/profile_file.py tests/sources/test_profile_file.py
git commit -m "feat(sources): load runtime profiles from environment"
```

### Task 12: Add audit bindings and exact source-grain checks

**Files:**
- Modify: `src/selayer/sources/registry.py:659-758`
- Create: `src/selayer/verification/audit.py`
- Modify: `src/selayer/verification/__init__.py`
- Create: `tests/verification/test_audit.py`
- Modify: `tests/sources/test_registry.py`
- Modify: `tests/sources/test_delta_adapter.py`
- Modify: `tests/sources/test_iceberg_adapter.py`
- Modify: `tests/sources/test_postgres_integration.py`
- Modify: `tests/sources/test_s3.py`

**Interfaces:**
- Produces: private `SourceRegistry.bind_requirements(requirements)` and source-grain `PhysicalCheck` outcomes.
- Consumes: `SourceScanRequirement`, source adapters, registry lock, and report types.

- [ ] **Step 1: Write a failing query-scoped binding test**

Create a query-scoped fake adapter using the existing registry test doubles. Call the new binding context with one source and assert `bind_query()` receives the exact requested grain columns, cleanup runs, and the registry lock covers the query.

```python
requirements = {
    "events": SourceScanRequirement(columns=("machine_id", "recorded_at"))
}
with registry.bind_requirements(requirements):
    registry.execute('select count(*) from "events"')
assert fake.bind_calls == [requirements["events"]]
assert fake.cleanup_calls == 1
```

- [ ] **Step 2: Write failing grain audit tests**

Use small temporary CSV or Parquet sources with declared schemas:

- valid single-column grain;
- valid composite grain;
- one null grain field;
- one duplicated composite tuple.

Assert counts but never offending values:

```python
report = verify(layer, PhysicalCheck())
outcome = next(item for item in report.outcomes if item.check_id == "source.events.grain")
assert outcome.status == "failed"
assert outcome.evidence["null_grain_rows"] == 1
assert outcome.evidence["duplicate_grain_groups"] == 1
assert "secret-key-value" not in repr(report.to_dict())
```

- [ ] **Step 3: Run tests and confirm missing binding and dispatch**

Run:

```bash
uv run pytest \
  tests/sources/test_registry.py \
  tests/verification/test_audit.py -q
```

Expected: new tests fail because the registry has no requirement-binding context and `PhysicalCheck` is unsupported.

- [ ] **Step 4: Refactor `bind()` through a requirement context**

Extract current query-scoped binding logic without changing its behavior:

```python
@contextmanager
def bind_requirements(
    self,
    requirements: Mapping[str, SourceScanRequirement],
) -> Iterator[None]:
    self._lock.acquire()
    prepared: list[tuple[SourceAdapter, SourceHandle, str]] = []
    bindings: list[QueryBinding] = []
    bind_failed_source: str | None = None
    try:
        for source_id in sorted(requirements):
            registration = self._registrations.get(source_id)
            if registration is None or not registration.handle.query_scoped:
                continue
            adapter = registration.adapter
            handle = registration.handle
            requirement = requirements[source_id]
            try:
                binding = adapter.bind_query(self._connection, handle, requirement)
                if binding is not None:
                    bindings.append(binding)
                    continue
                source = self._sources[source_id]
                fresh = adapter.prepare(source, self._profiles, self._arrow_providers)
                adapter.register(self._connection, source_id, fresh)
                prepared.append((adapter, fresh, source_id))
            except Exception:  # noqa: BLE001
                bind_failed_source = source_id
                break
        if bind_failed_source is None:
            yield
    finally:
        for binding in reversed(bindings):
            binding.cleanup()
        for adapter, fresh, source_id in reversed(prepared):
            unregister_quietly(self._connection, source_id)
            close_quietly(adapter, fresh)
        self._lock.release()
    if bind_failed_source is not None:
        raise SourceConnectionError(
            bind_failed_source,
            "bind_failed",
            "the source could not be bound for the query",
        )

@contextmanager
def bind(self, plan: QueryPlan) -> Iterator[None]:
    requirements = requirements_for_plan(plan)
    selected = {source_id: requirements[source_id] for source_id in _plan_sources(plan)}
    with self.bind_requirements(selected):
        yield
```

This is the current binding body with requirement calculation moved to the caller. Preserve all current error codes and cleanup order.

- [ ] **Step 5: Implement exact grain SQL**

Build requirements containing every grain column. Under `bind_requirements()`, execute quoted SQL equivalent to:

```sql
select
  count(*) as row_count,
  count(*) filter (where "g1" is null or "g2" is null) as null_grain_rows,
  count(*) - count(distinct struct_pack(g1 := "g1", g2 := "g2"))
    as duplicate_row_count,
  (
    select count(*)
    from (
      select "g1", "g2"
      from "source"
      group by "g1", "g2"
      having count(*) > 1
    ) duplicates
  ) as duplicate_grain_groups
from "source"
```

Generate `struct_pack` field aliases internally, not from source values. Quote all source and column identifiers with one shared audit helper. For a single grain column, use the same grouped duplicate query and avoid dialect-specific tuple syntax.

Create one `source.{source_id}.grain` outcome per source with connector kind, registry generation, safe snapshot or version, schema fingerprint, row count, distinct grain count, null count, and duplicate-group count. Read connector metadata through `registry.status(source_id)` so audit code never inspects adapter handles.

- [ ] **Step 6: Handle resources and unavailable sources**

Create a private in-memory DuckDB connection and `SourceRegistry` for the audit. Close both in `finally`. Adapt sanitized `SourceError` values to `unavailable` outcomes. Do not catch unexpected programmer errors.

Add exact-type dispatch for `PhysicalCheck` in `verification.__init__.verify()`.

- [ ] **Step 7: Add connector-specific audit smoke tests**

Reuse existing connector fixtures to run one valid full grain audit through:

- programmatic Arrow and local Parquet/CSV;
- SQLite and DuckDB files;
- local Delta and Iceberg fixtures;
- PostgreSQL through the existing testcontainer fixture;
- S3-backed Parquet through the existing MinIO fixture.

Each test asserts connector kind, generation, schema fingerprint, safe snapshot value where the adapter supplies one, full-scan scope, and zero leaked location or credential text. Keep PostgreSQL and S3 tests under their existing integration marker.

- [ ] **Step 8: Run registry, adapter, and grain tests**

Run:

```bash
uv run pytest \
  tests/verification/test_audit.py \
  tests/sources/test_registry.py \
  tests/sources/test_adapter_contract.py \
  tests/sources/test_arrow_adapter.py \
  tests/sources/test_database_adapters.py \
  tests/sources/test_delta_adapter.py \
  tests/sources/test_iceberg_adapter.py -q
```

Expected: all non-integration tests pass.

When Docker is available, also run:

```bash
uv run pytest \
  tests/sources/test_postgres_integration.py \
  tests/sources/test_s3.py -m integration -q
```

Expected: all integration audit smoke tests pass.

- [ ] **Step 9: Commit**

```bash
git add \
  src/selayer/sources/registry.py \
  src/selayer/verification \
  tests/sources/test_registry.py \
  tests/sources/test_delta_adapter.py \
  tests/sources/test_iceberg_adapter.py \
  tests/sources/test_postgres_integration.py \
  tests/sources/test_s3.py \
  tests/verification/test_audit.py
git commit -m "feat(verification): audit source grains exactly"
```

### Task 13: Add exact relationship audits

**Files:**
- Modify: `src/selayer/verification/audit.py`
- Modify: `tests/verification/test_audit.py`

**Interfaces:**
- Produces: one relationship outcome for every declared relationship.
- Consumes: Task 12 registry binding and exact SQL helpers.

- [ ] **Step 1: Write one-to-many success and failure tests**

Create deterministic parent and child sources and assert:

```python
outcome = relationship_outcome(report, "orders_items")
assert outcome.status == "passed"
assert outcome.evidence == {
    "one_side_null_rows": 0,
    "one_side_duplicate_groups": 0,
    "many_side_null_rows": 1,
    "orphan_non_null_rows": 0,
    "zero_child_one_side_rows": 1,
    "maximum_child_multiplicity": 2,
}
```

Add separate fixtures with duplicate one-side keys and non-null orphan child keys. Assert failure codes and counts without values.

- [ ] **Step 2: Add one-to-one, many-to-one, and many-to-many tests**

Verify:

- one-to-one uniqueness, nullness, and unmatched keys in both directions;
- many-to-one uses the relationship target as the one side;
- many-to-many records unmatched non-null counts and an informational no-safe-traversal diagnostic without uniqueness failure;
- nullable many-side keys are accepted;
- zero-child one-side rows remain informational.

- [ ] **Step 3: Run tests and observe missing relationship outcomes**

Run:

```bash
uv run pytest tests/verification/test_audit.py -q
```

Expected: relationship tests fail because the report contains source outcomes only.

- [ ] **Step 4: Implement cardinality normalization**

```python
@dataclass(frozen=True, slots=True)
class _RelationshipSides:
    one_source: str | None
    one_column: str | None
    many_source: str | None
    many_column: str | None


def _relationship_sides(relationship: Relationship) -> _RelationshipSides:
    if relationship.type == "one_to_many":
        return _RelationshipSides(
            relationship.source,
            relationship.source_column,
            relationship.target,
            relationship.target_column,
        )
    if relationship.type == "many_to_one":
        return _RelationshipSides(
            relationship.target,
            relationship.target_column,
            relationship.source,
            relationship.source_column,
        )
    return _RelationshipSides(None, None, None, None)
```

Handle one-to-one and many-to-many in dedicated query helpers so their semantics remain explicit.

- [ ] **Step 5: Implement exact relationship SQL**

Bind both sources with the required join columns. Run aggregate-only queries for:

- one-side null rows;
- one-side duplicate groups;
- many-side null rows;
- non-null orphan rows using `not exists`;
- zero-child one-side rows using `not exists`;
- maximum child multiplicity;
- reverse unmatched counts for one-to-one and many-to-many.

Never select key values. Use quoted identifiers and stable generated aliases.

- [ ] **Step 6: Add outcome and report completeness rules**

A one-to-many or many-to-one relationship fails on one-side nulls, one-side duplicates, or non-null many-side orphans. Zero-child parents and nullable many-side keys remain informational.

A one-to-one relationship fails on nulls, duplicates, or unmatched non-null keys on either side.

A many-to-many relationship does not claim uniqueness or safe traversal. It fails only when the chosen referential policy detects unmatched non-null keys, and otherwise passes with an informational diagnostic.

Any unavailable bound source makes the relationship outcome unavailable and the report incomplete.

- [ ] **Step 7: Run audit tests and leak scans**

Run:

```bash
uv run pytest tests/verification/test_audit.py -q
uv run ruff check src/selayer/verification/audit.py tests/verification/test_audit.py
uv run pyright src/selayer/verification/audit.py tests/verification/test_audit.py
```

Expected: all commands pass.

- [ ] **Step 8: Commit**

```bash
git add src/selayer/verification/audit.py tests/verification/test_audit.py
git commit -m "feat(verification): audit relationship cardinality"
```

### Task 14: Add the audit CLI and finish public documentation

**Files:**
- Modify: `src/selayer/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `.github/copilot-instructions.md`

**Interfaces:**
- Produces: `selayer catalog audit CATALOG [--profiles FILE]` and final documented verification workflow.
- Consumes: profile parser, `PhysicalCheck`, unified CLI report rendering, and all prior stages.

- [ ] **Step 1: Write failing audit CLI tests**

```python
def test_catalog_audit_uses_profile_file_without_leaking_values(
    local_catalog_path: Path,
    profile_file: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WAREHOUSE_DSN", "sentinel-secret-dsn")
    code = main([
        "catalog", "audit", str(local_catalog_path),
        "--profiles", str(profile_file),
    ])
    captured = capsys.readouterr()
    assert code in {0, 1}
    assert "sentinel-secret-dsn" not in captured.out
    assert "sentinel-secret-dsn" not in captured.err
    payload = json.loads(captured.out)
    assert payload["check_kind"] == "physical"
```

Add tests for a passing credential-free local catalog, failed grain audit exit `1`, missing profile environment exit `1`, unavailable Arrow provider exit `1`, and argparse usage exit `2`.

- [ ] **Step 2: Run tests and confirm the audit command is missing**

Run:

```bash
uv run pytest tests/test_cli.py -q
```

Expected: new audit tests fail at argument parsing.

- [ ] **Step 3: Implement audit command dispatch**

Add:

```text
selayer catalog audit CATALOG [--profiles FILE]
```

Handler order:

1. call `validate_catalog()`;
2. print and exit `1` if static validation fails;
3. load `MappingProfileResolver({})` when `--profiles` is absent;
4. otherwise call `load_profile_file()`;
5. call `verify(layer, PhysicalCheck(profiles=resolver))`;
6. print one sorted JSON object;
7. return `0` only when `report.passed`.

Do not initialize an Arrow provider resolver from configuration.

- [ ] **Step 4: Update README commands and contracts**

Document exact examples:

```bash
uv run selayer catalog validate ecommerce_semantic_layer.yaml
uv run selayer catalog compatibility ecommerce_semantic_layer.yaml
uv run selayer catalog audit ecommerce_semantic_layer.yaml
uv run selayer catalog audit catalog.yaml --profiles runtime-profiles.yaml
uv run selayer okf build catalog.yaml knowledge \
  --references business_context \
  --overlays okf_overlays
```

Explain validation versus verification, exact scan cost, per-source snapshot limitations, profile file environment resolution, compatibility coverage, fresh-only overlay composition, and unchanged `selayer-okf` behavior.

- [ ] **Step 5: Update Copilot instructions**

Add `src/selayer/verification/` and `src/selayer/okf/` to active modules. Replace the stale prohibition with:

```markdown
The catalog is execution authority. OKF is advisory and cannot add or override
queryable dimensions, facts, measures, metrics, relationships, planning, or
execution behavior. Keep LLM and agent orchestration outside the installable
package.
```

- [ ] **Step 6: Run all targeted tests**

Run:

```bash
uv run pytest \
  tests/verification \
  tests/sources/test_profile_file.py \
  tests/sources/test_profiles.py \
  tests/sources/test_registry.py \
  tests/okf \
  tests/test_catalog.py \
  tests/test_query.py \
  tests/test_cli.py \
  tests/planning -q
```

Expected: all tests pass.

- [ ] **Step 7: Run project-wide verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests examples
uv build
```

Expected: every command exits `0`.

- [ ] **Step 8: Run CLI smoke checks**

Run:

```bash
uv run selayer catalog validate examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer catalog compatibility examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer catalog audit examples/shopfloor/shopfloor_semantic_layer.yaml
TMP_ROOT=$(mktemp -d)
uv run selayer okf generate \
  examples/shopfloor/shopfloor_semantic_layer.yaml \
  "$TMP_ROOT/knowledge"
uv run selayer okf validate "$TMP_ROOT/knowledge" \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer-okf validate "$TMP_ROOT/knowledge" \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
rm -rf "$TMP_ROOT"
```

Expected:

- catalog validation passes;
- compatibility records both compatible and planner-rejected combinations, completes every requested check, and exits successfully;
- physical audit passes against the current physical data state;
- temporary OKF generation succeeds;
- both OKF validation forms produce equivalent successful output.

- [ ] **Step 9: Commit**

```bash
git add \
  src/selayer/cli.py \
  tests/test_cli.py \
  README.md \
  .github/copilot-instructions.md
git commit -m "docs(verification): document validation workflow"
```

## Completion audit

Before declaring the plan complete during implementation, map each design requirement to evidence:

- Report schema, immutability, ordering, and stable codes: Task 1 and Task 2 tests.
- Shared static rules and programmatic boundary: Task 3 and Task 4 tests.
- Unified CLI and legacy compatibility: Task 5, Task 9, and Task 14 tests.
- Catalog-aware OKF integrity: Task 6 tests.
- Restricted fresh composition and atomicity: Task 7 and Task 8 tests.
- Planner-only compatibility: Task 10 tests.
- Environment-backed profile file: Task 11 tests.
- Exact source and relationship audits: Task 12 and Task 13 tests.
- Secret non-disclosure: Tasks 4, 11, 12, 13, and 14 tests.
- Documentation and full-project health: Task 14 commands.

Do not mark implementation complete if a required connector check is silently skipped, a report is incomplete, a legacy CLI output changes unexpectedly, or the hardened shopfloor follow-up cannot consume `OkfBundle.build()` as specified.
