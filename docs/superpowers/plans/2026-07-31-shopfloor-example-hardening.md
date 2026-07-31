# Shopfloor example hardening implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `examples/shopfloor` into a deterministic reference for safe grain-aware modeling, exact verification, immutable reload demonstrations, and on-demand enriched OKF generation.

**Architecture:** Generate all runtime data in temporary directories and rebase an already validated `SemanticLayer` onto those files. Keep the YAML catalog as execution authority, version reviewed business documents and sparse overlays, and use the library's `verify()` and `OkfBundle.build()` interfaces for checks and composition. A shopfloor-only policy validates documentation coverage and structured query examples before publishing generated knowledge.

**Tech Stack:** Python 3.13, dataclasses, pathlib, argparse, tempfile, PyYAML, PyArrow, DuckDB, Delta Lake, selayer verification API, selayer OKF API, pytest, Ruff, Pyright, uv.

## Prerequisites

Complete the selayer verification implementation plan through Stage 4 first:

- `docs/superpowers/plans/2026-07-31-selayer-verification.md`

This plan consumes these exact interfaces:

```python
from selayer.verification import (
    CompatibilityCheck,
    PhysicalCheck,
    StaticCheck,
    VerificationReport,
    verify,
)

OkfBundle.build(
    layer,
    output_dir,
    references_dir=references_dir,
    overlays_dir=overlays_dir,
)
```

Do not add a second grain auditor, compatibility graph, generated-integrity checker, or generic overlay merger under `examples/shopfloor`.

## Global constraints

- Keep the example a small deterministic teaching fixture.
- Intentional catalog identifier changes are allowed. Update scripts, tests, documentation, and overlays together.
- Keep all eight source connectors and the six safe relationships.
- Repository source data remains generated and ignored by Git.
- `run_example.py` must not write to `examples/shopfloor/data/`.
- The baseline has exactly three EOL attempts and EOL attempt pass rate `2/3`.
- The temporary retest adds exactly one attempt and changes only EOL attempt pass rate to `3/4`; first-pass yield remains `2/3`.
- Do not add a direct operation-to-telemetry relationship.
- Add only source-backed time dimensions.
- Keep all twelve headline metric names and formulas.
- Catalog YAML remains execution authority. Business documents and OKF remain advisory.
- Generated OKF output goes under `.generated/` or an explicit output directory and is never committed.
- Version business-context References and overlays.
- Overlays may contain only the frontmatter and sections allowed by `OkfBundle.build()`.
- Do not write `verified` claims from scripts or overlays.
- Structured examples contain declarative query requests, not executable Python or SQL.
- No build step may publish partial knowledge output.
- Use `uv` for every command.
- Follow TDD for every behavior change.

---

## File structure

### New authored files

```text
examples/shopfloor/
├── business_context/
│   ├── glossary.md
│   ├── kpi_definitions.md
│   ├── process_overview.md
│   └── quality_policy.md
├── okf_overlays/
│   ├── dimensions/
│   ├── facts/
│   ├── measures/
│   ├── metrics/
│   ├── relationships/
│   └── sources/
├── build_knowledge.py
└── knowledge_policy.py
```

### Existing files changed

- `examples/shopfloor/generate_data.py`: command-line entry point and unchanged deterministic baseline generation.
- `examples/shopfloor/run_example.py`: temporary runtime data and rebased layer.
- `examples/shopfloor/shopfloor_semantic_layer.yaml`: conformed drive identity, domain-specific operation and telemetry names, and supported time dimensions.
- `examples/shopfloor/README.md`: new ownership, generation, verification, and knowledge workflow.
- `tests/integration/test_shopfloor.py`: baseline, reload, semantic, audit, policy, and end-to-end acceptance tests.
- `.gitignore`: ignore `examples/shopfloor/.generated/`.

The existing generated `examples/shopfloor/knowledge/` directory is not a source artifact. Do not stage it. The new default output is `examples/shopfloor/.generated/knowledge/`.

---

### Task 1: Add an explicit deterministic data-generation command

**Files:**
- Modify: `examples/shopfloor/generate_data.py:1-692`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int` and the existing `generate_shopfloor_data(output_dir) -> ShopfloorDataPaths`.
- Consumes: existing source writers and `DeltaDependencyError`.

- [ ] **Step 1: Write failing CLI tests**

```python
from examples.shopfloor.generate_data import main as generate_main


def test_generate_data_cli_requires_output_dir(tmp_path: Path) -> None:
    output = tmp_path / "shopfloor-data"
    assert generate_main(["--output-dir", str(output)]) == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "component_consumption.parquet",
        "component_lot_inspections.parquet",
        "customer_orders.csv",
        "eol_test_runs.delta",
        "machine_telemetry.parquet",
        "operation_executions.parquet",
        "production_orders.sqlite",
        "shopfloor.duckdb",
    ]


def test_generate_data_cli_resets_an_existing_retest(tmp_path: Path) -> None:
    output = tmp_path / "shopfloor-data"
    assert generate_main(["--output-dir", str(output)]) == 0
    append_eol_retest(output / "eol_test_runs.delta")
    assert _delta_row_count(output / "eol_test_runs.delta") == 4
    assert generate_main(["--output-dir", str(output)]) == 0
    assert _delta_row_count(output / "eol_test_runs.delta") == 3
```

Use the existing optional-Delta skip or failure convention in the integration module. `_delta_row_count()` must read the generated Delta table, not generator constants.

- [ ] **Step 2: Run tests and confirm the CLI is missing**

Run:

```bash
uv run pytest \
  tests/integration/test_shopfloor.py::test_generate_data_cli_requires_output_dir \
  tests/integration/test_shopfloor.py::test_generate_data_cli_resets_an_existing_retest -q
```

Expected: tests fail because `generate_data.main` does not exist.

- [ ] **Step 3: Implement the command-line entry point**

```python
# examples/shopfloor/generate_data.py
import argparse
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic shop-floor connector inputs."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        generate_shopfloor_data(arguments.output_dir)
    except DeltaDependencyError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Import `sys`. Do not change `generate_shopfloor_data()` reset behavior or any logical fixture rows.

