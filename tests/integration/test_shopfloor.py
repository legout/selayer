"""Integration tests for the shop-floor example.

The first test verifies that :func:`generate_shopfloor_data` materialises
every deterministic connector input (CSV, SQLite, DuckDB, Parquet, Delta Lake)
with the agreed row counts.  The second test loads the static semantic catalog
against the generated data and asserts every documented query answer plus the
grain-safe planner boundary.  No external services are required.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow.parquet as pq
import pytest
import yaml
from deltalake import DeltaTable

from examples.shopfloor.generate_data import (
    DeltaDependencyError,
    ShopfloorDataPaths,
    append_eol_retest,
    generate_shopfloor_data,
)
from examples.shopfloor.run_example import run_walkthrough
from selayer import QueryEngine, QueryPlanningError, SemanticLayer

_REPO = Path(__file__).parents[2]
_CATALOG = _REPO / "examples" / "shopfloor" / "shopfloor_semantic_layer.yaml"
_SCHEMA_DIR = _REPO / "examples" / "shopfloor" / "schemas"

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


def _temporary_shopfloor_catalog(tmp_path: Path, paths: ShopfloorDataPaths) -> Path:
    """Materialise a copy of the static catalog under ``tmp_path``.

    Only the eight ``location`` values are rewritten to the absolute
    :class:`ShopfloorDataPaths` members, and every ``schema_ref`` is replaced
    by the inlined schema mapping loaded from ``examples/shopfloor/schemas/``.
    This mirrors the established e-commerce integration fixture and avoids
    mutating repository data.
    """
    catalog = cast(
        dict[str, Any],
        yaml.safe_load(_CATALOG.read_text(encoding="utf-8")),
    )
    for name, source in cast(
        dict[str, dict[str, Any]], catalog["data_sources"]
    ).items():
        source["location"] = str(getattr(paths, _LOCATION_ATTRS[name]))
        schema_ref = cast(str, source.pop("schema_ref"))
        source["schema"] = yaml.safe_load(
            (_SCHEMA_DIR / Path(schema_ref).name).read_text(encoding="utf-8")
        )
    catalog_path = tmp_path / "shopfloor_semantic_layer.yaml"
    # ``yaml.safe_dump`` is typed as ``str | bytes | None``; with only
    # text-valued content and default (text) output it always returns ``str``.
    catalog_path.write_text(
        cast(str, yaml.safe_dump(catalog, sort_keys=False)), encoding="utf-8"
    )
    return catalog_path


def test_generate_shopfloor_data_writes_all_connector_inputs(tmp_path: Path) -> None:
    paths = generate_shopfloor_data(tmp_path / "data")

    with paths.customer_orders.open(newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 2
    with sqlite3.connect(paths.production_orders_db) as connection:
        assert connection.execute(
            "select count(*) from production_orders"
        ).fetchone() == (3,)
    with duckdb.connect(str(paths.shopfloor_db), read_only=True) as connection:
        assert connection.execute(
            "select count(*) from serialized_drives"
        ).fetchone() == (3,)

    assert pq.read_table(paths.component_consumption).num_rows == 6
    assert pq.read_table(paths.component_lot_inspections).num_rows == 5
    assert pq.read_table(paths.operation_executions).num_rows == 7
    assert pq.read_table(paths.machine_telemetry).num_rows == 4
    assert DeltaTable(paths.eol_test_runs).to_pyarrow_table().num_rows == 3


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
        assert engine.query(["eol_attempt_pass_rate", "first_pass_yield"]).row(
            0
        ) == pytest.approx((2 / 3, 2 / 3))
        assert engine.query(["alarm_event_count", "average_temperature_c"]).row(
            0
        ) == pytest.approx((1, 48.5))

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


def test_walkthrough_prints_the_planner_boundary_and_reload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_run_walkthrough_propagates_non_mixed_grain_planning_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-mixed-grain QueryPlanningError must not be swallowed by run_walkthrough.

    The walkthrough is only permitted to catch the intentional ``mixed_grain``
    rejection; every other planner error must propagate. The engine ``plan``
    boundary is monkeypatched to raise a deterministic ``unknown_metric`` error,
    exercising the handler's re-raise path without changing ``src/selayer``.
    """
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = SemanticLayer.load(_temporary_shopfloor_catalog(tmp_path, paths))

    with QueryEngine(layer) as engine:

        def raise_unknown_metric(
            metrics: list[str],
            dimensions: list[str] | None = None,
            filters: dict[str, object] | None = None,
        ) -> None:
            raise QueryPlanningError("unknown_metric", "simulated unknown metric")

        monkeypatch.setattr(engine, "plan", raise_unknown_metric)

        with pytest.raises(QueryPlanningError, match="unknown_metric"):
            run_walkthrough(engine, paths.eol_test_runs)


def test_main_prints_delta_setup_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    from examples.shopfloor import run_example

    def missing_delta(_: Path) -> ShopfloorDataPaths:
        raise DeltaDependencyError(
            "Delta support is required for the shop-floor example; run: uv sync --extra delta"
        )

    monkeypatch.setattr(run_example, "generate_shopfloor_data", missing_delta)
    with pytest.raises(SystemExit, match="uv sync --extra delta"):
        run_example.main()


def test_shopfloor_docs_match_the_runnable_contract() -> None:
    repo = Path(__file__).parents[2]
    shopfloor_readme = (repo / "examples/shopfloor/README.md").read_text(
        encoding="utf-8"
    )
    root_readme = (repo / "README.md").read_text(encoding="utf-8")

    assert "uv sync --extra delta" in shopfloor_readme
    assert "uv run python examples/shopfloor/run_example.py" in shopfloor_readme
    assert "mixed_grain" in shopfloor_readme
    assert "JSON" in shopfloor_readme
    assert "DuckLake" in shopfloor_readme
    assert "examples/shopfloor/README.md" in root_readme
