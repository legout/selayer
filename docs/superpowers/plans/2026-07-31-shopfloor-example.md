# Shop-floor Multi-source Example Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-contained, deterministic industrial motor-drive shop-floor example that teaches grain-aware planning across CSV, SQLite, DuckDB, Parquet, and Delta sources.

**Architecture:** The example owns a small deterministic data generator, a static schema-version-1 catalog, and a runner. The generator emits local connector inputs; the catalog encodes the safe many-to-one paths from production facts to orders and serialized drives; the runner composes valid queries, demonstrates an intentional mixed-grain failure, and reloads a Delta EOL source after a retest append. Tests materialize the same data in `tmp_path`, rewrite only catalog locations/schema references, and independently assert the documented outputs.

**Tech Stack:** Python 3.13, PyArrow/Parquet, DuckDB, SQLite standard library, `deltalake` optional extra, Polars result frames, PyYAML, pytest, ruff, pyright.

## Global Constraints

- Use only the existing source types: `csv`, `sqlite`, `duckdb`, `parquet`, and `delta`; do not add JSON, DuckLake, new connectors, profiles, cloud storage, Docker, or runtime services.
- Keep all source data local, synthetic, fixed-seed or literal, and deterministic; generated output belongs in `examples/shopfloor/data/` and must be ignored by Git.
- Give every catalog source a non-empty, physically valid grain and write every Parquet/Delta fixture with an explicit PyArrow schema matching its YAML declaration exactly.
- Keep telemetry at its independent `machine_id × recorded_at` grain. Do not introduce time-window joins, automatic allocation, automatic re-graining, or an unsafe relationship to unit-level operation data.
- Require the existing `delta` extra for this example and surface the exact remediation command `uv sync --extra delta` when it is absent.
- The runner catches only the intentional `QueryPlanningError` with code `mixed_grain` and the custom missing-Delta setup error. All other errors propagate.
- The example must run from the repository root with `uv run python examples/shopfloor/run_example.py` after the Delta extra is installed.
- Tests must use `tmp_path`; they must not write into `examples/shopfloor/data/`, require Docker, or contact external services.
- Do not modify `src/selayer/`; this work exercises existing public behavior only.

---

## File Structure

| Path | Responsibility |
|---|---|
| `examples/shopfloor/generate_data.py` | Deterministically create all five physical source formats, expose data paths, append the known EOL retest, and expose a precise missing-Delta error. |
| `examples/shopfloor/shopfloor_semantic_layer.yaml` | Executable schema-version-1 model: eight sources, dimensions, facts, measures, metrics, and safe relationships. |
| `examples/shopfloor/schemas/*.yaml` | Per-source physical schemas used by catalog validation. |
| `examples/shopfloor/run_example.py` | Reset default data, execute the named walkthrough, print results, catch the expected mixed-grain error, then reload Delta EOL data. |
| `examples/shopfloor/README.md` | Explain the manufacturing narrative, connector and grain map, setup, commands, queries, reload, and non-goals. |
| `tests/integration/test_shopfloor.py` | Create temporary source data, construct a temporary catalog, verify all documented queries, verify the planner boundary, verify Delta reload, and verify docs commands. |
| `.gitignore` | Ignore `examples/shopfloor/data/`. |
| `README.md` | Add a concise link to the shop-floor walkthrough next to the current e-commerce example. |

## Deterministic fixture contract

Use the following literal business data. It gives the tests fixed expected values and ensures every documented scenario exists.

| Entity | Required records |
|---|---|
| Customer orders | `CO-1001` / North / X200; `CO-1002` / Europe / X300. |
| Production orders | `PO-2001`: CO-1001, planned 2, completed 2, on-time. `PO-2002`: CO-1002, planned 2, completed 1, late. `PO-2003`: CO-1002, planned 1, completed 0, open. Total planned/completed = 5/3. |
| Serialized drives | `DRV-001`, `DRV-002` from PO-2001: X200, BOM-A, FW-1.0, shipped. `DRV-003` from PO-2002: X300, BOM-B, FW-2.1, complete but unshipped. |
| Component consumption | Two fitted positions (`power_module`, `control_pcb`) per drive; DRV-003 uses `LOT-P-02` and `LOT-C-02`. |
| Component-lot inspections | Four released lots (`LOT-P-01`, `LOT-C-01`, `LOT-P-02`, `LOT-C-02`) and one quarantined lot (`LOT-C-03`): acceptance rate = 4/5. The quarantined lot is not consumed. |
| Operation executions | Seven rows with cycle seconds `[60, 40, 65, 45, 55, 80, 50]`; exactly one `is_rework=True`; operation count = 7 and rework rate = 1/7. |
| Telemetry | Four machine events; one `machine_state=alarm`; temperatures `[44, 82, 38, 30]`; alarm count = 1 and average temperature = 48.5. |
| Initial EOL attempts | DRV-001 pass attempt 1 and `is_first_pass=true`, DRV-002 pass attempt 1 and `is_first_pass=true`, DRV-003 fail attempt 1 and `is_first_pass=false`: all-attempt pass rate = 2/3 and first-pass yield = 2/3. |
| Appended EOL retest | DRV-003 pass attempt 2 and `is_first_pass=false`: all-attempt pass rate becomes 3/4 and first-pass yield remains 2/3. |