- [ ] **Step 4: Run generation tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "generate_data" -q
```

Expected: all generation tests pass.

- [ ] **Step 5: Run the command twice in a temporary directory**

Run:

```bash
TMP_ROOT=$(mktemp -d)
uv run python examples/shopfloor/generate_data.py --output-dir "$TMP_ROOT/data"
uv run python examples/shopfloor/generate_data.py --output-dir "$TMP_ROOT/data"
find "$TMP_ROOT/data" -maxdepth 1 -mindepth 1 -print | sort
rm -rf "$TMP_ROOT"
```

Expected: both commands exit `0`; the final listing contains the eight expected source paths.

- [ ] **Step 6: Commit**

```bash
git add examples/shopfloor/generate_data.py tests/integration/test_shopfloor.py
git commit -m "feat(shopfloor): add explicit data generator command"
```

### Task 2: Run the walkthrough entirely on temporary data

**Files:**
- Modify: `examples/shopfloor/run_example.py:1-70`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: `_layer_for_paths(layer, paths) -> SemanticLayer` and a `main()` that leaves repository data untouched.
- Consumes: `ShopfloorDataPaths`, connector config dataclasses, `generate_shopfloor_data()`, and `QueryEngine` boundary validation.

- [ ] **Step 1: Write a failing rebasing test**

```python
from examples.shopfloor.run_example import _layer_for_paths


def test_layer_for_paths_rebases_every_source(
    tmp_path: Path,
) -> None:
    paths = generate_shopfloor_data(tmp_path / "data")
    original = SemanticLayer.load(SHOPFLOOR_CATALOG)
    rebased = _layer_for_paths(original, paths)
    expected = {
        "customer_orders": paths.customer_orders,
        "production_orders": paths.production_orders_db,
        "serialized_drives": paths.shopfloor_db,
        "component_consumption": paths.component_consumption,
        "component_lot_inspections": paths.component_lot_inspections,
        "operation_executions": paths.operation_executions,
        "machine_telemetry": paths.machine_telemetry,
        "eol_test_runs": paths.eol_test_runs,
    }
    assert {
        name: Path(source.connector.location)
        for name, source in rebased.data_sources.items()
    } == expected
    assert SemanticLayer.load(SHOPFLOOR_CATALOG).data_sources == original.data_sources
```

Narrow connector typing with `isinstance()` against `CsvConfig`, `ParquetConfig`, `SqliteConfig`, `DuckDbConfig`, and `DeltaConfig`.

- [ ] **Step 2: Write a failing repository-nonmutation test**

```python
def test_runner_does_not_write_repository_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository_data = SHOPFLOOR_ROOT / "data"
    before = _logical_source_snapshot(repository_data) if repository_data.exists() else None
    monkeypatch.setattr("examples.shopfloor.run_example.TemporaryDirectory", _temp_factory(tmp_path))
    assert run_main() == 0
    after = _logical_source_snapshot(repository_data) if repository_data.exists() else None
    assert after == before
```

The snapshot helper records relative filenames, logical row counts, and Delta versions without assuming byte-identical database metadata.

- [ ] **Step 3: Run tests and observe current repository writes**

Run:

```bash
uv run pytest \
  tests/integration/test_shopfloor.py::test_layer_for_paths_rebases_every_source \
  tests/integration/test_shopfloor.py::test_runner_does_not_write_repository_data -q
```

Expected: tests fail because the helper is missing and current `main()` writes `examples/shopfloor/data/`.

- [ ] **Step 4: Implement typed source rebasing**

```python
from dataclasses import replace
from tempfile import TemporaryDirectory

from selayer.sources.config import (
    CsvConfig,
    DeltaConfig,
    DuckDbConfig,
    ParquetConfig,
    SqliteConfig,
)

_LOCATION_CONNECTORS = (
    CsvConfig,
    DeltaConfig,
    DuckDbConfig,
    ParquetConfig,
    SqliteConfig,
)


def _layer_for_paths(
    layer: SemanticLayer,
    paths: ShopfloorDataPaths,
) -> SemanticLayer:
    locations = {
        "customer_orders": paths.customer_orders,
        "production_orders": paths.production_orders_db,
        "serialized_drives": paths.shopfloor_db,
        "component_consumption": paths.component_consumption,
        "component_lot_inspections": paths.component_lot_inspections,
        "operation_executions": paths.operation_executions,
        "machine_telemetry": paths.machine_telemetry,
        "eol_test_runs": paths.eol_test_runs,
    }
    sources = {}
    for name, source in layer.data_sources.items():
        connector = source.connector
        if not isinstance(connector, _LOCATION_CONNECTORS):
            raise TypeError("shopfloor source has no file location")
        sources[name] = replace(
            source,
            connector=replace(connector, location=str(locations[name])),
        )
    return replace(layer, data_sources=sources)
```

`QueryEngine` will revalidate the returned programmatic layer before opening sources.

- [ ] **Step 5: Move `main()` into a temporary directory**

```python
def main() -> int:
    try:
        with TemporaryDirectory(prefix="selayer-shopfloor-") as directory:
            paths = generate_shopfloor_data(Path(directory) / "data")
            layer = _layer_for_paths(SemanticLayer.load(CATALOG), paths)
            with QueryEngine(layer) as engine:
                run_walkthrough(engine, paths.eol_test_runs)
    except DeltaDependencyError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Remove `DATA_DIR`. Keep `run_walkthrough()` independently testable.

- [ ] **Step 6: Verify baseline and temporary reload values**

Update the runner capture test to assert output contains:

```text
EOL quality before Delta reload
EOL source generation: 0 -> 1
EOL pass rate after Delta reload
```

Query the temporary layer directly and assert pass rate changes `2/3` to `3/4`, while first-pass yield stays `2/3`.

- [ ] **Step 7: Run runner and integration tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "runner or reload or rebase" -q
uv run python examples/shopfloor/run_example.py
```

Expected: tests pass; the walkthrough exits `0`; repository data stays untouched.

- [ ] **Step 8: Commit**

```bash
git add examples/shopfloor/run_example.py tests/integration/test_shopfloor.py
git commit -m "fix(shopfloor): isolate reload walkthrough data"
```

### Task 3: Correct conformed identities and supported time dimensions

**Files:**
- Modify: `examples/shopfloor/shopfloor_semantic_layer.yaml`
- Modify: `examples/shopfloor/run_example.py`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: corrected semantic IDs and a catalog that passes `StaticCheck`.
- Consumes: unchanged physical source schemas and relationships.

- [ ] **Step 1: Write failing catalog-shape tests**

```python
def test_shopfloor_catalog_uses_conformed_and_domain_specific_dimensions() -> None:
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    assert layer.dimension("drive_serial_number").source == "serialized_drives"
    assert layer.dimension("operation_line_id").source == "operation_executions"
    assert layer.dimension("operation_machine_id").source == "operation_executions"
    assert layer.dimension("telemetry_line_id").source == "machine_telemetry"
    assert layer.dimension("telemetry_machine_id").source == "machine_telemetry"
    assert layer.dimension("requested_ship_date").data_type == "date"
    assert layer.dimension("telemetry_recorded_at").data_type == "timestamp"
    with pytest.raises(KeyError):
        layer.dimension("line_id")
    with pytest.raises(KeyError):
        layer.dimension("machine_id")
