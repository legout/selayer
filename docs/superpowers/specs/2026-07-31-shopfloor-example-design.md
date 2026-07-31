# Shop-floor multi-source example design

**Status:** approved for planning

## Purpose

Add a self-contained, runnable shop-floor tutorial centered on a serialized
industrial motor drive/controller. The example should demonstrate that
`selayer` can model a realistic manufacturing path across local heterogeneous
sources while preserving the grain of every metric:

```text
customer order → internal production order → serialized drive
  ├─ component-lot consumption and incoming inspection
  ├─ unit-level operation execution
  └─ final EOL test attempt → shipment state

machine telemetry remains a separate machine × timestamp event stream.
```

The example is a documentation and planner showcase, not a complete MES,
industrial-IoT, or connector reference implementation.

## User experience

A user installs the Delta optional dependency once and runs one script from the
repository root:

```bash
uv sync --extra delta
uv run python examples/shopfloor/run_example.py
```

The script resets deterministic local data, loads the catalog, prints a small
set of valid business queries, catches one intentional `mixed_grain` planning
failure, appends a deterministic Delta EOL retest, reloads only that source,
and prints the changed result and source generation.

No Docker daemon, cloud account, runtime profile, credentials, or external
service is required.

## Scope

Create the following structure:

```text
examples/shopfloor/
  README.md
  shopfloor_semantic_layer.yaml
  generate_data.py
  run_example.py
  schemas/
    customer_orders.yaml
    production_orders.yaml
    serialized_drives.yaml
    component_consumption.yaml
    component_lot_inspections.yaml
    operation_executions.yaml
    machine_telemetry.yaml
    eol_test_runs.yaml
  data/                         # regenerated and gitignored

tests/integration/test_shopfloor.py
```

The catalog and schema references live with the example. Source locations are
repository-root-relative so the documented command works from the repository
root, matching the current e-commerce example convention.

`generate_data.py` exposes a callable generation entry point for tests and a
small command-line entry point for manual inspection. `run_example.py` calls
the same generator to reset `examples/shopfloor/data/` before opening the
`QueryEngine`; repeated runs therefore remain deterministic even though the
walkthrough appends a Delta retest.

## Source topology and grains

The example has eight catalog sources.

| Source | Physical format | Grain | Key fields and purpose |
|---|---|---|---|
| `customer_orders` | CSV | `customer_order_id` | Customer, destination/region, requested date, ordered product model, order status. |
| `production_orders` | SQLite relation | `production_order_id` | Internal order, customer order reference, routing, planned/completed quantities, schedule status. |
| `serialized_drives` | DuckDB relation | `serial_number` | Finished unit, production-order reference, product model, BOM revision, firmware revision, completion and shipment state. |
| `component_consumption` | Parquet | `serial_number × fitted_position` | Fitted component part/serial, supplier lot, and unit genealogy. |
| `component_lot_inspections` | Parquet | `component_lot_id` | One incoming/component-test result per supplier lot, with supplier, component part, result, and disposition. |
| `operation_executions` | Parquet | `operation_execution_id` | Serialized unit, routing operation, line, machine, shift, duration, energy, summarized torque/temperature, result, and rework flag. |
| `machine_telemetry` | Parquet | `machine_id × recorded_at` | Raw machine-state, temperature, power, and alarm events. |
| `eol_test_runs` | Delta | `eol_test_run_id` | Serialized unit, station, attempt number, pass/fail result, and measured test values. |

The relationship graph is deliberately constrained to safe, grain-preserving
many-to-one traversal from fact anchors:

```text
customer_orders (1) ──< production_orders (many) ──< serialized_drives (many)
                                                   ├──< component_consumption (many)
component_lot_inspections (1) ────────────────────┘
serialized_drives (1) ──< operation_executions (many)
serialized_drives (1) ──< eol_test_runs (many)
```

Shipment attributes stay on `serialized_drives`, so the end-to-end customer
fulfilment story fits without a ninth source. `machine_telemetry` is
intentionally independent: sharing a `machine_id` code does not make raw
machine events safely joinable to unit operations without an explicit
re-graining/time-window allocation rule, which is outside `selayer`'s current
planner scope.

## Connector boundaries

The example intentionally uses only supported catalog connector types:

- CSV for `customer_orders`;
- SQLite for `production_orders`;
- DuckDB-file relations for `serialized_drives`;
- Parquet for component, operation, and telemetry records;
- Delta for `eol_test_runs`.

