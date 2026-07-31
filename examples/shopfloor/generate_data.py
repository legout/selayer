"""Deterministic shop-floor source-data generator.

This module materialises a small, fully deterministic set of shop-floor source
files into an output directory so that downstream connector and semantic-layer
tasks always consume identical data:

* ``customer_orders.csv``          - CSV of customer sales orders
* ``production_orders.sqlite``     - SQLite MES production-order table
* ``shopfloor.duckdb``             - DuckDB serial-number registry
* ``component_consumption.parquet``  - Parquet component-fit log
* ``component_lot_inspections.parquet`` - Parquet incoming-inspection log
* ``operation_executions.parquet`` - Parquet machine operation log
* ``machine_telemetry.parquet``    - Parquet machine telemetry stream
* ``eol_test_runs.delta``          - Delta Lake end-of-line test results

No randomness, Faker, Pandas, current timestamps, or schema inference are used:
every value is a literal so the resulting fixture is reproducible bit-for-bit.

The data literals follow the deterministic fixture contract in
``docs/superpowers/plans/2026-07-31-shopfloor-example.md`` exactly, so the
catalog metrics in Task 2 resolve to their documented expected values.
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Delta dependency boundary
# ---------------------------------------------------------------------------


def _delta_writer() -> Callable[..., Any]:
    """Return ``deltalake.write_deltalake``, importing it lazily.

    ``deltalake`` is an optional extra (``uv sync --extra delta``).  Importing it
    eagerly would make the whole generator unusable without the extra, so the
    import is deferred to this single seam and the missing-dependency case is
    translated into :class:`DeltaDependencyError`.
    """
    try:
        from deltalake import write_deltalake
    except ImportError as exc:  # pragma: no cover - dependency-absent path
        raise DeltaDependencyError(
            "Delta support is required for the shop-floor example; run: uv sync --extra delta"
        ) from exc
    return write_deltalake


# ---------------------------------------------------------------------------
# CSV - customer orders
# ---------------------------------------------------------------------------

_CUSTOMER_ORDER_FIELDS: tuple[str, ...] = (
    "customer_order_id",
    "customer_name",
    "customer_region",
    "requested_ship_date",
    "product_model",
    "order_status",
)

_CUSTOMER_ORDERS = [
    {
        "customer_order_id": "CO-1001",
        "customer_name": "Acme Drives Inc",
        "customer_region": "North",
        "requested_ship_date": "2025-03-15",
        "product_model": "X200",
        "order_status": "confirmed",
    },
    {
        "customer_order_id": "CO-1002",
        "customer_name": "Nordic Motion Oy",
        "customer_region": "Europe",
        "requested_ship_date": "2025-03-22",
        "product_model": "X300",
        "order_status": "confirmed",
    },
]


def _write_customer_orders(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CUSTOMER_ORDER_FIELDS)
        writer.writeheader()
        writer.writerows(_CUSTOMER_ORDERS)


# ---------------------------------------------------------------------------
# SQLite - production orders
# ---------------------------------------------------------------------------

_PRODUCTION_ORDERS_DDL = """
create table production_orders (
    production_order_id text primary key,
    customer_order_id text not null,
    product_model text not null,
    routing text not null,
    planned_units integer not null,
    completed_units integer not null,
    schedule_status text not null
)
"""

_PRODUCTION_ORDERS_INSERT = (
    "insert into production_orders "
    "(production_order_id, customer_order_id, product_model, routing, "
    "planned_units, completed_units, schedule_status) "
    "values (?, ?, ?, ?, ?, ?, ?)"
)

_PRODUCTION_ORDERS = [
    {
        "production_order_id": "PO-2001",
        "customer_order_id": "CO-1001",
        "product_model": "X200",
        "routing": "final-assembly",
        "planned_units": 2,
        "completed_units": 2,
        "schedule_status": "on_time",
    },
    {
        "production_order_id": "PO-2002",
        "customer_order_id": "CO-1002",
        "product_model": "X300",
        "routing": "final-assembly",
        "planned_units": 2,
        "completed_units": 1,
        "schedule_status": "late",
    },
    {
        "production_order_id": "PO-2003",
        "customer_order_id": "CO-1002",
        "product_model": "X300",
        "routing": "final-assembly",
        "planned_units": 1,
        "completed_units": 0,
        "schedule_status": "open",
    },
]

_PRODUCTION_ORDER_COLUMNS = (
    "production_order_id",
    "customer_order_id",
    "product_model",
    "routing",
    "planned_units",
    "completed_units",
    "schedule_status",
)


def _write_production_orders(path: Path) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(_PRODUCTION_ORDERS_DDL)
        connection.executemany(
            _PRODUCTION_ORDERS_INSERT,
            [
                tuple(row[col] for col in _PRODUCTION_ORDER_COLUMNS)
                for row in _PRODUCTION_ORDERS
            ],
        )


# ---------------------------------------------------------------------------
# DuckDB - serialized drives
# ---------------------------------------------------------------------------

_SERIALIZED_DRIVES_DDL = """
create table serialized_drives (
    serial_number varchar primary key,
    production_order_id varchar not null,
    product_model varchar not null,
    bom_revision varchar not null,
    firmware_revision varchar not null,
    completion_status varchar not null,
    shipment_status varchar not null
)
"""

_SERIALIZED_DRIVES_INSERT = (
    "insert into serialized_drives "
    "(serial_number, production_order_id, product_model, bom_revision, "
    "firmware_revision, completion_status, shipment_status) "
    "values (?, ?, ?, ?, ?, ?, ?)"
)

_SERIALIZED_DRIVES = [
    {
        "serial_number": "DRV-001",
        "production_order_id": "PO-2001",
        "product_model": "X200",
        "bom_revision": "BOM-A",
        "firmware_revision": "FW-1.0",
        "completion_status": "completed",
        "shipment_status": "shipped",
    },
    {
        "serial_number": "DRV-002",
        "production_order_id": "PO-2001",
        "product_model": "X200",
        "bom_revision": "BOM-A",
        "firmware_revision": "FW-1.0",
        "completion_status": "completed",
        "shipment_status": "shipped",
    },
    {
        "serial_number": "DRV-003",
        "production_order_id": "PO-2002",
        "product_model": "X300",
        "bom_revision": "BOM-B",
        "firmware_revision": "FW-2.1",
        "completion_status": "completed",
        "shipment_status": "in_stock",
    },
]

_SERIALIZED_DRIVE_COLUMNS = (
    "serial_number",
    "production_order_id",
    "product_model",
    "bom_revision",
    "firmware_revision",
    "completion_status",
    "shipment_status",
)


def _write_serialized_drives(path: Path) -> None:
    # DuckDB stores a single persistent file (not a directory), so remove any
    # stale file directly.  The top-level generator also wipes ``output_dir``.
    path.unlink(missing_ok=True)
    with duckdb.connect(str(path)) as connection:
        connection.execute(_SERIALIZED_DRIVES_DDL)
        connection.executemany(
            _SERIALIZED_DRIVES_INSERT,
            [
                tuple(row[col] for col in _SERIALIZED_DRIVE_COLUMNS)
                for row in _SERIALIZED_DRIVES
            ],
        )


# ---------------------------------------------------------------------------
# Parquet sources
# ---------------------------------------------------------------------------

# Every physical Parquet field is declared ``nullable=False``.  The catalog's
# declared schemas in Task 2 are likewise non-nullable, and the Parquet/Delta
# connectors observe the *physical* nullability before comparing against the
# declaration; declaring the physical schema non-nullable keeps the two in
# exact agreement so no false drift is reported.
_COMPONENT_CONSUMPTION_SCHEMA = pa.schema(
    [
        pa.field("serial_number", pa.string(), nullable=False),
        pa.field("fitted_position", pa.string(), nullable=False),
        pa.field("component_part_number", pa.string(), nullable=False),
        pa.field("component_serial_number", pa.string(), nullable=False),
        pa.field("component_lot_id", pa.string(), nullable=False),
    ]
)

_COMPONENT_LOT_INSPECTIONS_SCHEMA = pa.schema(
    [
        pa.field("component_lot_id", pa.string(), nullable=False),
        pa.field("supplier_name", pa.string(), nullable=False),
        pa.field("component_type", pa.string(), nullable=False),
        pa.field("incoming_result", pa.string(), nullable=False),
        pa.field("disposition", pa.string(), nullable=False),
    ]
)

_OPERATION_EXECUTIONS_SCHEMA = pa.schema(
    [
        pa.field("operation_execution_id", pa.string(), nullable=False),
        pa.field("serial_number", pa.string(), nullable=False),
        pa.field("operation_name", pa.string(), nullable=False),
        pa.field("line_id", pa.string(), nullable=False),
        pa.field("machine_id", pa.string(), nullable=False),
        pa.field("shift", pa.string(), nullable=False),
        # ``cycle_seconds`` is a physical measurement modelled as a decimal
        # metric in the catalog; the catalog's ``decimal`` data type is
        # compatible with ``float64`` (not ``int64``), so it is stored as a
        # double physically.
        pa.field("cycle_seconds", pa.float64(), nullable=False),
        pa.field("energy_kwh", pa.float64(), nullable=False),
        pa.field("max_torque_nm", pa.float64(), nullable=False),
        pa.field("max_temperature_c", pa.float64(), nullable=False),
        pa.field("result", pa.string(), nullable=False),
        pa.field("is_rework", pa.bool_(), nullable=False),
    ]
)

_MACHINE_TELEMETRY_SCHEMA = pa.schema(
    [
        pa.field("machine_id", pa.string(), nullable=False),
        pa.field("recorded_at", pa.timestamp("ns"), nullable=False),
        pa.field("line_id", pa.string(), nullable=False),
        pa.field("machine_state", pa.string(), nullable=False),
        pa.field("temperature_c", pa.float64(), nullable=False),
        pa.field("power_kw", pa.float64(), nullable=False),
    ]
)

_COMPONENT_CONSUMPTION = [
    {
        "serial_number": "DRV-001",
        "fitted_position": "power_module",
        "component_part_number": "PN-PM-100",
        "component_serial_number": "CS-PM-001",
        "component_lot_id": "LOT-P-01",
    },
    {
        "serial_number": "DRV-001",
        "fitted_position": "control_pcb",
        "component_part_number": "PN-PC-200",
        "component_serial_number": "CS-PC-001",
        "component_lot_id": "LOT-C-01",
    },
    {
        "serial_number": "DRV-002",
        "fitted_position": "power_module",
        "component_part_number": "PN-PM-100",
        "component_serial_number": "CS-PM-002",
        "component_lot_id": "LOT-P-01",
    },
    {
        "serial_number": "DRV-002",
        "fitted_position": "control_pcb",
        "component_part_number": "PN-PC-200",
        "component_serial_number": "CS-PC-002",
        "component_lot_id": "LOT-C-01",
    },
    {
        "serial_number": "DRV-003",
        "fitted_position": "power_module",
        "component_part_number": "PN-PM-100",
        "component_serial_number": "CS-PM-003",
        "component_lot_id": "LOT-P-02",
    },
    {
        "serial_number": "DRV-003",
        "fitted_position": "control_pcb",
        "component_part_number": "PN-PC-200",
        "component_serial_number": "CS-PC-003",
        "component_lot_id": "LOT-C-02",
    },
]

_COMPONENT_LOT_INSPECTIONS = [
    {
        "component_lot_id": "LOT-P-01",
        "supplier_name": "Northwind Components",
        "component_type": "power_module",
        "incoming_result": "pass",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-C-01",
        "supplier_name": "BrightCaps Ltd",
        "component_type": "control_pcb",
        "incoming_result": "pass",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-P-02",
        "supplier_name": "Northwind Components",
        "component_type": "power_module",
        "incoming_result": "pass",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-C-02",
        "supplier_name": "BrightCaps Ltd",
        "component_type": "control_pcb",
        "incoming_result": "pass",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-C-03",
        "supplier_name": "BrightCaps Ltd",
        "component_type": "control_pcb",
        "incoming_result": "fail",
        "disposition": "quarantined",
    },
]

_OPERATION_EXECUTIONS = [
    {
        "operation_execution_id": "OPE-7001",
        "serial_number": "DRV-001",
        "operation_name": "winding",
        "line_id": "LINE-1",
        "machine_id": "MC-01",
        "shift": "day",
        "cycle_seconds": 60.0,
        "energy_kwh": 1.10,
        "max_torque_nm": 45.0,
        "max_temperature_c": 60.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-7002",
        "serial_number": "DRV-001",
        "operation_name": "assembly",
        "line_id": "LINE-1",
        "machine_id": "MC-02",
        "shift": "day",
        "cycle_seconds": 40.0,
        "energy_kwh": 1.20,
        "max_torque_nm": 46.0,
        "max_temperature_c": 61.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-7003",
        "serial_number": "DRV-002",
        "operation_name": "winding",
        "line_id": "LINE-1",
        "machine_id": "MC-01",
        "shift": "day",
        "cycle_seconds": 65.0,
        "energy_kwh": 1.15,
        "max_torque_nm": 44.0,
        "max_temperature_c": 59.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-7004",
        "serial_number": "DRV-002",
        "operation_name": "assembly",
        "line_id": "LINE-1",
        "machine_id": "MC-02",
        "shift": "day",
        "cycle_seconds": 45.0,
        "energy_kwh": 1.25,
        "max_torque_nm": 47.0,
        "max_temperature_c": 62.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-7005",
        "serial_number": "DRV-002",
        "operation_name": "torque_test",
        "line_id": "LINE-1",
        "machine_id": "MC-03",
        "shift": "day",
        "cycle_seconds": 55.0,
        "energy_kwh": 0.30,
        "max_torque_nm": 48.0,
        "max_temperature_c": 55.0,
        "result": "fail",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-7006",
        "serial_number": "DRV-002",
        "operation_name": "torque_test",
        "line_id": "LINE-1",
        "machine_id": "MC-03",
        "shift": "day",
        "cycle_seconds": 80.0,
        "energy_kwh": 0.32,
        "max_torque_nm": 49.0,
        "max_temperature_c": 57.0,
        "result": "pass",
        "is_rework": True,
    },
    {
        "operation_execution_id": "OPE-7007",
        "serial_number": "DRV-003",
        "operation_name": "winding",
        "line_id": "LINE-2",
        "machine_id": "MC-04",
        "shift": "night",
        "cycle_seconds": 50.0,
        "energy_kwh": 1.30,
        "max_torque_nm": 43.0,
        "max_temperature_c": 63.0,
        "result": "pass",
        "is_rework": False,
    },
]

_MACHINE_TELEMETRY = [
    {
        "machine_id": "MC-01",
        "recorded_at": datetime.fromisoformat("2025-03-10T08:00:00"),
        "line_id": "LINE-1",
        "machine_state": "running",
        "temperature_c": 44.0,
        "power_kw": 4.5,
    },
    {
        "machine_id": "MC-03",
        "recorded_at": datetime.fromisoformat("2025-03-10T08:15:00"),
        "line_id": "LINE-1",
        "machine_state": "alarm",
        "temperature_c": 82.0,
        "power_kw": 5.1,
    },
    {
        "machine_id": "MC-01",
        "recorded_at": datetime.fromisoformat("2025-03-10T09:00:00"),
        "line_id": "LINE-1",
        "machine_state": "idle",
        "temperature_c": 38.0,
        "power_kw": 0.8,
    },
    {
        "machine_id": "MC-04",
        "recorded_at": datetime.fromisoformat("2025-03-10T22:30:00"),
        "line_id": "LINE-2",
        "machine_state": "running",
        "temperature_c": 30.0,
        "power_kw": 4.7,
    },
]


def _write_parquet(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _write_parquet_sources(paths: ShopfloorDataPaths) -> None:
    _write_parquet(
        paths.component_consumption,
        _COMPONENT_CONSUMPTION,
        _COMPONENT_CONSUMPTION_SCHEMA,
    )
    _write_parquet(
        paths.component_lot_inspections,
        _COMPONENT_LOT_INSPECTIONS,
        _COMPONENT_LOT_INSPECTIONS_SCHEMA,
    )
    _write_parquet(
        paths.operation_executions,
        _OPERATION_EXECUTIONS,
        _OPERATION_EXECUTIONS_SCHEMA,
    )
    _write_parquet(
        paths.machine_telemetry,
        _MACHINE_TELEMETRY,
        _MACHINE_TELEMETRY_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Delta Lake - end-of-line test runs
# ---------------------------------------------------------------------------

_EOL_SCHEMA = pa.schema(
    [
        pa.field("eol_test_run_id", pa.string(), nullable=False),
        pa.field("serial_number", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("attempt", pa.int64(), nullable=False),
        pa.field("result", pa.string(), nullable=False),
        pa.field("is_first_pass", pa.bool_(), nullable=False),
        pa.field("input_voltage_v", pa.float64(), nullable=False),
        pa.field("output_voltage_v", pa.float64(), nullable=False),
        pa.field("power_w", pa.float64(), nullable=False),
    ]
)

# Initial end-of-line attempts only.  The shop-floor runner (later task)
# appends the deterministic DRV-003 passing second attempt at runtime.
_EOL_TEST_RUNS = [
    {
        "eol_test_run_id": "EOL-5001",
        "serial_number": "DRV-001",
        "station_id": "STN-EOL-01",
        "attempt": 1,
        "result": "pass",
        "is_first_pass": True,
        "input_voltage_v": 400.0,
        "output_voltage_v": 480.0,
        "power_w": 1200.0,
    },
    {
        "eol_test_run_id": "EOL-5002",
        "serial_number": "DRV-002",
        "station_id": "STN-EOL-01",
        "attempt": 1,
        "result": "pass",
        "is_first_pass": True,
        "input_voltage_v": 400.0,
        "output_voltage_v": 481.0,
        "power_w": 1210.0,
    },
    {
        "eol_test_run_id": "EOL-5003",
        "serial_number": "DRV-003",
        "station_id": "STN-EOL-01",
        "attempt": 1,
        "result": "fail",
        "is_first_pass": False,
        "input_voltage_v": 400.0,
        "output_voltage_v": 0.0,
        "power_w": 60.0,
    },
]


def _write_eol_test_runs(path: Path) -> None:
    write_deltalake = _delta_writer()
    # ``_EOL_SCHEMA`` is the explicit physical schema for the Delta table; the
    # table is materialised against it so no schema inference occurs.
    table = pa.Table.from_pylist(_EOL_TEST_RUNS, schema=_EOL_SCHEMA)
    write_deltalake(str(path), table, mode="overwrite")
