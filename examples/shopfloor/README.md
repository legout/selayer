# Shop-floor motor-drive tutorial

This example is a self-contained, deterministic reference for safe grain-aware
semantic modeling, exact physical verification, immutable reload demonstrations,
and on-demand enriched OKF generation. It models a small motor-drive
manufacturing floor and stitches together **eight** local source files across
**five** physical formats — CSV, SQLite, DuckDB, Parquet, and Delta Lake — into
one catalog, then runs twelve valid business metrics, an intentional mixed-grain
rejection, and a live Delta source reload on temporary data.

Everything is generated locally in a temporary directory and produces
deterministic logical data and results: the fixed literals guarantee the same
rows and metrics on every run. No cloud storage, credentials, Docker containers,
or remote services are required.

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
   (`machine_telemetry`, Parquet) keyed by telemetry machine × timestamp.
7. **End-of-line (EOL) testing.** Every drive gets one or more EOL test
   attempts (`eol_test_runs`, Delta Lake), with an attempt number, result, and
   first-pass flag. DRV-003 fails its first attempt and is retested at runtime.

## Setup and run

The Delta source requires the optional `delta` extra. The runner generates
all runtime data in a temporary directory and never writes to
`examples/shopfloor/data/`:

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

The runner (`run_example.py`) generates temporary data and cleans up
automatically; it never writes to `examples/shopfloor/data/`. The standalone
generator (`generate_data.py`) requires an explicit `--output-dir`, resets or
replaces any existing output on each run, and is used for physical audits that
read source files from a known location. `examples/shopfloor/data/` is
disposable generated output, not an authored source directory — never edit it
or commit it.

## Source and grain map

The catalog declares eight sources. Each one owns a non-empty, physically valid
grain, and the planner only combines metrics that resolve to the same grain via
a safe many-to-one relationship path. Physical grain columns are shown below
with an arrow to the catalog semantic identifier where the name differs.

| Source | Physical format | Grain |
| --- | --- | --- |
| `customer_orders` | CSV | `[customer_order_id]` |
| `production_orders` | SQLite | `[production_order_id]` |
| `serialized_drives` | DuckDB | `[serial_number]` → `drive_serial_number` |
| `component_consumption` | Parquet | `[serial_number, fitted_position]` |
| `component_lot_inspections` | Parquet | `[component_lot_id]` |
| `operation_executions` | Parquet | `[operation_execution_id]` |
| `machine_telemetry` | Parquet | `[machine_id, recorded_at]` → `telemetry_machine_id`, `telemetry_recorded_at` |
| `eol_test_runs` | Delta Lake | `[eol_test_run_id]` |

## Corrected semantic model

- Serialized drives own the conformed drive identity (`drive_serial_number`).
- Operation and telemetry machine dimensions are domain-specific:
  `operation_machine_id` belongs to operation executions and
  `telemetry_machine_id` belongs to telemetry events.
- No operation-to-telemetry relationship exists. Matching string values do not
  establish a safe join.
- `requested_ship_date` and `telemetry_recorded_at` are the only modeled times.
  `requested_ship_date` is order intent, not actual shipment time.
  `telemetry_recorded_at` is a sample timestamp, not operation time.

`machine_telemetry` is deliberately a **separate** telemetry machine ×
timestamp stream. There is no relationship from telemetry to per-drive
operation facts: that would require a time-window join and would expand the
operation grain. Telemetry is therefore queried at its own grain
(`alarm_event_count`, `average_temperature_c`) and is never silently merged into
unit-level analysis.

## Walkthrough

`run_example.py` loads the static catalog, generates temporary data, rebases
every source onto the temporary files, then `run_walkthrough` prints each
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
5. **Operation performance** — `average_cycle_seconds`, `operation_count`,
   `rework_rate`, and `energy_per_operation_kwh` by `operation_line_id`,
   `operation_machine_id`, `shift`, and `operation_name`. Seven distinct
   operation executions run, exactly one is a rework
   (`rework_rate = 1/7`).
6. **EOL quality before Delta reload** — `eol_attempt_pass_rate` and
   `first_pass_yield` by `station_id`, `product_model`, and `firmware_revision`.
   Two of three attempts pass and the first-pass yield is `2/3`.
7. **Raw machine health** — `alarm_event_count` and `average_temperature_c` by
   `telemetry_line_id`, `telemetry_machine_id`, and `machine_state`. There is one
   alarm event. The global average machine temperature across the four telemetry
   samples is `48.5` °C (each grouped row reports the per-group average, not the
   global value).

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

## Baseline and retest states

The runner then demonstrates a live Delta reload. It records the current EOL
source generation, appends the deterministic DRV-003 **second** EOL attempt
(`attempt = 2`, `result = pass`, `is_first_pass = false`) via
`append_eol_retest(paths.eol_test_runs)`, and reloads the source:

```
EOL source generation: 0 -> 1
```

The source registry initializes every source at generation `1` and advances
on each reload (`1 -> 2 -> 3`). The walkthrough presents this demonstration as
a zero-based reload count so it reads as the first reload of freshly generated
data; the underlying registry generations are unchanged.

The appended row changes only the temporary Delta data. The exact states are:

```text
Baseline: 3 EOL attempts, attempt pass rate 2/3, first-pass yield 2/3.
Temporary retest: 4 EOL attempts, attempt pass rate 3/4, first-pass yield 2/3.
```

The reload mutates only temporary Delta data. Repository data is never touched.

## Authored and generated knowledge

The example ships reviewed business context and curated OKF overlays as source
files:

- `business_context/` contains four reviewed Reference documents (process
  overview, KPI definitions, quality policy, and glossary).
- `okf_overlays/` contains curated metric, source, relationship, dimension,
  fact, and measure overlays with declarative query examples.

`build_knowledge.py` composes a fresh OKF bundle from the catalog plus these
authored inputs, validates it against the shopfloor knowledge policy, and
publishes it atomically to `.generated/knowledge/`:

```bash
uv run python examples/shopfloor/build_knowledge.py
```

The generated output at `.generated/knowledge/` is disposable and never
committed to Git. Generated fields and `Catalog Definition` come only from the
catalog. Overlays can add only curated sections and approved provenance.
`build_knowledge.py` rejects an existing non-empty destination; remove
`.generated/knowledge/` (or pass a fresh `--output-dir`) before rerunning.

**Catalog YAML is execution authority. OKF is advisory.** The catalog controls
queryable dimensions, facts, measures, metrics, relationships, planning, and
compilation. OKF Markdown can explain those objects, but it cannot add
executable semantics or override the catalog.

## Non-goals

This example intentionally does **not** do any of the following:

- **JSON and DuckLake are not catalog connector types.** The closed connector
  matrix is `csv`, `sqlite`, `duckdb`, `parquet`, and `delta`.
- No automatic re-graining or allocation. The `mixed_grain` boundary is a hard
  planning error, not something the example works around.
- No time-window telemetry joins. `machine_telemetry` stays at its own
  `telemetry_machine_id` × `telemetry_recorded_at` grain and is never joined
  onto per-drive operations.
- No cloud storage, credentials, or remote services. Every source is a local
  file generated into a temporary directory.
- No MES, ERP, or SCADA behaviour. The fixtures are static literals, not a live
  factory integration.