```

- [ ] **Step 2: Write failing fact-rename tests**

```python
def test_telemetry_count_facts_are_event_markers() -> None:
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    assert layer.measure("telemetry_event_count").fact == "telemetry_event_machine_id"
    assert layer.measure("alarm_event_count_measure").fact == "alarm_event_machine_id"
    with pytest.raises(KeyError):
        layer.fact("telemetry_machine_id")
    with pytest.raises(KeyError):
        layer.fact("alarm_machine_id")
```

- [ ] **Step 3: Run tests and confirm current names and source mappings fail**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "conformed or domain_specific or event_markers" -q
```

Expected: new tests fail.

- [ ] **Step 4: Update dimensions**

Apply these exact catalog changes:

```yaml
dimensions:
  requested_ship_date:
    source: customer_orders
    column: requested_ship_date
    data_type: date
    description: Requested customer shipment date
  drive_serial_number:
    source: serialized_drives
    column: serial_number
    data_type: string
    description: Conformed drive serial number from the drive registry
  operation_line_id:
    source: operation_executions
    column: line_id
    data_type: string
    description: Production line recorded by operation execution
  operation_machine_id:
    source: operation_executions
    column: machine_id
    data_type: string
    description: Machine recorded by operation execution
  telemetry_machine_id:
    source: machine_telemetry
    column: machine_id
    data_type: string
    description: Machine recorded by telemetry
  telemetry_recorded_at:
    source: machine_telemetry
    column: recorded_at
    data_type: timestamp
    description: Telemetry sample timestamp
```

Keep `telemetry_line_id` and update its description to state that it is telemetry-local. Remove `line_id` and `machine_id`.

- [ ] **Step 5: Rename telemetry marker facts**

```yaml
facts:
  telemetry_event_machine_id:
    source: machine_telemetry
    expression: machine_telemetry.machine_id
    data_type: string
    description: Non-null machine marker for one telemetry event
  alarm_event_machine_id:
    source: machine_telemetry
    expression: >-
      if(machine_telemetry.machine_state = 'alarm',
      machine_telemetry.machine_id, null)
    data_type: string
    description: Non-null machine marker for one alarm telemetry event
```

Update `telemetry_event_count.fact` and `alarm_event_count_measure.fact` to the new IDs. Do not change metric formulas.

- [ ] **Step 6: Update walkthrough query names**

Replace operation dimensions with:

```python
[
    "operation_line_id",
    "operation_machine_id",
    "shift",
    "operation_name",
]
```

Add `telemetry_machine_id` to the machine-health query and keep telemetry dimensions separate from operation dimensions.

- [ ] **Step 7: Add planning tests for intended semantics**

Using a rebased temporary layer, assert:

```python
engine.plan(["component_count"], ["drive_serial_number"])
engine.plan(["average_cycle_seconds"], ["drive_serial_number", "operation_machine_id"])
engine.plan(["eol_attempt_pass_rate"], ["drive_serial_number", "station_id"])
engine.plan(["production_completion_rate"], ["requested_ship_date"])
engine.plan(["average_temperature_c"], ["telemetry_recorded_at", "telemetry_machine_id"])
```

Also assert:

```python
with pytest.raises(QueryPlanningError) as raised:
    engine.plan(["average_cycle_seconds"], ["telemetry_machine_id"])
assert raised.value.code == "no_relationship_path"

with pytest.raises(QueryPlanningError) as raised:
    engine.plan(["average_temperature_c"], ["operation_machine_id"])
assert raised.value.code == "no_relationship_path"
```

- [ ] **Step 8: Run catalog and shopfloor tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py tests/test_catalog.py tests/planning -q
uv run selayer catalog validate examples/shopfloor/shopfloor_semantic_layer.yaml
```

Expected: all tests and static validation pass.

- [ ] **Step 9: Commit**

```bash
git add \
  examples/shopfloor/shopfloor_semantic_layer.yaml \
  examples/shopfloor/run_example.py \
  tests/integration/test_shopfloor.py
git commit -m "refactor(shopfloor): clarify semantic identities"
```

### Task 4: Verify physical declarations and business invariants

**Files:**
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Consumes: `verify(layer, PhysicalCheck())` and temporary generated source files.
- Produces: fixture-level assertions for data semantics not represented by catalog version 1.

- [ ] **Step 1: Write a failing exact physical-audit test**

```python
def test_shopfloor_physical_audit_passes(tmp_path: Path) -> None:
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = _layer_for_paths(SemanticLayer.load(SHOPFLOOR_CATALOG), paths)
    report = verify(layer, PhysicalCheck())
    assert report.complete
    assert report.passed
    assert all(outcome.scope == "full_scan" for outcome in report.outcomes)
    assert {
        outcome.check_id for outcome in report.outcomes
        if outcome.check_id.startswith("source.")
    } == {
        "source.customer_orders.grain",
        "source.production_orders.grain",
        "source.serialized_drives.grain",
        "source.component_consumption.grain",
        "source.component_lot_inspections.grain",
        "source.operation_executions.grain",
        "source.machine_telemetry.grain",
        "source.eol_test_runs.grain",
    }
```

Also assert six relationship outcomes exist and pass.

- [ ] **Step 2: Write fixture-level business-rule tests**

Read the generated files and assert these exact rules:

```python
assert all(row["completed_units"] <= row["planned_units"] for row in production_orders)
assert all(row["attempt"] >= 1 for row in eol_runs)
assert len({(row["serial_number"], row["attempt"]) for row in eol_runs}) == len(eol_runs)
assert all(
    row["is_first_pass"] == (row["attempt"] == 1 and row["result"] == "pass")
    for row in eol_runs
)
assert all(
    (row["incoming_result"], row["disposition"])
    in {("pass", "released"), ("fail", "quarantined")}
    for row in inspections
)
```

Assert exact domains:

```python
assert {row["result"] for row in eol_runs} <= {"pass", "fail"}
assert {row["result"] for row in operation_executions} <= {"pass", "fail"}
assert {row["shipment_status"] for row in serialized_drives} <= {"shipped", "in_stock"}
assert {row["machine_state"] for row in telemetry} <= {"running", "idle", "alarm"}
assert {row["schedule_status"] for row in production_orders} <= {"on_time", "late", "open"}
```

Use `sqlite3` for production orders, DuckDB read-only access for serialized drives, `pyarrow.parquet` for Parquet, and `DeltaTable.to_pyarrow_table()` for Delta. Close every connection.

- [ ] **Step 3: Run tests and confirm the verification dependency**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "physical_audit or business_rule" -q
```