### Task 1: Generate deterministic connector inputs

**Files:**

- Create: `examples/shopfloor/generate_data.py`
- Create: `tests/integration/test_shopfloor.py`
- Modify: `.gitignore`

**Interfaces:**

- Consumes: Python standard library, `duckdb`, `pyarrow`, `pyarrow.parquet`, and the installed `deltalake` package.
- Produces: `ShopfloorDataPaths`, `DeltaDependencyError`, and `generate_shopfloor_data(output_dir: Path) -> ShopfloorDataPaths`.
- Later tasks use: every `ShopfloorDataPaths` member, deterministic row counts/values, and the `DeltaDependencyError` contract.

- [ ] **Step 1: Write the failing physical-data test**

  Add imports for `csv`, `sqlite3`, `duckdb`, `pyarrow.parquet as pq`, `pytest`, and `DeltaTable`, then add this test to `tests/integration/test_shopfloor.py`:

  ```python
  from examples.shopfloor.generate_data import generate_shopfloor_data


  def test_generate_shopfloor_data_writes_all_connector_inputs(tmp_path: Path) -> None:
      paths = generate_shopfloor_data(tmp_path / "data")

      with paths.customer_orders.open(newline="", encoding="utf-8") as stream:
          assert len(list(csv.DictReader(stream))) == 2
      with sqlite3.connect(paths.production_orders_db) as connection:
          assert connection.execute("select count(*) from production_orders").fetchone() == (3,)
      with duckdb.connect(str(paths.shopfloor_db), read_only=True) as connection:
          assert connection.execute("select count(*) from serialized_drives").fetchone() == (3,)

      assert pq.read_table(paths.component_consumption).num_rows == 6
      assert pq.read_table(paths.component_lot_inspections).num_rows == 5
      assert pq.read_table(paths.operation_executions).num_rows == 7
      assert pq.read_table(paths.machine_telemetry).num_rows == 4
      assert DeltaTable(paths.eol_test_runs).to_pyarrow_table().num_rows == 3
  ```

- [ ] **Step 2: Run the new test to verify the red state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_generate_shopfloor_data_writes_all_connector_inputs -q
  ```

  Expected: collection fails because `examples.shopfloor.generate_data` does not exist.

- [ ] **Step 3: Create the deterministic data generator**

  Create `examples/shopfloor/generate_data.py` with this public interface and keep its data literals equal to the deterministic fixture contract above:

  ```python
  from __future__ import annotations

  import csv
  import shutil
  import sqlite3
  from dataclasses import dataclass
  from datetime import datetime
  from pathlib import Path

  import duckdb
  import pyarrow as pa
  import pyarrow.parquet as pq


  class DeltaDependencyError(RuntimeError):
      """Raised when the optional Delta dependency is absent."""


  @dataclass(frozen=True, slots=True)
  class ShopfloorDataPaths:
      root: Path
      customer_orders: Path
      production_orders_db: Path
      shopfloor_db: Path
      component_consumption: Path
      component_lot_inspections: Path
      operation_executions: Path
      machine_telemetry: Path
      eol_test_runs: Path


  def generate_shopfloor_data(output_dir: Path) -> ShopfloorDataPaths:
      """Reset ``output_dir`` and write the deterministic local shop-floor data."""
      shutil.rmtree(output_dir, ignore_errors=True)
      output_dir.mkdir(parents=True)
      paths = ShopfloorDataPaths(
          root=output_dir,
          customer_orders=output_dir / "customer_orders.csv",
          production_orders_db=output_dir / "production_orders.sqlite",
          shopfloor_db=output_dir / "shopfloor.duckdb",
          component_consumption=output_dir / "component_consumption.parquet",
          component_lot_inspections=output_dir / "component_lot_inspections.parquet",
          operation_executions=output_dir / "operation_executions.parquet",
          machine_telemetry=output_dir / "machine_telemetry.parquet",
          eol_test_runs=output_dir / "eol_test_runs.delta",
      )
      _write_customer_orders(paths.customer_orders)
      _write_production_orders(paths.production_orders_db)
      _write_serialized_drives(paths.shopfloor_db)
      _write_parquet_sources(paths)
      _write_eol_test_runs(paths.eol_test_runs)
      return paths

  ```

  Implement `_delta_writer()` so it imports `write_deltalake` lazily and raises exactly:

  ```python
  DeltaDependencyError(
      "Delta support is required for the shop-floor example; run: uv sync --extra delta"
  )
  ```

  Write CSV through `csv.DictWriter`; create the SQLite and DuckDB tables with explicit SQL column types and `executemany`; write every Parquet file with `pq.write_table(pa.Table.from_pylist(rows, schema=...))`; and use the same explicit `_EOL_SCHEMA` with `write_deltalake`. Use `pa.timestamp("ns")` for the telemetry `recorded_at` values. Do not use Pandas, Faker, current timestamps, random values, or schema inference.

  Use these exact source field sets:

  | Output | Fields |
  |---|---|
  | CSV `customer_orders` | `customer_order_id`, `customer_name`, `customer_region`, `requested_ship_date`, `product_model`, `order_status` |
  | SQLite `production_orders` | `production_order_id`, `customer_order_id`, `product_model`, `routing`, `planned_units`, `completed_units`, `schedule_status` |
  | DuckDB `serialized_drives` | `serial_number`, `production_order_id`, `product_model`, `bom_revision`, `firmware_revision`, `completion_status`, `shipment_status` |
  | Parquet `component_consumption` | `serial_number`, `fitted_position`, `component_part_number`, `component_serial_number`, `component_lot_id` |
  | Parquet `component_lot_inspections` | `component_lot_id`, `supplier_name`, `component_type`, `incoming_result`, `disposition` |
  | Parquet `operation_executions` | `operation_execution_id`, `serial_number`, `operation_name`, `line_id`, `machine_id`, `shift`, `cycle_seconds`, `energy_kwh`, `max_torque_nm`, `max_temperature_c`, `result`, `is_rework` |
  | Parquet `machine_telemetry` | `machine_id`, `recorded_at`, `line_id`, `machine_state`, `temperature_c`, `power_kw` |
  | Delta `eol_test_runs` | `eol_test_run_id`, `serial_number`, `station_id`, `attempt`, `result`, `is_first_pass`, `input_voltage_v`, `output_voltage_v`, `power_w` |

- [ ] **Step 4: Ignore generated example data**

  Add this exact line to `.gitignore` under the generated-output section:

  ```gitignore
  examples/shopfloor/data/
  ```

- [ ] **Step 5: Run the physical-data test to verify the green state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_generate_shopfloor_data_writes_all_connector_inputs -q
  ```

  Expected: `1 passed`.

