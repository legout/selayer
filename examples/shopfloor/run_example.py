from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Make the repository root importable when this script is executed directly
# via ``uv run python examples/shopfloor/run_example.py``: running a script
# places the script's own directory on ``sys.path`` rather than the repo
# root, so the sibling ``examples.shopfloor`` import below would otherwise
# fail with ``ModuleNotFoundError``.
sys.path.insert(0, str(ROOT))

from examples.shopfloor.generate_data import (
    DeltaDependencyError,
    append_eol_retest,
    generate_shopfloor_data,
)
from selayer import QueryEngine, QueryPlanningError, SemanticLayer

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
        # Only the intentional mixed-grain rejection is expected here; any
        # other planner error indicates a real problem and must propagate.
        if error.code != "mixed_grain":
            raise
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