Expected after the verification prerequisite: tests pass against the current deterministic rows. If any domain literal differs, correct the assertion only after reconciling it with `generate_data.py` and the approved design.

- [ ] **Step 4: Add expected aggregate evidence assertions**

Assert source row counts:

```python
expected_rows = {
    "customer_orders": 2,
    "production_orders": 3,
    "serialized_drives": 3,
    "component_consumption": 6,
    "component_lot_inspections": 5,
    "operation_executions": 7,
    "machine_telemetry": 4,
    "eol_test_runs": 3,
}
```

For every grain outcome, assert `null_grain_rows == 0` and `duplicate_grain_groups == 0`. For every relationship outcome, assert `orphan_non_null_rows == 0`.

- [ ] **Step 5: Verify planner compatibility coverage**

```python
report = verify(
    layer,
    CompatibilityCheck(
        query_cases=(
            QueryRequest(["component_count"], ["drive_serial_number"]),
            QueryRequest(["average_cycle_seconds"], ["operation_machine_id"]),
            QueryRequest(["average_temperature_c"], ["telemetry_machine_id"]),
            QueryRequest(["average_cycle_seconds", "eol_attempt_pass_rate"]),
        )
    ),
)
assert report.complete
assert report.passed
assert any(
    item.evidence.get("planner_code") == "mixed_grain"
    for item in report.outcomes
)
```

Compatibility rejection is an observed planner result, not a failed verification outcome.

- [ ] **Step 6: Run integration tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -q
```

Expected: all shopfloor tests pass.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/test_shopfloor.py
git commit -m "test(shopfloor): verify physical and business rules"
```

### Task 5: Add reviewed business-context Reference concepts

**Files:**
- Create: `examples/shopfloor/business_context/process_overview.md`
- Create: `examples/shopfloor/business_context/kpi_definitions.md`
- Create: `examples/shopfloor/business_context/quality_policy.md`
- Create: `examples/shopfloor/business_context/glossary.md`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: four valid authored OKF `Reference` concepts without `selayer_id`.
- Consumes: approved fixture story, metric formulas, and business rules from Tasks 3 and 4.

- [ ] **Step 1: Write a failing Reference-validation test**

```python
def test_business_context_is_four_valid_reference_concepts(tmp_path: Path) -> None:
    output = tmp_path / "knowledge"
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    bundle = OkfBundle.build(
        layer,
        output,
        references_dir=SHOPFLOOR_ROOT / "business_context",
    )
    references = {
        path: concept
        for path, concept in bundle.concepts.items()
        if path.startswith("references/")
    }
    assert set(references) == {
        "references/glossary.md",
        "references/kpi_definitions.md",
        "references/process_overview.md",
        "references/quality_policy.md",
    }
    assert all(concept.frontmatter["type"] == "Reference" for concept in references.values())
    assert all("selayer_id" not in concept.frontmatter for concept in references.values())
```

- [ ] **Step 2: Run the test and confirm the corpus is absent**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py::test_business_context_is_four_valid_reference_concepts -q
```

Expected: failure because `business_context/` does not exist.

- [ ] **Step 3: Write common frontmatter**

Each document uses:

```yaml
---
type: Reference
title: <specific title>
status: stable
x_selayer_document_id: shopfloor.<specific_id>
x_selayer_owner: manufacturing-analytics
x_selayer_version: "1"
---
```

Use exact IDs:

- `shopfloor.process_overview`
- `shopfloor.kpi_definitions`
- `shopfloor.quality_policy`
- `shopfloor.glossary`

Do not add `selayer_id`, `verified`, dynamic timestamps, credentials, or source-row dumps.

- [ ] **Step 4: Write `process_overview.md`**

Required sections and facts:

- `# Purpose`: deterministic motor-drive teaching process.
- `# Order to shipment`: customer order, production order, serialized drive, shipment state.
- `# Component genealogy`: component fitting grain and incoming lot inspection.
- `# Operation execution`: one row per operation execution, including rework.
- `# End-of-line testing`: attempts, pass rate, and first-pass unit logic.
- `# Telemetry`: independent machine samples and deliberate lack of operation-event join.
- `# Data authority`: catalog executes; this document explains.

- [ ] **Step 5: Write `kpi_definitions.md`**

Define all twelve metrics in a table with columns:

```text
Metric | Grain | Numerator or aggregate | Denominator | Unit | Zero behavior
```

Use exact formulas from the catalog. State that `component_count` counts fitted component rows, EOL pass rate counts attempts, first-pass yield counts distinct drives, and alarm count counts telemetry events.

- [ ] **Step 6: Write `quality_policy.md`**

State the exact tested rules:

- pass inspection maps to released;
- fail inspection maps to quarantined;
- rework comes from `is_rework` on operation execution;
- EOL attempts start at one and are unique per drive;
- first pass means attempt one and pass;
- a later passing retest changes attempt pass rate but not first-pass yield;
- completed units cannot exceed planned units in this fixture.

- [ ] **Step 7: Write `glossary.md`**

Define these terms with one canonical meaning each:

- customer order;
- production order;
- serialized drive;
- fitted component;
- component lot;
- operation execution;
- rework operation;
- EOL attempt;
- first-pass unit;
- telemetry event;
- alarm event;
- operation machine;
- telemetry machine;
- conformed drive serial number;
- grain.

- [ ] **Step 8: Run Reference tests and strict bundle load**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "business_context" -q
```

Expected: tests pass and strict bundle loading reports no diagnostics.

- [ ] **Step 9: Commit**

```bash
git add examples/shopfloor/business_context tests/integration/test_shopfloor.py
git commit -m "docs(shopfloor): add reviewed business context"
```

### Task 6: Add curated overlays for all headline metrics

**Files:**
- Create: `examples/shopfloor/okf_overlays/metrics/alarm_event_count.md`
- Create: `examples/shopfloor/okf_overlays/metrics/average_cycle_seconds.md`
- Create: `examples/shopfloor/okf_overlays/metrics/average_temperature_c.md`
- Create: `examples/shopfloor/okf_overlays/metrics/component_count.md`
- Create: `examples/shopfloor/okf_overlays/metrics/energy_per_operation_kwh.md`
- Create: `examples/shopfloor/okf_overlays/metrics/eol_attempt_pass_rate.md`
- Create: `examples/shopfloor/okf_overlays/metrics/first_pass_yield.md`
- Create: `examples/shopfloor/okf_overlays/metrics/incoming_acceptance_rate.md`
- Create: `examples/shopfloor/okf_overlays/metrics/operation_count.md`
- Create: `examples/shopfloor/okf_overlays/metrics/production_completion_rate.md`
- Create: `examples/shopfloor/okf_overlays/metrics/rework_rate.md`
- Create: `examples/shopfloor/okf_overlays/metrics/shipped_unit_count.md`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: one valid overlay per metric with all four curated sections and one declarative query example.
- Consumes: `OkfBundle.build()` overlay contract and business References.

- [ ] **Step 1: Write a failing metric-overlay coverage test**

```python
def test_every_metric_has_complete_curated_overlay(tmp_path: Path) -> None:
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    output = tmp_path / "knowledge"
    bundle = OkfBundle.build(
        layer,
        output,
        references_dir=SHOPFLOOR_ROOT / "business_context",
        overlays_dir=SHOPFLOOR_ROOT / "okf_overlays",
    )
    for metric_name in sorted(layer.metrics):
        concept = bundle.concepts[f"metrics/{metric_name}.md"]
        sections = {section.title: section.content.strip() for section in concept.sections}
        assert sections["Usage Guidance"]
        assert sections["Examples"]
        assert sections["Caveats"]
        assert sections["Related Concepts"]