Delta requires the existing `delta` optional dependency. JSON and DuckLake are
not supported catalog connector types and are explicitly out of scope. The
example must not present conversions, direct DuckDB SQL, or a PyArrow provider
as an implicit JSON or DuckLake connector workaround.

## Synthetic data contract

The generator uses fixed seeds and explicit physical schemas so catalog schema
validation observes the intended types and nullability. It writes a small,
inspectable data set rather than a volume benchmark. The generated records
must include these meaningful cases:

- customer demand producing one or more internal production orders;
- completed, late, and still-open production orders;
- multiple serialized drives across product, BOM, and firmware revisions;
- distinct component supplier lots, including an incoming-inspection failure;
- a fitted-component genealogy for each finished drive;
- successful operations, at least one rework operation, and varied cycle times;
- telemetry with normal states plus at least one alarm/temperature anomaly;
- EOL first-pass successes and a unit that fails first attempt then passes a
  second attempt;
- at least one shipped and one unshipped serialized unit.

The Delta append used by `run_example.py` adds a new second-attempt EOL pass for
a known unit. It must change the all-attempt EOL pass-rate query while leaving
the first-pass-yield query unchanged.

## Catalog metrics and walkthrough

The catalog provides dimensions and metrics that make every fact anchor clear.
The runner demonstrates the following independent queries:

1. **Production order execution** — completion rate and planned/completed units
   by product model, routing, and schedule status; anchored at
   `production_orders`.
2. **Customer fulfilment** — shipped-unit count by customer region, product
   model, BOM revision, and firmware revision; anchored at `serialized_drives`
   and traversing safely back through production and customer orders.
3. **Unit genealogy** — component count by part, supplier lot, and fitted
   position filtered to a selected `serial_number`; anchored at
   `component_consumption`.
4. **Incoming component quality** — component-lot acceptance rate by supplier
   and component type; anchored at `component_lot_inspections`.
5. **Operation performance** — average cycle time, rework rate, and energy per
   operation by line, machine, shift, and routing step; anchored at
   `operation_executions`.
6. **EOL quality** — all-attempt pass rate and first-pass yield by station,
   product model, and firmware revision; anchored at `eol_test_runs`.
7. **Raw machine health** — alarm-event count and average temperature/power by
   machine, line, and machine state; anchored at `machine_telemetry`.

The script then intentionally requests one operation metric and one EOL metric
in a single query. It catches and labels the expected `QueryPlanningError` with
code `mixed_grain`; it does not try to hide that failure or automatically
allocate/re-grain the data.

Finally, the script appends the deterministic Delta retest, calls
`engine.reload_source("eol_test_runs")`, prints the updated generation/status,
and repeats the all-attempt EOL pass-rate query. This demonstrates the
connector lifecycle without changing unrelated sources or metrics.

## Error handling and documentation

`run_example.py` checks for the `deltalake` dependency before generation and
raises a concise setup error that directs users to `uv sync --extra delta`.
All other errors should surface normally; the only expected exception caught by
the walkthrough is the intentionally demonstrated `mixed_grain` planning
failure.

`examples/shopfloor/README.md` documents:

- the product narrative and source/grain map;
- setup and run commands;
- the supported connector mix and the Delta requirement;
- each printed query and the business question it answers;
- why telemetry is a separate event grain;
- why the mixed-grain request fails;
- the Delta append/reload sequence;
- explicit non-goals: JSON, DuckLake, remote services, raw telemetry
  allocation, automatic re-graining, and MES completeness.

The top-level `README.md` gains a concise link to this shop-floor example next
to the existing e-commerce example.

## Verification

Add `tests/integration/test_shopfloor.py`. It uses `tmp_path` and the callable
generator, following the repository's local deterministic Delta-test pattern;
it must not require Docker or any external service.

Tests cover:

1. deterministic generation of all physical source formats and the expected
   schemas;
2. successful catalog loading and connector initialization;
3. every valid walkthrough query, including expected columns and stable
   aggregate values;
4. the intentional `mixed_grain` error code;
5. Delta append plus `reload_source` advancing the EOL source generation and
   updating only the retest-sensitive EOL metric;
6. regeneration in a new temporary directory producing equivalent query
   results.

Run the focused test file, the full non-service test suite, `ruff check`, and
`pyright` over the new example, tests, and source files before declaring the
implementation complete.

## Non-goals

This work does not add connector support, query-engine features, automatic
allocation/re-graining, time-window joins, real-time ingestion, remote object
storage, credentials/profiles, Docker services, a product/BOM master source,
or a production-ready MES data model. Those would be separate feature designs.