- [ ] **Step 6: Commit the independently usable data foundation**

  ```bash
  git add .gitignore examples/shopfloor/generate_data.py tests/integration/test_shopfloor.py
  git commit -m "feat(examples): generate shop-floor source data"
  ```

### Task 2: Define schemas, catalog, and grain-safe queries

**Files:**

- Create: `examples/shopfloor/shopfloor_semantic_layer.yaml`
- Create: `examples/shopfloor/schemas/customer_orders.yaml`
- Create: `examples/shopfloor/schemas/production_orders.yaml`
- Create: `examples/shopfloor/schemas/serialized_drives.yaml`
- Create: `examples/shopfloor/schemas/component_consumption.yaml`
- Create: `examples/shopfloor/schemas/component_lot_inspections.yaml`
- Create: `examples/shopfloor/schemas/operation_executions.yaml`
- Create: `examples/shopfloor/schemas/machine_telemetry.yaml`
- Create: `examples/shopfloor/schemas/eol_test_runs.yaml`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**

- Consumes: `ShopfloorDataPaths` from Task 1 and the existing `SemanticLayer`/`QueryEngine` public API.
- Produces: a schema-version-1 catalog whose exact metric names are listed below, plus `_temporary_shopfloor_catalog(tmp_path, paths) -> Path` in the test module.
- Later tasks use: the static catalog at `examples/shopfloor/shopfloor_semantic_layer.yaml`, all metric names, and the temporary-catalog fixture.

- [ ] **Step 1: Write failing catalog/query tests**

  Extend `tests/integration/test_shopfloor.py` with a catalog helper that loads the static YAML, rewrites only its eight `location` values to `ShopfloorDataPaths` absolute paths, replaces every `schema_ref` with the loaded schema mapping from `examples/shopfloor/schemas/`, writes the result under `tmp_path`, and returns that path. This mirrors the established e-commerce integration fixture and avoids mutating repository data.

  Add the test below. The exact names are the public catalog contract for this example:

  ```python
  def test_shopfloor_catalog_answers_documented_questions(tmp_path: Path) -> None:
      paths = generate_shopfloor_data(tmp_path / "data")
      layer = SemanticLayer.load(_temporary_shopfloor_catalog(tmp_path, paths))

      with QueryEngine(layer) as engine:
          assert engine.query(["production_completion_rate"])[
              "production_completion_rate"
          ].item() == pytest.approx(3 / 5)
          assert engine.query(["shipped_unit_count"])["shipped_unit_count"].item() == 2
          assert engine.query(["incoming_acceptance_rate"])[
              "incoming_acceptance_rate"
          ].item() == pytest.approx(4 / 5)
          assert engine.query(["operation_count", "rework_rate"]).row(0) == pytest.approx(
              (7, 1 / 7)
          )
          assert engine.query(["eol_attempt_pass_rate", "first_pass_yield"]).row(0) == pytest.approx(
              (2 / 3, 2 / 3)
          )
          assert engine.query(["alarm_event_count", "average_temperature_c"]).row(0) == pytest.approx(
              (1, 48.5)
          )

          trace = engine.query(
              ["component_count"],
              ["component_lot_id"],
              {"drive_serial_number": "DRV-003"},
          ).sort("component_lot_id")
          assert trace["component_lot_id"].to_list() == ["LOT-C-02", "LOT-P-02"]
          assert trace["component_count"].to_list() == [1, 1]

          with pytest.raises(QueryPlanningError) as caught:
              engine.plan(["average_cycle_seconds", "eol_attempt_pass_rate"])

      assert caught.value.code == "mixed_grain"
  ```