```

- [ ] **Step 2: Run the test and confirm overlays are absent**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py::test_every_metric_has_complete_curated_overlay -q
```

Expected: failure because `okf_overlays/` does not exist.

- [ ] **Step 3: Use the exact overlay format**

Every metric overlay begins:

```yaml
---
selayer_id: metric.<metric_name>
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---
```

Each has exactly these headings:

```markdown
# Usage Guidance

# Examples

# Caveats

# Related Concepts
```

Each Examples section contains one fenced request:

````markdown
```json selayer-query
{"metrics":["metric_name"],"dimensions":["dimension_name"],"filters":{}}
```
````

No overlay contains title, description, generated metadata, `verified`, `Catalog Definition`, preamble text, or source values.

- [ ] **Step 4: Write production and shipment metric overlays**

Use these exact examples and caveats:

| Metric | Example dimensions | Required caveat | Related measures |
|---|---|---|---|
| `production_completion_rate` | `schedule_status`, `requested_ship_date` | Production-order ratio; do not group through child drives | `total_completed_units`, `total_planned_units` |
| `shipped_unit_count` | `customer_region`, `product_model` | Counts distinct shipped drives, not orders or planned units | `shipped_units` |

Link to measures with relative links such as `../measures/total_completed_units.md`.

- [ ] **Step 5: Write component and incoming-quality overlays**

| Metric | Example dimensions | Required caveat | Related measures |
|---|---|---|---|
| `component_count` | `drive_serial_number`, `component_lot_id` | Counts fitted rows; not distinct component identity | `component_count_measure` |
| `incoming_acceptance_rate` | `supplier_name`, `component_type` | Distinct accepted lots divided by distinct inspected lots | `accepted_component_lot_count`, `inspected_component_lot_count` |

Add `quality_policy.md` as a third source for incoming acceptance.

- [ ] **Step 6: Write operation metric overlays**

Use `operation_line_id`, `operation_machine_id`, `shift`, and `operation_name` examples.

| Metric | Unit or meaning | Required caveat |
|---|---|---|
| `average_cycle_seconds` | seconds per operation execution | Average is non-additive |
| `operation_count` | distinct operation executions | Counts execution IDs, not drives |
| `rework_rate` | rework executions divided by all executions | One drive may have several operations |
| `energy_per_operation_kwh` | kWh per operation execution | Ratio of total energy to distinct operation count |

Link each metric to its declared measures and to `../dimensions/operation_machine_id.md`.

- [ ] **Step 7: Write EOL quality overlays**

Use `drive_serial_number`, `station_id`, `product_model`, and `firmware_revision` examples.

| Metric | Meaning | Required caveat |
|---|---|---|
| `eol_attempt_pass_rate` | passing attempts divided by attempts | A retest can change the rate without changing unit yield |
| `first_pass_yield` | distinct first-pass drives divided by distinct drives with attempt one | Unit metric, not attempt metric |

Add `quality_policy.md` as a source and cross-link the two metrics.

- [ ] **Step 8: Write telemetry overlays**

Use `telemetry_line_id`, `telemetry_machine_id`, and `machine_state` examples.

| Metric | Meaning | Required caveat |
|---|---|---|
| `alarm_event_count` | telemetry rows in alarm state | No safe operation-event join exists |
| `average_temperature_c` | average sampled temperature in degrees Celsius | Sampling frequency affects the average |

Link to telemetry-local dimensions only. Do not link `operation_machine_id` as a related grouping concept.

- [ ] **Step 9: Run composition and metric coverage tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "curated_overlay" -q
```

Expected: all metric overlays compose and every required section is non-empty.

- [ ] **Step 10: Commit**

```bash
git add examples/shopfloor/okf_overlays/metrics tests/integration/test_shopfloor.py
git commit -m "docs(shopfloor): curate metric knowledge"
```

### Task 7: Add structural overlays and shopfloor knowledge policy

**Files:**
- Create: `examples/shopfloor/knowledge_policy.py`
- Create: `examples/shopfloor/okf_overlays/sources/component_consumption.md`
- Create: `examples/shopfloor/okf_overlays/sources/component_lot_inspections.md`
- Create: `examples/shopfloor/okf_overlays/sources/customer_orders.md`
- Create: `examples/shopfloor/okf_overlays/sources/eol_test_runs.md`
- Create: `examples/shopfloor/okf_overlays/sources/machine_telemetry.md`
- Create: `examples/shopfloor/okf_overlays/sources/operation_executions.md`
- Create: `examples/shopfloor/okf_overlays/sources/production_orders.md`
- Create: `examples/shopfloor/okf_overlays/sources/serialized_drives.md`
- Create: `examples/shopfloor/okf_overlays/relationships/component_lot_inspections_component_consumption.md`
- Create: `examples/shopfloor/okf_overlays/relationships/customer_orders_production_orders.md`
- Create: `examples/shopfloor/okf_overlays/relationships/production_orders_serialized_drives.md`
- Create: `examples/shopfloor/okf_overlays/relationships/serialized_drives_component_consumption.md`
- Create: `examples/shopfloor/okf_overlays/relationships/serialized_drives_eol_test_runs.md`
- Create: `examples/shopfloor/okf_overlays/relationships/serialized_drives_operation_executions.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/drive_serial_number.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/operation_line_id.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/operation_machine_id.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/requested_ship_date.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/telemetry_line_id.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/telemetry_machine_id.md`
- Create: `examples/shopfloor/okf_overlays/dimensions/telemetry_recorded_at.md`
- Create: `examples/shopfloor/okf_overlays/facts/alarm_event_machine_id.md`
- Create: `examples/shopfloor/okf_overlays/facts/first_attempt_serial.md`
- Create: `examples/shopfloor/okf_overlays/facts/first_pass_serial.md`
- Create: `examples/shopfloor/okf_overlays/facts/telemetry_event_machine_id.md`
- Create: `examples/shopfloor/okf_overlays/measures/alarm_event_count_measure.md`
- Create: `examples/shopfloor/okf_overlays/measures/component_count_measure.md`
- Create: `examples/shopfloor/okf_overlays/measures/first_attempt_unit_count.md`
- Create: `examples/shopfloor/okf_overlays/measures/first_pass_unit_count.md`
- Create: `examples/shopfloor/okf_overlays/measures/telemetry_event_count.md`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: `validate_shopfloor_knowledge(bundle, layer) -> tuple[ShopfloorKnowledgeIssue, ...]`.
- Consumes: composed `OkfBundle`, `QueryRequest`, `plan_query()`, and metric query blocks from Task 6.

- [ ] **Step 1: Write failing policy tests**

```python
def test_shopfloor_policy_requires_kind_specific_coverage(
    composed_shopfloor_bundle: OkfBundle,
    shopfloor_layer: SemanticLayer,
) -> None:
    issues = validate_shopfloor_knowledge(composed_shopfloor_bundle, shopfloor_layer)
    assert issues == ()


