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
        "customer_region": "EU-North",
        "requested_ship_date": "2025-03-15",
        "product_model": "DRV-X1",
        "order_status": "confirmed",
    },
    {
        "customer_order_id": "CO-1002",
        "customer_name": "Nordic Motion Oy",
        "customer_region": "EU-North",
        "requested_ship_date": "2025-03-22",
        "product_model": "DRV-X2",
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
        "product_model": "DRV-X1",
        "routing": "base-assembly",
        "planned_units": 2,
        "completed_units": 2,
        "schedule_status": "completed",
    },
    {
        "production_order_id": "PO-2002",
        "customer_order_id": "CO-1002",
        "product_model": "DRV-X2",
        "routing": "base-assembly",
        "planned_units": 2,
        "completed_units": 1,
        "schedule_status": "in_progress",
    },
    {
        "production_order_id": "PO-2003",
        "customer_order_id": "CO-1002",
        "product_model": "DRV-X3",
        "routing": "base-assembly",
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
        "serial_number": "SN-DRV-0001",
        "production_order_id": "PO-2001",
        "product_model": "DRV-X1",
        "bom_revision": "BOM-1.0",
        "firmware_revision": "FW-2.4.1",
        "completion_status": "completed",
        "shipment_status": "shipped",
    },
    {
        "serial_number": "SN-DRV-0002",
        "production_order_id": "PO-2001",
        "product_model": "DRV-X1",
        "bom_revision": "BOM-1.0",
        "firmware_revision": "FW-2.4.1",
        "completion_status": "completed",
        "shipment_status": "in_stock",
    },
    {
        "serial_number": "SN-DRV-0003",
        "production_order_id": "PO-2002",
        "product_model": "DRV-X2",
        "bom_revision": "BOM-1.1",
        "firmware_revision": "FW-2.4.1",
        "completion_status": "in_progress",
        "shipment_status": "pending",
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

_COMPONENT_CONSUMPTION_SCHEMA = pa.schema(
    [
        pa.field("serial_number", pa.string()),
        pa.field("fitted_position", pa.string()),
        pa.field("component_part_number", pa.string()),
        pa.field("component_serial_number", pa.string()),
        pa.field("component_lot_id", pa.string()),
    ]
)

_COMPONENT_LOT_INSPECTIONS_SCHEMA = pa.schema(
    [
        pa.field("component_lot_id", pa.string()),
        pa.field("supplier_name", pa.string()),
        pa.field("component_type", pa.string()),
        pa.field("incoming_result", pa.string()),
        pa.field("disposition", pa.string()),
    ]
)

_OPERATION_EXECUTIONS_SCHEMA = pa.schema(
    [
        pa.field("operation_execution_id", pa.string()),
        pa.field("serial_number", pa.string()),
        pa.field("operation_name", pa.string()),
        pa.field("line_id", pa.string()),
        pa.field("machine_id", pa.string()),
        pa.field("shift", pa.string()),
        pa.field("cycle_seconds", pa.int64()),
        pa.field("energy_kwh", pa.float64()),
        pa.field("max_torque_nm", pa.float64()),
        pa.field("max_temperature_c", pa.float64()),
        pa.field("result", pa.string()),
        pa.field("is_rework", pa.bool_()),
    ]
)

_MACHINE_TELEMETRY_SCHEMA = pa.schema(
    [
        pa.field("machine_id", pa.string()),
        pa.field("recorded_at", pa.timestamp("ns")),
        pa.field("line_id", pa.string()),
        pa.field("machine_state", pa.string()),
        pa.field("temperature_c", pa.float64()),
        pa.field("power_kw", pa.float64()),
    ]
)

_COMPONENT_CONSUMPTION = [
    {
        "serial_number": "SN-DRV-0001",
        "fitted_position": "HOUSING",
        "component_part_number": "P-100-HSG",
        "component_serial_number": "C-HSG-0001",
        "component_lot_id": "LOT-A",
    },
    {
        "serial_number": "SN-DRV-0001",
        "fitted_position": "STATOR",
        "component_part_number": "P-200-STAT",
        "component_serial_number": "C-STAT-0001",
        "component_lot_id": "LOT-B",
    },
    {
        "serial_number": "SN-DRV-0002",
        "fitted_position": "HOUSING",
        "component_part_number": "P-100-HSG",
        "component_serial_number": "C-HSG-0002",
        "component_lot_id": "LOT-A",
    },
    {
        "serial_number": "SN-DRV-0002",
        "fitted_position": "STATOR",
        "component_part_number": "P-200-STAT",
        "component_serial_number": "C-STAT-0002",
        "component_lot_id": "LOT-B",
    },
    {
        "serial_number": "SN-DRV-0003",
        "fitted_position": "HOUSING",
        "component_part_number": "P-100-HSG",
        "component_serial_number": "C-HSG-0003",
        "component_lot_id": "LOT-A",
    },
    {
        "serial_number": "SN-DRV-0003",
        "fitted_position": "STATOR",
        "component_part_number": "P-200-STAT",
        "component_serial_number": "C-STAT-0003",
        "component_lot_id": "LOT-C",
    },
]

_COMPONENT_LOT_INSPECTIONS = [
    {
        "component_lot_id": "LOT-A",
        "supplier_name": "Northwind Components",
        "component_type": "HOUSING",
        "incoming_result": "accept",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-B",
        "supplier_name": "Magnetics Co",
        "component_type": "STATOR",
        "incoming_result": "accept",
        "disposition": "released",
    },
    {
        "component_lot_id": "LOT-C",
        "supplier_name": "Magnetics Co",
        "component_type": "STATOR",
        "incoming_result": "conditional",
        "disposition": "rework",
    },
    {
        "component_lot_id": "LOT-D",
        "supplier_name": "BrightCaps Ltd",
        "component_type": "CAPACITOR",
        "incoming_result": "reject",
        "disposition": "scrapped",
    },
    {
        "component_lot_id": "LOT-E",
        "supplier_name": "Northwind Components",
        "component_type": "ROTOR",
        "incoming_result": "accept",
        "disposition": "released",
    },
]

_OPERATION_EXECUTIONS = [
    {
        "operation_execution_id": "OPE-9001",
        "serial_number": "SN-DRV-0001",
        "operation_name": "ASSEMBLY",
        "line_id": "LINE-1",
        "machine_id": "MC-AS-01",
        "shift": "day",
        "cycle_seconds": 240,
        "energy_kwh": 1.2,
        "max_torque_nm": 45.0,
        "max_temperature_c": 60.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-9002",
        "serial_number": "SN-DRV-0001",
        "operation_name": "TORQUE_TEST",
        "line_id": "LINE-1",
        "machine_id": "MC-TT-01",
        "shift": "day",
        "cycle_seconds": 90,
        "energy_kwh": 0.3,
        "max_torque_nm": 48.5,
        "max_temperature_c": 55.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-9003",
        "serial_number": "SN-DRV-0001",
        "operation_name": "PACKOUT",
        "line_id": "LINE-1",
        "machine_id": "MC-PK-01",
        "shift": "day",
        "cycle_seconds": 60,
        "energy_kwh": 0.1,
        "max_torque_nm": 0.0,
        "max_temperature_c": 25.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-9004",
        "serial_number": "SN-DRV-0002",
        "operation_name": "ASSEMBLY",
        "line_id": "LINE-1",
        "machine_id": "MC-AS-01",
        "shift": "day",
        "cycle_seconds": 245,
        "energy_kwh": 1.25,
        "max_torque_nm": 46.0,
        "max_temperature_c": 61.0,
        "result": "pass",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-9005",
        "serial_number": "SN-DRV-0002",
        "operation_name": "TORQUE_TEST",
        "line_id": "LINE-1",
        "machine_id": "MC-TT-01",
        "shift": "day",
        "cycle_seconds": 92,
        "energy_kwh": 0.31,
        "max_torque_nm": 47.0,
        "max_temperature_c": 56.0,
        "result": "fail",
        "is_rework": False,
    },
    {
        "operation_execution_id": "OPE-9006",
        "serial_number": "SN-DRV-0002",
        "operation_name": "TORQUE_TEST",
        "line_id": "LINE-1",
        "machine_id": "MC-TT-01",
        "shift": "day",
        "cycle_seconds": 95,
        "energy_kwh": 0.32,
        "max_torque_nm": 49.0,
        "max_temperature_c": 57.0,
        "result": "pass",
        "is_rework": True,
    },
    {
        "operation_execution_id": "OPE-9007",
        "serial_number": "SN-DRV-0003",
        "operation_name": "ASSEMBLY",
        "line_id": "LINE-2",
        "machine_id": "MC-AS-02",
        "shift": "night",
        "cycle_seconds": 250,
        "energy_kwh": 1.3,
        "max_torque_nm": 44.0,
        "max_temperature_c": 62.0,
        "result": "pass",
        "is_rework": False,
    },
]

_MACHINE_TELEMETRY = [
    {
        "machine_id": "MC-AS-01",
        "recorded_at": datetime.fromisoformat("2025-03-10T08:00:00"),
        "line_id": "LINE-1",
        "machine_state": "running",
        "temperature_c": 58.0,
        "power_kw": 4.5,
    },
    {
        "machine_id": "MC-AS-01",
        "recorded_at": datetime.fromisoformat("2025-03-10T08:15:00"),
        "line_id": "LINE-1",
        "machine_state": "idle",
        "temperature_c": 35.0,
        "power_kw": 0.8,
    },
    {
        "machine_id": "MC-TT-01",
        "recorded_at": datetime.fromisoformat("2025-03-10T09:00:00"),
        "line_id": "LINE-1",
        "machine_state": "running",
        "temperature_c": 52.0,
        "power_kw": 2.1,
    },
    {
        "machine_id": "MC-AS-02",
        "recorded_at": datetime.fromisoformat("2025-03-10T22:30:00"),
        "line_id": "LINE-2",
        "machine_state": "running",
        "temperature_c": 60.0,
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
        pa.field("eol_test_run_id", pa.string()),
        pa.field("serial_number", pa.string()),
        pa.field("station_id", pa.string()),
        pa.field("attempt", pa.int64()),
        pa.field("result", pa.string()),
        pa.field("is_first_pass", pa.bool_()),
        pa.field("input_voltage_v", pa.float64()),
        pa.field("output_voltage_v", pa.float64()),
        pa.field("power_w", pa.float64()),
    ]
)

_EOL_TEST_RUNS = [
    {
        "eol_test_run_id": "EOL-5001",
        "serial_number": "SN-DRV-0001",
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
        "serial_number": "SN-DRV-0002",
        "station_id": "STN-EOL-01",
        "attempt": 1,
        "result": "fail",
        "is_first_pass": False,
        "input_voltage_v": 400.0,
        "output_voltage_v": 0.0,
        "power_w": 60.0,
    },
    {
        "eol_test_run_id": "EOL-5003",
        "serial_number": "SN-DRV-0002",
        "station_id": "STN-EOL-01",
        "attempt": 2,
        "result": "pass",
        "is_first_pass": False,
        "input_voltage_v": 400.0,
        "output_voltage_v": 481.0,
        "power_w": 1210.0,
    },
]


def _write_eol_test_runs(path: Path) -> None:
    write_deltalake = _delta_writer()
    # ``_EOL_SCHEMA`` is the explicit physical schema for the Delta table; the
    # table is materialised against it so no schema inference occurs.
    table = pa.Table.from_pylist(_EOL_TEST_RUNS, schema=_EOL_SCHEMA)
    write_deltalake(str(path), table, mode="overwrite")