- [ ] **Step 2: Run the query test to verify the red state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_shopfloor_catalog_answers_documented_questions -q
  ```

  Expected: failure because the shop-floor catalog and schemas do not exist.

- [ ] **Step 3: Create the eight physical schema documents**

  Create one YAML schema per source using the field sets in Task 1. Declare identifiers and classifications as `utf8`, counts/attempts as `int64`, physical measurements as `float64`, `is_rework` and `is_first_pass` as `boolean`, and `recorded_at` as:

  ```yaml
  - name: recorded_at
    type:
      timestamp: {unit: ns}
    nullable: false
  ```

  Set every generated field `nullable: false`; the deterministic rows contain no missing values. This ensures the physical PyArrow schemas in Task 1 and the catalog schemas match exactly.

- [ ] **Step 4: Create the executable catalog**

  Create `examples/shopfloor/shopfloor_semantic_layer.yaml` with `version: 1`, `name: shopfloor`, `label: Shop-floor Motor Drive Analytics`, and the eight data sources below. Use the exact relative locations so the repository-root runner command resolves each file:

  ```yaml
  data_sources:
    customer_orders:
      type: csv
      location: examples/shopfloor/data/customer_orders.csv
      schema_ref: schemas/customer_orders.yaml
      grain: [customer_order_id]
    production_orders:
      type: sqlite
      location: examples/shopfloor/data/production_orders.sqlite
      relation: production_orders
      schema_ref: schemas/production_orders.yaml
      grain: [production_order_id]
    serialized_drives:
      type: duckdb
      location: examples/shopfloor/data/shopfloor.duckdb
      relation: serialized_drives
      schema_ref: schemas/serialized_drives.yaml
      grain: [serial_number]
    component_consumption:
      type: parquet
      location: examples/shopfloor/data/component_consumption.parquet
      schema_ref: schemas/component_consumption.yaml
      grain: [serial_number, fitted_position]
    component_lot_inspections:
      type: parquet
      location: examples/shopfloor/data/component_lot_inspections.parquet
      schema_ref: schemas/component_lot_inspections.yaml
      grain: [component_lot_id]
    operation_executions:
      type: parquet
      location: examples/shopfloor/data/operation_executions.parquet
      schema_ref: schemas/operation_executions.yaml
      grain: [operation_execution_id]
    machine_telemetry:
      type: parquet
      location: examples/shopfloor/data/machine_telemetry.parquet
      schema_ref: schemas/machine_telemetry.yaml
      grain: [machine_id, recorded_at]
    eol_test_runs:
      type: delta
      location: examples/shopfloor/data/eol_test_runs.delta
      schema_ref: schemas/eol_test_runs.yaml
      grain: [eol_test_run_id]
  ```

  Add dimensions with these exact names/source columns:

  | Dimension | Source and column |
  |---|---|
  | `customer_region` | `customer_orders.customer_region` |
  | `product_model` | `production_orders.product_model` |
  | `bom_revision` | `serialized_drives.bom_revision` |
  | `firmware_revision` | `serialized_drives.firmware_revision` |
  | `schedule_status` | `production_orders.schedule_status` |
  | `routing` | `production_orders.routing` |
  | `drive_serial_number` | `component_consumption.serial_number` |
  | `component_lot_id` | `component_consumption.component_lot_id` |
  | `supplier_name` | `component_lot_inspections.supplier_name` |
  | `component_type` | `component_lot_inspections.component_type` |
  | `line_id` | `operation_executions.line_id` |
  | `machine_id` | `operation_executions.machine_id` |
  | `shift` | `operation_executions.shift` |
  | `operation_name` | `operation_executions.operation_name` |
  | `station_id` | `eol_test_runs.station_id` |
  | `machine_state` | `machine_telemetry.machine_state` |
  | `telemetry_line_id` | `machine_telemetry.line_id` |

  Add facts, measures, and metrics with the following exact definitions. `count_distinct` facts use their source identifiers; conditional identifier facts use the existing expression DSL `if(condition, identifier, null)`.

  ```yaml
  # Facts and measures; keep the names exactly as shown.
  facts:
    planned_units: {source: production_orders, expression: production_orders.planned_units, data_type: integer}
    completed_units: {source: production_orders, expression: production_orders.completed_units, data_type: integer}
    drive_serial_number: {source: serialized_drives, expression: serialized_drives.serial_number, data_type: string}
    shipped_drive_serial: {source: serialized_drives, expression: "if(serialized_drives.shipment_status = 'shipped', serialized_drives.serial_number, null)", data_type: string}
    component_serial_number: {source: component_consumption, expression: component_consumption.component_serial_number, data_type: string}
    inspected_component_lot: {source: component_lot_inspections, expression: component_lot_inspections.component_lot_id, data_type: string}
    accepted_component_lot: {source: component_lot_inspections, expression: "if(component_lot_inspections.incoming_result = 'pass', component_lot_inspections.component_lot_id, null)", data_type: string}
    operation_execution_id: {source: operation_executions, expression: operation_executions.operation_execution_id, data_type: string}
    rework_operation_execution_id: {source: operation_executions, expression: "if(operation_executions.is_rework = true, operation_executions.operation_execution_id, null)", data_type: string}
    cycle_seconds: {source: operation_executions, expression: operation_executions.cycle_seconds, data_type: decimal}
    energy_kwh: {source: operation_executions, expression: operation_executions.energy_kwh, data_type: decimal}
    eol_test_run_id: {source: eol_test_runs, expression: eol_test_runs.eol_test_run_id, data_type: string}
    passed_eol_test_run_id: {source: eol_test_runs, expression: "if(eol_test_runs.result = 'pass', eol_test_runs.eol_test_run_id, null)", data_type: string}
    first_attempt_serial: {source: eol_test_runs, expression: "if(eol_test_runs.attempt = 1, eol_test_runs.serial_number, null)", data_type: string}
    first_pass_serial: {source: eol_test_runs, expression: "if(eol_test_runs.is_first_pass = true, eol_test_runs.serial_number, null)", data_type: string}
    telemetry_machine_id: {source: machine_telemetry, expression: machine_telemetry.machine_id, data_type: string}
    alarm_machine_id: {source: machine_telemetry, expression: "if(machine_telemetry.machine_state = 'alarm', machine_telemetry.machine_id, null)", data_type: string}
    temperature_c: {source: machine_telemetry, expression: machine_telemetry.temperature_c, data_type: decimal}

  measures:
    total_planned_units: {fact: planned_units, aggregation: sum}
    total_completed_units: {fact: completed_units, aggregation: sum}
    shipped_units: {fact: shipped_drive_serial, aggregation: count_distinct}
    component_count_measure: {fact: component_serial_number, aggregation: count}
    inspected_component_lot_count: {fact: inspected_component_lot, aggregation: count_distinct}
    accepted_component_lot_count: {fact: accepted_component_lot, aggregation: count_distinct}
    operation_count_measure: {fact: operation_execution_id, aggregation: count_distinct}
    rework_operation_count: {fact: rework_operation_execution_id, aggregation: count_distinct}
    average_cycle_seconds_measure: {fact: cycle_seconds, aggregation: avg}
    total_operation_energy_kwh: {fact: energy_kwh, aggregation: sum}
    eol_attempt_count: {fact: eol_test_run_id, aggregation: count_distinct}
    passed_eol_attempt_count: {fact: passed_eol_test_run_id, aggregation: count_distinct}
    first_attempt_unit_count: {fact: first_attempt_serial, aggregation: count_distinct}
    first_pass_unit_count: {fact: first_pass_serial, aggregation: count_distinct}
    telemetry_event_count: {fact: telemetry_machine_id, aggregation: count}
    alarm_event_count_measure: {fact: alarm_machine_id, aggregation: count}
    average_temperature_c_measure: {fact: temperature_c, aggregation: avg}
  ```

  | Metric | Anchor | Measures/formula |
  |---|---|---|
  | `production_completion_rate` | `production_orders` | `total_completed_units / nullif(total_planned_units, 0)` with measures `[total_completed_units, total_planned_units]` |
  | `shipped_unit_count` | `serialized_drives` | `shipped_units` with measures `[shipped_units]` |
  | `component_count` | `component_consumption` | `component_count_measure` with measures `[component_count_measure]` |
  | `incoming_acceptance_rate` | `component_lot_inspections` | `accepted_component_lot_count / nullif(inspected_component_lot_count, 0)` with measures `[accepted_component_lot_count, inspected_component_lot_count]` |
  | `average_cycle_seconds` | `operation_executions` | `average_cycle_seconds_measure` with measures `[average_cycle_seconds_measure]` |
  | `operation_count` | `operation_executions` | `operation_count_measure` with measures `[operation_count_measure]` |
  | `rework_rate` | `operation_executions` | `rework_operation_count / nullif(operation_count_measure, 0)` with measures `[rework_operation_count, operation_count_measure]` |
  | `energy_per_operation_kwh` | `operation_executions` | `total_operation_energy_kwh / nullif(operation_count_measure, 0)` with measures `[total_operation_energy_kwh, operation_count_measure]` |
  | `eol_attempt_pass_rate` | `eol_test_runs` | `passed_eol_attempt_count / nullif(eol_attempt_count, 0)` with measures `[passed_eol_attempt_count, eol_attempt_count]` |
  | `first_pass_yield` | `eol_test_runs` | `first_pass_unit_count / nullif(first_attempt_unit_count, 0)` with measures `[first_pass_unit_count, first_attempt_unit_count]` |
  | `alarm_event_count` | `machine_telemetry` | `alarm_event_count_measure` with measures `[alarm_event_count_measure]` |
  | `average_temperature_c` | `machine_telemetry` | `average_temperature_c_measure` with measures `[average_temperature_c_measure]` |

  Add these relationships exactly:

  ```yaml
  relationships:
    customer_orders_production_orders:
      source: customer_orders
      target: production_orders
      type: one_to_many
      source_column: customer_order_id
      target_column: customer_order_id
    production_orders_serialized_drives:
      source: production_orders
      target: serialized_drives
      type: one_to_many
      source_column: production_order_id
      target_column: production_order_id
    serialized_drives_component_consumption:
      source: serialized_drives
      target: component_consumption
      type: one_to_many
      source_column: serial_number
      target_column: serial_number
    component_lot_inspections_component_consumption:
      source: component_lot_inspections
      target: component_consumption
      type: one_to_many
      source_column: component_lot_id
      target_column: component_lot_id
    serialized_drives_operation_executions:
      source: serialized_drives
      target: operation_executions
      type: one_to_many
      source_column: serial_number
      target_column: serial_number
    serialized_drives_eol_test_runs:
      source: serialized_drives
      target: eol_test_runs
      type: one_to_many
      source_column: serial_number
      target_column: serial_number
  ```

  Do not add any relationship for `machine_telemetry`.

- [ ] **Step 5: Run the catalog/query test to verify the green state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_shopfloor_catalog_answers_documented_questions -q
  ```

  Expected: `1 passed`.

