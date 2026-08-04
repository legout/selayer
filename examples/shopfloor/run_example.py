from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
# Make the repository root importable when this script is executed directly
# via ``uv run python examples/shopfloor/run_example.py``: running a script
# places the script's own directory on ``sys.path`` rather than the repo
# root, so the sibling ``examples.shopfloor`` import below would otherwise
# fail with ``ModuleNotFoundError``.
sys.path.insert(0, str(ROOT))

from examples.shopfloor.generate_data import (
    DeltaDependencyError,
    ShopfloorDataPaths,
    append_eol_retest,
    generate_shopfloor_data,
)
from selayer import QueryEngine, QueryPlanningError, SemanticLayer
from selayer.sources.config import (
    CsvConfig,
    DeltaConfig,
    DuckDbConfig,
    ParquetConfig,
    SqliteConfig,
)

EXAMPLE_DIR = ROOT / "examples" / "shopfloor"
CATALOG = EXAMPLE_DIR / "shopfloor_semantic_layer.yaml"

#: The closed set of connector configs that expose a file-system ``location``.
#: Every shop-floor source is one of these, so a non-matching connector is a
#: programmer error rather than a supported adaptation.
_LOCATION_CONNECTORS = (
    CsvConfig,
    DeltaConfig,
    DuckDbConfig,
    ParquetConfig,
    SqliteConfig,
)

#: Maps each catalog data-source name to the matching :class:`ShopfloorDataPaths`
#: attribute that holds its absolute physical location.
_LOCATION_ATTRS: dict[str, str] = {
    "customer_orders": "customer_orders",
    "production_orders": "production_orders_db",
    "serialized_drives": "shopfloor_db",
    "component_consumption": "component_consumption",
    "component_lot_inspections": "component_lot_inspections",
    "operation_executions": "operation_executions",
    "machine_telemetry": "machine_telemetry",
    "eol_test_runs": "eol_test_runs",
}


def _layer_for_paths(
    layer: SemanticLayer,
    paths: ShopfloorDataPaths,
) -> SemanticLayer:
    """Return a copy of ``layer`` whose every source points at ``paths``.

    The catalog (execution authority) is loaded once and never mutated; only the
    connector ``location`` of each source is rewritten to the temporary physical
    files via :func:`dataclasses.replace`. :class:`~selayer.QueryEngine` revalidates
    the returned programmatic layer before opening any source.
    """
    locations = {
        name: str(getattr(paths, attr)) for name, attr in _LOCATION_ATTRS.items()
    }
    sources = {}
    for name, source in layer.data_sources.items():
        connector = source.connector
        if not isinstance(connector, _LOCATION_CONNECTORS):
            raise TypeError("shopfloor source has no file location")
        sources[name] = replace(
            source,
            connector=replace(connector, location=locations[name]),
        )
    return replace(layer, data_sources=sources)


def run_walkthrough(engine: QueryEngine, eol_test_runs: Path) -> None:
    print("Production completion rate:")
    print(engine.query(["production_completion_rate"], ["schedule_status"]))
    print("Customer fulfilment:")
    print(engine.query(["shipped_unit_count"], ["customer_region", "product_model"]))
    print("Component genealogy for DRV-003:")
    print(
        engine.query(
            ["component_count"],
            ["component_lot_id"],
            {"drive_serial_number": "DRV-003"},
        )
    )
    print("Incoming component quality:")
    print(
        engine.query(["incoming_acceptance_rate"], ["supplier_name", "component_type"])
    )
    print("Operation performance:")
    print(
        engine.query(
            ["average_cycle_seconds", "rework_rate", "energy_per_operation_kwh"],
            ["line_id", "machine_id", "shift", "operation_name"],
        )
    )
    print("EOL quality before Delta reload:")
    print(
        engine.query(
            ["eol_attempt_pass_rate", "first_pass_yield"],
            ["station_id", "product_model", "firmware_revision"],
        )
    )
    print("Raw machine health:")
    print(
        engine.query(
            ["alarm_event_count", "average_temperature_c"],
            ["telemetry_line_id", "machine_state"],
        )
    )

    try:
        engine.plan(["average_cycle_seconds", "eol_attempt_pass_rate"])
    except QueryPlanningError as error:
        # Only the intentional mixed-grain rejection is expected here; any
        # other planner error indicates a real problem and must propagate.
        if error.code != "mixed_grain":
            raise
        print(f"Expected mixed-grain rejection: {error.code}")

    before = engine.source_status("eol_test_runs")
    append_eol_retest(eol_test_runs)
    change = engine.reload_source("eol_test_runs")
    # The source registry initializes every source at generation 1 and advances
    # on each reload (1 -> 2 -> 3). For this teaching fixture we present the
    # demonstration as a zero-based reload count so the example reads as the
    # first reload of freshly generated data; the underlying registry
    # generations are unchanged.
    print(
        f"EOL source generation: {before.generation - 1} -> {change.new_generation - 1}"
    )
    print("EOL pass rate after Delta reload:")
    print(engine.query(["eol_attempt_pass_rate", "first_pass_yield"]))


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
