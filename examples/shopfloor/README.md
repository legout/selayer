# Shop-floor motor-drive tutorial

This example is a self-contained, deterministic walk through `selayer`'s
grain-aware semantic model. It models a small motor-drive manufacturing floor
and stitches together **eight** local source files across **five** physical
formats — CSV, SQLite, DuckDB, Parquet, and Delta Lake — into one catalog, then
runs seven valid business questions, an intentional mixed-grain rejection, and a
live Delta source reload.

Everything is generated locally and bit-for-bit deterministic. No cloud
storage, credentials, Docker containers, or remote services are required.

## Motor-drive story

The example follows a single customer order through a make-to-order factory:

1. **Customer order.** A customer (`customer_orders`, CSV) requests a product
   model, e.g. `CO-1001` from the North region for the `X200` drive.
2. **Internal production order.** The ERP releases one or more production
   orders (`production_orders`, SQLite) against the customer order, each with
   planned and completed unit counts and a schedule status.
3. **Serialized drive.** Each built drive is registered by serial number
   (`serialized_drives`, DuckDB), tied to its production order, BOM and firmware
   revisions, and a completion/shipment status. Three drives exist:
   `DRV-001`, `DRV-002` (X200, shipped) and `DRV-003` (X300, in stock).
4. **Component consumption and lot inspection.** Every drive consumes two
   fitted components — a `power_module` and a `control_pcb` — each stamped with
   its component lot (`component_consumption`, Parquet). Incoming inspection
   records (`component_lot_inspections`, Parquet) record whether each lot passed
   or was quarantined. DRV-003 consumes lots `LOT-P-02` and `LOT-C-02`.
5. **Operations.** Each drive runs through manufacturing operations such as
   winding, assembly, and torque test (`operation_executions`, Parquet), one row
   per execution, including cycle seconds, energy, and a rework flag.
6. **Telemetry.** Machines emit an independent state/temperature/power stream
   (`machine_telemetry`, Parquet) keyed by machine × timestamp.
7. **End-of-line (EOL) testing.** Every drive gets one or more EOL test
   attempts (`eol_test_runs`, Delta Lake), with an attempt number, result, and
   first-pass flag. DRV-003 fails its first attempt and is retested at runtime.

## Setup and run

The Delta source requires the optional `delta` extra:

```bash
uv sync --extra delta
uv run python examples/shopfloor/run_example.py
```

If Delta is not installed the runner exits with the exact remediation command
`uv sync --extra delta` instead of producing a confusing import error. Running
the example resets `examples/shopfloor/data/`, regenerates every source file,
prints the walkthrough, and reloads the EOL source.

## Source and grain map

The catalog declares eight sources. Each one owns a non-empty, physically valid
grain, and the planner only combines metrics that resolve to the same grain via
a safe many-to-one relationship path.

| Source | Physical format | Grain |
| --- | --- | --- |
| `customer_orders` | CSV | `[customer_order_id]` |
| `production_orders` | SQLite | `[production_order_id]` |
| `serialized_drives` | DuckDB | `[serial_number]` |
| `component_consumption` | Parquet | `[serial_number, fitted_position]` |
| `component_lot_inspections` | Parquet | `[component_lot_id]` |
| `operation_executions` | Parquet | `[operation_execution_id]` |
| `machine_telemetry` | Parquet | `[machine_id, recorded_at]` |
| `eol_test_runs` | Delta Lake | `[eol_test_run_id]` |

`machine_telemetry` is deliberately a **separate** machine × timestamp stream.
There is no relationship from telemetry to per-drive operation facts: that would
require a time-window join and would expand the operation grain. Telemetry is
therefore queried at its own grain (`alarm_event_count`, `average_temperature_c`)
and is never silently merged into unit-level analysis.

## Walkthrough

`run_example.py` loads the static catalog, then `run_walkthrough` prints each
section in order:

1. **Production completion rate** — `production_completion_rate` by
   `schedule_status`. Across the three production orders, 3 of 5 planned units are
   complete (`3/5`), split by `on_time`, `late`, and `open` status.
2. **Customer fulfilment** — `shipped_unit_count` by `customer_region` and
   `product_model`. Two drives shipped (`DRV-001`, `DRV-002`).
3. **Component genealogy for DRV-003** — `component_count` by `component_lot_id`,
   filtered to `drive_serial_number = DRV-003`. DRV-003 consumes exactly two lots,
   `LOT-P-02` (power module) and `LOT-C-02` (control PCB), one component each.
4. **Incoming component quality** — `incoming_acceptance_rate` by `supplier_name`
   and `component_type`. Four of five inspected lots were accepted (`4/5`); the
   quarantined `LOT-C-03` is never consumed.
5. **Operation performance** — `average_cycle_seconds`, `rework_rate`, and
   `energy_per_operation_kwh` by `line_id`, `machine_id`, `shift`, and
   `operation_name`. Seven operations run, exactly one is a rework
   (`rework_rate = 1/7`).
6. **EOL quality before Delta reload** — `eol_attempt_pass_rate` and
   `first_pass_yield` by `station_id`, `product_model`, and `firmware_revision`.
   Two of three attempts pass and the first-pass yield is `2/3`.
7. **Raw machine health** — `alarm_event_count` and `average_temperature_c` by
   `telemetry_line_id` and `machine_state`. There is one alarm event and the
   average machine temperature across the four telemetry samples is `48.5` °C.

After the seven sections, the runner demonstrates the planning boundary:

```python
engine.plan(["average_cycle_seconds", "eol_attempt_pass_rate"])
```

Combining `average_cycle_seconds` (operation grain) with `eol_attempt_pass_rate`
(EOL grain) in a single plan is rejected because there is no safe relationship
path that preserves both grains. The runner catches the
`QueryPlanningError` and prints:

```
Expected mixed-grain rejection: mixed_grain
```

The runner then demonstrates a live Delta reload. It records the current EOL
source generation, appends the deterministic DRV-003 **second** EOL attempt
(`attempt = 2`, `result = pass`, `is_first_pass = false`) via
`append_eol_retest(paths.eol_test_runs)`, and reloads the source:

```python
change = engine.reload_source("eol_test_runs")
```

This prints the generation transition (e.g. `1 -> 2`) and re-queries the EOL
metrics. The appended row changes the attempt-pass rate from `2/3` to `3/4`
while the first-pass yield stays at `2/3`, because the retest is a pass but is
explicitly not a first pass. Reloads are explicit and preserve the previously
published source if the refresh fails.

## Non-goals

This example intentionally does **not** do any of the following:

- **JSON and DuckLake are not catalog connector types.** The closed connector
  matrix is `csv`, `sqlite`, `duckdb`, `parquet`, and `delta`. Feeding a JSON
  file or pointing at a DuckLake-managed table would require a new connector and
  is out of scope.
- No automatic re-graining or allocation. The `mixed_grain` boundary is a hard
  planning error, not something the example works around.
- No time-window telemetry joins. `machine_telemetry` stays at its own machine ×
  timestamp grain and is never joined onto per-drive operations.
- No cloud storage, credentials, or remote services. Every source is a local
  file written into `examples/shopfloor/data/`.
- No MES, ERP, or SCADA behaviour. The fixtures are static literals, not a live
  factory integration.