- [ ] **Step 6: Commit the catalog as a testable semantic-model unit**

  ```bash
  git add examples/shopfloor/shopfloor_semantic_layer.yaml examples/shopfloor/schemas tests/integration/test_shopfloor.py
  git commit -m "feat(examples): add shop-floor semantic catalog"
  ```

### Task 3: Add the runnable walkthrough and Delta reload demonstration

**Files:**

- Create: `examples/shopfloor/run_example.py`
- Modify: `examples/shopfloor/generate_data.py`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**

- Consumes: `generate_shopfloor_data`, `DeltaDependencyError`, the static catalog, and public `QueryEngine` lifecycle methods.
- Produces: `append_eol_retest(delta_path: Path) -> None`, `run_walkthrough(engine: QueryEngine, eol_test_runs: Path) -> None`, and `main() -> None`.
- Later tasks use: the stable command and printed labels documented by the README.

- [ ] **Step 1: Write failing reload and runner tests**

  Add these tests, reusing `_temporary_shopfloor_catalog` from Task 2:

  ```python
  from examples.shopfloor.generate_data import (
      DeltaDependencyError,
      ShopfloorDataPaths,
      append_eol_retest,
  )
  from examples.shopfloor.run_example import run_walkthrough


  def test_delta_retest_reload_changes_only_attempt_rate(tmp_path: Path) -> None:
      paths = generate_shopfloor_data(tmp_path / "data")
      layer = SemanticLayer.load(_temporary_shopfloor_catalog(tmp_path, paths))

      with QueryEngine(layer) as engine:
          before_status = engine.source_status("eol_test_runs")
          before = engine.query(["eol_attempt_pass_rate", "first_pass_yield"])
          append_eol_retest(paths.eol_test_runs)
          reload = engine.reload_source("eol_test_runs")
          after = engine.query(["eol_attempt_pass_rate", "first_pass_yield"])

      assert reload.old_generation == before_status.generation
      assert reload.new_generation == before_status.generation + 1
      assert before["eol_attempt_pass_rate"].item() == pytest.approx(2 / 3)
      assert after["eol_attempt_pass_rate"].item() == pytest.approx(3 / 4)
      assert before["first_pass_yield"].item() == pytest.approx(2 / 3)
      assert after["first_pass_yield"].item() == pytest.approx(2 / 3)


  def test_walkthrough_prints_the_planner_boundary_and_reload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
      paths = generate_shopfloor_data(tmp_path / "data")
      layer = SemanticLayer.load(_temporary_shopfloor_catalog(tmp_path, paths))

      with QueryEngine(layer) as engine:
          run_walkthrough(engine, paths.eol_test_runs)

      output = capsys.readouterr().out
      assert "Production completion rate:" in output
      assert "Component genealogy for DRV-003:" in output
      assert "Expected mixed-grain rejection: mixed_grain" in output
      assert "EOL source generation:" in output
      assert "EOL pass rate after Delta reload:" in output


  def test_main_prints_delta_setup_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
      from examples.shopfloor import run_example

      def missing_delta(_: Path) -> ShopfloorDataPaths:
          raise DeltaDependencyError(
              "Delta support is required for the shop-floor example; run: uv sync --extra delta"
          )

      monkeypatch.setattr(run_example, "generate_shopfloor_data", missing_delta)
      with pytest.raises(SystemExit, match="uv sync --extra delta"):
          run_example.main()
  ```