def test_shopfloor_policy_rejects_unplannable_example(
    composed_shopfloor_bundle: OkfBundle,
    shopfloor_layer: SemanticLayer,
) -> None:
    changed = _replace_example_request(
        composed_shopfloor_bundle,
        "metrics/average_cycle_seconds.md",
        {"metrics": ["average_cycle_seconds"], "dimensions": ["telemetry_machine_id"], "filters": {}},
    )
    issues = validate_shopfloor_knowledge(changed, shopfloor_layer)
    assert issues[0].code == "shopfloor.example.unplannable"
```

Add tests for missing required sections, missing required selected overlays, missing query block, malformed JSON, unknown query keys, duplicate query blocks, and an expected mixed-grain request incorrectly presented as valid.

- [ ] **Step 2: Define exact coverage policy**

```python
_REQUIRED_SECTIONS = {
    "metric": frozenset({"Usage Guidance", "Examples", "Caveats", "Related Concepts"}),
    "source": frozenset({"Usage Guidance", "Caveats"}),
    "relationship": frozenset({"Usage Guidance", "Caveats", "Related Concepts"}),
}

_REQUIRED_SELECTED = frozenset({
    "dimension.drive_serial_number",
    "dimension.operation_line_id",
    "dimension.operation_machine_id",
    "dimension.requested_ship_date",
    "dimension.telemetry_line_id",
    "dimension.telemetry_machine_id",
    "dimension.telemetry_recorded_at",
    "fact.alarm_event_machine_id",
    "fact.first_attempt_serial",
    "fact.first_pass_serial",
    "fact.telemetry_event_machine_id",
    "measure.alarm_event_count_measure",
    "measure.component_count_measure",
    "measure.first_attempt_unit_count",
    "measure.first_pass_unit_count",
    "measure.telemetry_event_count",
})
```

- [ ] **Step 3: Implement safe query-block parsing**

```python
_QUERY_FENCE = re.compile(
    r"```json selayer-query\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_ALLOWED_QUERY_KEYS = frozenset({"metrics", "dimensions", "filters"})


def _query_requests(concept: OkfConcept) -> tuple[QueryRequest, ...]:
    examples = next(
        section.content for section in concept.sections
        if section.title == "Examples"
    )
    matches = tuple(_QUERY_FENCE.finditer(examples))
    if len(matches) != 1:
        raise ShopfloorKnowledgeError("metric example must contain one query request")
    payload = json.loads(matches[0].group("body"))
    if type(payload) is not dict or set(payload) - _ALLOWED_QUERY_KEYS:
        raise ShopfloorKnowledgeError("metric query request has invalid fields")
    metrics = payload.get("metrics")
    dimensions = payload.get("dimensions", [])
    filters = payload.get("filters", {})
    if (
        type(metrics) is not list
        or not metrics
        or any(type(item) is not str for item in metrics)
        or type(dimensions) is not list
        or any(type(item) is not str for item in dimensions)
        or type(filters) is not dict
        or any(type(key) is not str for key in filters)
    ):
        raise ShopfloorKnowledgeError("metric query request has invalid values")
    return (QueryRequest(metrics, dimensions, filters),)
```

Do not execute Python, SQL, shell, Markdown links, or code blocks. Convert parsing and planning failures into sorted `ShopfloorKnowledgeIssue` values.

- [ ] **Step 4: Add all source overlays**

Create exact files:

```text
sources/component_consumption.md
sources/component_lot_inspections.md
sources/customer_orders.md
sources/eol_test_runs.md
sources/machine_telemetry.md
sources/operation_executions.md
sources/production_orders.md
sources/serialized_drives.md
```

Each overlay cites `/references/process_overview.md`, states its declared grain and connector usage, and has a caveat naming its scope. Telemetry caveats state that samples do not join to operations. EOL caveats distinguish attempts from drives. Component consumption caveats state that fitted position is part of grain.

- [ ] **Step 5: Add all relationship overlays**

Create exact files for the six catalog relationships. Every overlay:

- names source and target grains;
- states the safe traversal direction from many side to one side;
- explains zero-child parent rows where present;
- links both source concepts and any relevant conformed dimension;
- warns that declaration does not replace physical audit.

The component inspection relationship notes that quarantined lots can have zero consumption rows. The production-order relationship notes that a production order can have zero serialized drives.

- [ ] **Step 6: Add selected dimension overlays**

Create the seven paths listed in `_REQUIRED_SELECTED`. Required content:

- drive serial is conformed through the serialized-drive registry;
- operation line and machine belong to operation executions;
- telemetry line and machine belong to telemetry events;
- operation and telemetry identities are not joined;
- requested ship date is order intent, not actual shipment time;
- telemetry recorded time is a sample timestamp, not operation time.

- [ ] **Step 7: Add selected fact and measure overlays**

Create the nine fact and measure paths listed in `_REQUIRED_SELECTED`. Required content:

- first-attempt and first-pass markers count distinct drives;
- telemetry marker facts are non-null row markers, not conformed machine facts;
- alarm marker is null outside alarm state;
- component count counts fitted rows;
- telemetry and alarm measures count events;
- first-attempt and first-pass measures are distinct unit counts.

- [ ] **Step 8: Implement policy validation**

```python
@dataclass(frozen=True, slots=True, order=True)
class ShopfloorKnowledgeIssue:
    code: str
    path: str
    message: str


def validate_shopfloor_knowledge(
    bundle: OkfBundle,
    layer: SemanticLayer,
) -> tuple[ShopfloorKnowledgeIssue, ...]:
    issues: list[ShopfloorKnowledgeIssue] = []
    _check_required_kind_sections(bundle, layer, issues)
    _check_required_selected_concepts(bundle, issues)
    _check_metric_queries(bundle, layer, issues)
    return tuple(sorted(issues))
```

`_check_metric_queries()` requires one query request per metric overlay and calls `plan_query()` directly. A documented planner rejection is a policy failure because metric Examples must show valid queries.

- [ ] **Step 9: Run policy and composition tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "knowledge_policy or structural_overlay" -q
uv run ruff check examples/shopfloor/knowledge_policy.py
uv run pyright examples/shopfloor/knowledge_policy.py
```

Expected: every command passes.

- [ ] **Step 10: Commit**

```bash
git add \
  examples/shopfloor/knowledge_policy.py \
  examples/shopfloor/okf_overlays/sources \
  examples/shopfloor/okf_overlays/relationships \
  examples/shopfloor/okf_overlays/dimensions \
  examples/shopfloor/okf_overlays/facts \
  examples/shopfloor/okf_overlays/measures \
  tests/integration/test_shopfloor.py
git commit -m "docs(shopfloor): curate structural knowledge"
```

### Task 8: Build and publish enriched knowledge on demand

**Files:**
- Create: `examples/shopfloor/build_knowledge.py`
- Modify: `.gitignore`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int` and default generated output at `examples/shopfloor/.generated/knowledge/`.
- Consumes: `OkfBundle.build()` and `validate_shopfloor_knowledge()`.

- [ ] **Step 1: Write a failing successful-build test**

```python
from examples.shopfloor.build_knowledge import main as build_knowledge_main


def test_build_knowledge_publishes_complete_bundle(tmp_path: Path) -> None:
    output = tmp_path / "knowledge"
    assert build_knowledge_main(["--output-dir", str(output)]) == 0
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    bundle = OkfBundle.load(output, layer=layer, strict=True)
    assert validate_shopfloor_knowledge(bundle, layer) == ()
    assert len([
        concept for concept in bundle.concepts.values()
        if "selayer_id" in concept.frontmatter
    ]) == len(layer.semantic_objects())
    assert len([
        concept for concept in bundle.concepts.values()
        if concept.frontmatter.get("type") == "Reference"
    ]) == 4
```

- [ ] **Step 2: Write failing publication-safety tests**

Cover:

- existing non-empty output rejected without changes;
- malformed overlay exits `1` and publishes no output;
- policy failure exits `1` and publishes no output;
- simulated final rename failure cleans candidate output;
- no staging directories remain after success or failure;
- default output resolves to `.generated/knowledge` under the example;
- generated output is ignored by Git.

- [ ] **Step 3: Run tests and confirm the builder is missing**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "build_knowledge" -q
```

Expected: collection fails because `build_knowledge.py` does not exist.

- [ ] **Step 4: Implement the command parser**

```python
EXAMPLE_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = EXAMPLE_ROOT / ".generated" / "knowledge"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the enriched shopfloor OKF bundle."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser
```

The command accepts no network, model, prompt, or raw-SQL options.

- [ ] **Step 5: Compose into an outer candidate directory**

```python
def build_knowledge(output_dir: Path) -> OkfBundle:
    layer = SemanticLayer.load(EXAMPLE_ROOT / "shopfloor_semantic_layer.yaml")
    destination = output_dir.resolve()
    _require_absent_or_empty(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}.candidate-",
        dir=destination.parent,
    ) as directory:
        candidate = Path(directory) / "knowledge"
        bundle = OkfBundle.build(
            layer,
            candidate,
            references_dir=EXAMPLE_ROOT / "business_context",
            overlays_dir=EXAMPLE_ROOT / "okf_overlays",
        )
        issues = validate_shopfloor_knowledge(bundle, layer)
        if issues:
            raise ShopfloorKnowledgeBuildError(issues)
        if destination.exists():
            destination.rmdir()
        candidate.replace(destination)
    return OkfBundle.load(destination, layer=layer, strict=True)
```

`_require_absent_or_empty()` rejects files, symbolic links, and non-empty directories. `ShopfloorKnowledgeBuildError` stores only immutable policy issues.

- [ ] **Step 6: Implement deterministic JSON output and exit codes**

```python
def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bundle = build_knowledge(arguments.output_dir)
    except (OSError, ValueError, ShopfloorKnowledgeBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "command": "build-shopfloor-knowledge",
        "concepts": len(bundle.concepts),
        "destination": str(arguments.output_dir),
        "diagnostics": [],
    }, sort_keys=True))
    return 0
```

Ensure domain error rendering lists codes and paths but not document bodies or source values.

- [ ] **Step 7: Ignore generated output**

Add:

```gitignore
examples/shopfloor/.generated/
```

Do not ignore `business_context/` or `okf_overlays/`.

- [ ] **Step 8: Run builder tests and command**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "build_knowledge" -q
rm -rf examples/shopfloor/.generated
uv run python examples/shopfloor/build_knowledge.py
uv run selayer okf validate examples/shopfloor/.generated/knowledge \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
git status --short -- examples/shopfloor/.generated
```

Expected: tests pass; build and strict validation exit `0`; Git prints no generated paths.

- [ ] **Step 9: Commit**

```bash
git add \
  examples/shopfloor/build_knowledge.py \
  .gitignore \
  tests/integration/test_shopfloor.py
git commit -m "feat(shopfloor): build enriched knowledge on demand"
```

### Task 9: Update the tutorial and end-to-end contract

**Files:**
- Modify: `examples/shopfloor/README.md`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**
- Produces: documented commands and a clean-checkout acceptance test.
- Consumes: all prior tasks.

- [ ] **Step 1: Write failing README marker tests**

```python
def test_shopfloor_readme_documents_hardened_workflow() -> None:
    text = (SHOPFLOOR_ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "temporary directory",
        "operation_machine_id",
        "telemetry_machine_id",
        "requested_ship_date",
        "telemetry_recorded_at",
        "business_context/",
        "okf_overlays/",
        "build_knowledge.py",
        "selayer catalog audit",
        ".generated/knowledge",
        "Catalog YAML is execution authority",
        "OKF is advisory",
    )
    assert all(marker in text for marker in required)
```

- [ ] **Step 2: Write a failing clean-checkout workflow test**

In a temporary directory:

1. generate source data;
2. load and rebase the layer;
3. run static, physical, and compatibility verification;
4. query all twelve metrics;
5. append the temporary EOL retest and reload;
6. build knowledge;
7. validate shopfloor policy;
8. retrieve context for `metric.first_pass_yield` with linked concepts.

Assert no step reads or writes `examples/shopfloor/data/` or `examples/shopfloor/.generated/`.

- [ ] **Step 3: Run tests and confirm documentation is stale**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -k "readme or clean_checkout" -q
```

Expected: README marker test fails before documentation updates.

- [ ] **Step 4: Rewrite setup and run instructions**

Document:

```bash
uv sync --extra delta
uv run python examples/shopfloor/run_example.py
uv run selayer catalog validate examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer catalog compatibility examples/shopfloor/shopfloor_semantic_layer.yaml
uv run python examples/shopfloor/generate_data.py \
  --output-dir examples/shopfloor/data
uv run selayer catalog audit examples/shopfloor/shopfloor_semantic_layer.yaml
uv run python examples/shopfloor/build_knowledge.py
uv run selayer okf validate examples/shopfloor/.generated/knowledge \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
```

Explain that the runner generates temporary data while the standalone generator requires an explicit output directory.

- [ ] **Step 5: Document the corrected semantic model**

Update source and grain tables. Explain:

- serialized drives own conformed drive identity;
- operation and telemetry machine dimensions are domain-specific;
- no operation-to-telemetry relationship exists;
- requested ship date and telemetry sample time are the only modeled times;
- matching string values do not establish a safe join.

- [ ] **Step 6: Document baseline and retest states**

State exact values:

```text
Baseline: 3 EOL attempts, attempt pass rate 2/3, first-pass yield 2/3.
Temporary retest: 4 EOL attempts, attempt pass rate 3/4, first-pass yield 2/3.
```

Explain that reload mutates only temporary Delta data.

- [ ] **Step 7: Document authored and generated knowledge ownership**

State:

- `business_context/` and `okf_overlays/` are reviewed source files;
- `.generated/knowledge/` is disposable output;
- generated fields and `Catalog Definition` come only from the catalog;
- overlays can add only curated sections and approved provenance;
- Catalog YAML is execution authority;
- OKF is advisory.

- [ ] **Step 8: Run README and clean workflow tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py -q
```

Expected: every shopfloor integration test passes.

- [ ] **Step 9: Commit**

```bash
git add examples/shopfloor/README.md tests/integration/test_shopfloor.py
git commit -m "docs(shopfloor): document verified reference workflow"
```

### Task 10: Run the completion audit

**Files:**
- Verify only. Modify files only if a check exposes a defect, using a new failing test before the fix.

**Interfaces:**
- Consumes: the complete verification and shopfloor implementations.
- Produces: evidence that every approved requirement is met.

- [ ] **Step 1: Reset generated outputs and run the example**

Run:

```bash
rm -rf examples/shopfloor/.generated
uv run python examples/shopfloor/run_example.py
```

Expected: exit `0`; baseline and temporary reload sections print; no generated knowledge or repository data is required.

- [ ] **Step 2: Run static, compatibility, and exact physical verification**

Run:

```bash
uv run selayer catalog validate examples/shopfloor/shopfloor_semantic_layer.yaml
uv run selayer catalog compatibility examples/shopfloor/shopfloor_semantic_layer.yaml
uv run python examples/shopfloor/generate_data.py \
  --output-dir examples/shopfloor/data
uv run selayer catalog audit examples/shopfloor/shopfloor_semantic_layer.yaml
```

Expected: all commands exit `0`; compatibility contains documented planner rejections as `compatible: false` observations; the CLI audit reads a freshly reset baseline.

Also run the programmatic temporary-data audit through the integration test:

```bash
uv run pytest \
  tests/integration/test_shopfloor.py::test_shopfloor_physical_audit_passes -q
```

Expected: one test passes and every physical outcome has `scope: full_scan`.

- [ ] **Step 3: Build and validate knowledge from authored inputs**

Run:

```bash
uv run python examples/shopfloor/build_knowledge.py
uv run selayer okf validate examples/shopfloor/.generated/knowledge \
  --catalog examples/shopfloor/shopfloor_semantic_layer.yaml
```

Expected: both commands exit `0`; generated integrity and links pass.

- [ ] **Step 4: Verify generated output stays untracked**

Run:

```bash
git status --short -- \
  examples/shopfloor/.generated \
  examples/shopfloor/business_context \
  examples/shopfloor/okf_overlays
```

Expected: `.generated` does not appear. Business context and overlays are tracked and clean.

- [ ] **Step 5: Run all shopfloor and OKF tests**

Run:

```bash
uv run pytest tests/integration/test_shopfloor.py tests/okf -q
```

Expected: all tests pass.

- [ ] **Step 6: Run the full project test and quality suite**

Run:

```bash
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests examples
uv build
```

Expected: every command exits `0`.

- [ ] **Step 7: Review the final diff against authority boundaries**

Run:

```bash
git diff --check
git status --short
git diff -- \
  examples/shopfloor \
  tests/integration/test_shopfloor.py \
  .gitignore
```

Confirm:

- no generated OKF files are staged;
- no source data files are staged;
- no overlay changes generated frontmatter or `Catalog Definition`;
- no operation-to-telemetry relationship exists;
- no agent, model, network, prompt, or document-execution code entered `src/selayer` or the example.

- [ ] **Step 8: Commit any final documentation-only correction**

Only if Step 7 finds a documentation mismatch, add a focused commit:

```bash
git add examples/shopfloor/README.md
git commit -m "docs(shopfloor): correct reference workflow details"
```

If Step 7 finds no mismatch, do not create an empty commit.

## Completion mapping

Before declaring implementation complete, map each design requirement to evidence:

- Deterministic explicit generator: Task 1 tests.
- Temporary immutable walkthrough: Task 2 tests and runner output.
- Conformed drive and domain-specific machine identities: Task 3 catalog and planner tests.
- Supported time dimensions only: Task 3 tests.
- Exact grains, relationship cardinality, and RI: Task 4 `PhysicalCheck` report.
- Business and domain rules: Task 4 direct fixture tests.
- Four reviewed Reference concepts: Task 5 strict composed-bundle test.
- Complete metric overlays: Task 6 coverage test.
- Source, relationship, and selected-concept overlays: Task 7 policy test.
- Declarative examples that plan: Task 7 query-block tests.
- Atomic on-demand knowledge build: Task 8 failure and publication tests.
- Generated output ignored: Task 8 and Task 10 Git checks.
- Updated tutorial and clean-checkout flow: Task 9 tests.
- Full repository health: Task 10 commands.

Do not mark the example complete if the runner changes repository data, any physical check is skipped or unavailable, an overlay policy issue remains, generated output is staged, or the example depends on unimplemented local substitutes for verification library behavior.