- [ ] **Step 2: Run the reload test to verify the red state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_delta_retest_reload_changes_only_attempt_rate -q
  ```

  Expected: failure because `append_eol_retest` is not implemented or does not yet write the specified second attempt.

- [ ] **Step 3: Implement the retest append and runner as composition over public APIs**

  Add this function to `examples/shopfloor/generate_data.py`; it uses the existing lazy `_delta_writer()` and the Task 1 `_EOL_SCHEMA`:

  ```python
  def append_eol_retest(delta_path: Path) -> None:
      """Append the deterministic DRV-003 passing second EOL attempt."""
      _delta_writer()(
          delta_path,
          pa.Table.from_pylist(
              [{
                  "eol_test_run_id": "EOL-004",
                  "serial_number": "DRV-003",
                  "station_id": "EOL-1",
                  "attempt": 2,
                  "result": "pass",
                  "is_first_pass": False,
                  "input_voltage_v": 400.0,
                  "output_voltage_v": 400.2,
                  "power_w": 752.0,
              }],
              schema=_EOL_SCHEMA,
          ),
          mode="append",
      )
  ```

  Create `examples/shopfloor/run_example.py` with this shape:

  ```python
  from __future__ import annotations

  from pathlib import Path

  from selayer import QueryEngine, QueryPlanningError, SemanticLayer

  from examples.shopfloor.generate_data import (
      DeltaDependencyError,
      append_eol_retest,
      generate_shopfloor_data,
  )

  ROOT = Path(__file__).resolve().parents[2]
  EXAMPLE_DIR = ROOT / "examples" / "shopfloor"
  CATALOG = EXAMPLE_DIR / "shopfloor_semantic_layer.yaml"
  DATA_DIR = EXAMPLE_DIR / "data"


  def run_walkthrough(engine: QueryEngine, eol_test_runs: Path) -> None:
      print("Production completion rate:")
      print(engine.query(["production_completion_rate"], ["schedule_status"]))
      print("Customer fulfilment:")
      print(engine.query(["shipped_unit_count"], ["customer_region", "product_model"]))
      print("Component genealogy for DRV-003:")
      print(engine.query(["component_count"], ["component_lot_id"], {"drive_serial_number": "DRV-003"}))
      print("Incoming component quality:")
      print(engine.query(["incoming_acceptance_rate"], ["supplier_name", "component_type"]))
      print("Operation performance:")
      print(engine.query(["average_cycle_seconds", "rework_rate", "energy_per_operation_kwh"], ["line_id", "machine_id", "shift", "operation_name"]))
      print("EOL quality before Delta reload:")
      print(engine.query(["eol_attempt_pass_rate", "first_pass_yield"], ["station_id", "product_model", "firmware_revision"]))
      print("Raw machine health:")
      print(engine.query(["alarm_event_count", "average_temperature_c"], ["telemetry_line_id", "machine_state"]))

      try:
          engine.plan(["average_cycle_seconds", "eol_attempt_pass_rate"])
      except QueryPlanningError as error:
          print(f"Expected mixed-grain rejection: {error.code}")

      before = engine.source_status("eol_test_runs")
      append_eol_retest(eol_test_runs)
      change = engine.reload_source("eol_test_runs")
      print(f"EOL source generation: {before.generation} -> {change.new_generation}")
      print("EOL pass rate after Delta reload:")
      print(engine.query(["eol_attempt_pass_rate", "first_pass_yield"]))


  def main() -> None:
      try:
          paths = generate_shopfloor_data(DATA_DIR)
      except DeltaDependencyError as error:
          raise SystemExit(str(error)) from error
      layer = SemanticLayer.load(CATALOG)
      with QueryEngine(layer) as engine:
          run_walkthrough(engine, paths.eol_test_runs)


  if __name__ == "__main__":
      main()
  ```

  Keep the missing-Delta error boundary in `main`; do not catch catalog, query, or I/O errors. The generator reset must happen before catalog loading so every run begins from the three-attempt EOL state and appends exactly one retest.

- [ ] **Step 4: Run focused runner and reload tests to verify the green state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_delta_retest_reload_changes_only_attempt_rate tests/integration/test_shopfloor.py::test_walkthrough_prints_the_planner_boundary_and_reload tests/integration/test_shopfloor.py::test_main_prints_delta_setup_instruction -q
  ```

  Expected: `3 passed`.

- [ ] **Step 5: Execute the documented command manually**

  Run:

  ```bash
  uv run python examples/shopfloor/run_example.py
  ```

  Expected: output contains all seven named walkthrough sections, `Expected mixed-grain rejection: mixed_grain`, `EOL source generation: 1 -> 2` or the initial generation reported by the installed Delta runtime plus one, and an EOL attempt pass rate changing from 2/3 to 3/4 while first-pass yield stays 2/3.

- [ ] **Step 6: Commit the runnable walkthrough**

  ```bash
  git add examples/shopfloor/generate_data.py examples/shopfloor/run_example.py tests/integration/test_shopfloor.py
  git commit -m "feat(examples): add shop-floor walkthrough"
  ```

### Task 4: Document the shop-floor tutorial and validate the repository surface

**Files:**

- Create: `examples/shopfloor/README.md`
- Modify: `README.md`
- Modify: `tests/integration/test_shopfloor.py`

**Interfaces:**

- Consumes: the finished source topology, exact runner command, metric names, and output labels from Tasks 1–3.
- Produces: user-facing instructions that match the runnable example and a documentation assertion that prevents command drift.

- [ ] **Step 1: Write the failing documentation contract test**

  Add this test to `tests/integration/test_shopfloor.py`:

  ```python
  def test_shopfloor_docs_match_the_runnable_contract() -> None:
      repo = Path(__file__).parents[2]
      shopfloor_readme = (repo / "examples/shopfloor/README.md").read_text(encoding="utf-8")
      root_readme = (repo / "README.md").read_text(encoding="utf-8")

      assert "uv sync --extra delta" in shopfloor_readme
      assert "uv run python examples/shopfloor/run_example.py" in shopfloor_readme
      assert "mixed_grain" in shopfloor_readme
      assert "JSON" in shopfloor_readme
      assert "DuckLake" in shopfloor_readme
      assert "examples/shopfloor/README.md" in root_readme
  ```

- [ ] **Step 2: Run the documentation test to verify the red state**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py::test_shopfloor_docs_match_the_runnable_contract -q
  ```

  Expected: failure because the shop-floor README and root README link do not exist.

- [ ] **Step 3: Write the example README and link it from the root README**

  Create `examples/shopfloor/README.md` with these sections and exact commands:

  1. **Motor-drive story** — customer order, internal production order, serialized drive, component-lot inspection/consumption, operations, telemetry, and EOL testing.
  2. **Setup and run** — exactly:

     ```bash
     uv sync --extra delta
     uv run python examples/shopfloor/run_example.py
     ```

  3. **Source and grain map** — list all eight sources, their physical formats, and their grains; call out that telemetry is a separate machine × timestamp stream.
  4. **Walkthrough** — explain each printed query section, the `mixed_grain` boundary, the appended DRV-003 second EOL attempt, and `reload_source("eol_test_runs")`.
  5. **Non-goals** — JSON and DuckLake are not catalog connector types; the example adds no automatic re-graining, time-window telemetry joins, cloud storage, credentials, remote services, or MES behavior.

  In the root `README.md`, retain the e-commerce command and add this concise adjacent link:

  ```markdown
  For a multi-source manufacturing walkthrough with CSV, SQLite, DuckDB,
  Parquet, and Delta, see
  [`examples/shopfloor/README.md`](examples/shopfloor/README.md).
  ```

- [ ] **Step 4: Run documentation and focused integration verification**

  Run:

  ```bash
  uv run pytest tests/integration/test_shopfloor.py -q
  uv run ruff check examples/shopfloor tests/integration/test_shopfloor.py
  uv run pyright examples/shopfloor tests/integration/test_shopfloor.py
  ```

  Expected: all commands exit 0.

- [ ] **Step 5: Run the non-service regression suite**

  Run:

  ```bash
  uv run pytest -q -m "not integration"
  uv run pytest tests/integration/test_ecommerce.py tests/integration/test_shopfloor.py -q
  ```

  Expected: all selected tests pass without Docker or remote services.

- [ ] **Step 6: Commit documentation and final verification coverage**

  ```bash
  git add README.md examples/shopfloor/README.md tests/integration/test_shopfloor.py
  git commit -m "docs(examples): document shop-floor tutorial"
  ```

## Plan self-review

### Spec coverage

| Approved specification requirement | Plan task |
|---|---|
| One self-contained motor-drive example | Tasks 1–4 |
| CSV, SQLite, DuckDB, Parquet, and Delta sources | Tasks 1–2 |
| Eight named source grains and safe relationships | Task 2 |
| Orders, internal orders, serialized units, component inspection/genealogy, operations, telemetry, and EOL | Tasks 1–2 |
| Seven valid business questions | Task 2 catalog contract and Task 3 runner |
| Explicit mixed-grain rejection | Tasks 2–3 |
| Deterministic Delta retest and source reload | Tasks 1 and 3 |
| Missing-Delta setup instruction | Tasks 1 and 3 |
| README and root README link | Task 4 |
| Temporary-data integration tests with no Docker | Tasks 1–4 |
| Unsupported JSON/DuckLake made explicit | Task 4 |
| No connector/runtime engine changes | Global constraints and all tasks |

### Placeholder scan

The plan defines concrete source names, files, rows, metric names, grains, commands, expected outcomes, and commit messages. It contains no unresolved work markers or deferred implementation steps.

### Type and interface consistency

- `generate_shopfloor_data` returns `ShopfloorDataPaths` in Task 1 and is consumed with that exact name in Tasks 2–3.
- `append_eol_retest(delta_path)` is produced and tested in Task 3 after the initial Delta data foundation from Task 1.
- `eol_test_runs`, `average_cycle_seconds`, and `eol_attempt_pass_rate` use identical identifiers in the catalog, tests, and runner.
- `run_walkthrough(engine, eol_test_runs)` is introduced and consumed only in Task 3.
