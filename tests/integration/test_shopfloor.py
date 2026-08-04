"""Integration tests for the shop-floor example.

The first test verifies that :func:`generate_shopfloor_data` materialises
every deterministic connector input (CSV, SQLite, DuckDB, Parquet, Delta Lake)
with the agreed row counts.  The second test loads the static semantic catalog
against the generated data and asserts every documented query answer plus the
grain-safe planner boundary.  No external services are required.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import tempfile
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
from examples.shopfloor.generate_data import main as generate_main
from examples.shopfloor.run_example import _layer_for_paths, run_walkthrough
from examples.shopfloor.run_example import main as run_main
from selayer import QueryEngine, QueryPlanningError, SemanticLayer
from selayer.okf import OkfBundle
from selayer.planning.types import QueryRequest
from selayer.verification import CompatibilityCheck, PhysicalCheck, verify

_REPO = Path(__file__).parents[2]
SHOPFLOOR_ROOT = _REPO / "examples" / "shopfloor"
SHOPFLOOR_CATALOG = SHOPFLOOR_ROOT / "shopfloor_semantic_layer.yaml"
_CATALOG = SHOPFLOOR_CATALOG
_SCHEMA_DIR = SHOPFLOOR_ROOT / "schemas"

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


def _delta_row_count(delta_path: Path) -> int:
    """Return the live row count of a generated Delta table."""
    return DeltaTable(delta_path).to_pyarrow_table().num_rows


def _logical_source_snapshot(data_dir: Path) -> dict[str, tuple[str, object]] | None:
    """Record relative filenames, logical row counts, and Delta versions.

    Deliberately avoids byte-identical database metadata so that re-opening a
    SQLite/DuckDB file that has not logically changed still compares equal.
    """
    if not data_dir.exists():
        return None
    snapshot: dict[str, tuple[str, object]] = {}
    for path in sorted(data_dir.iterdir()):
        name = path.name
        if path.suffix == ".csv":
            with path.open(newline="", encoding="utf-8") as stream:
                snapshot[name] = ("csv", len(list(csv.DictReader(stream))))
        elif path.suffix == ".sqlite":
            counts: dict[str, int] = {}
            with sqlite3.connect(path) as connection:
                tables = [
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    ).fetchall()
                ]
                for table in tables:
                    row = connection.execute(f"select count(*) from {table}").fetchone()
                    counts[table] = row[0] if row is not None else 0
            snapshot[name] = ("sqlite", counts)
        elif path.suffix == ".duckdb":
            duck_counts: dict[str, int] = {}
            with duckdb.connect(str(path), read_only=True) as connection:
                tables = [
                    row[0]
                    for row in connection.execute(
                        "select table_name from information_schema.tables"
                    ).fetchall()
                ]
                for table in tables:
                    row = connection.execute(f"select count(*) from {table}").fetchone()
                    duck_counts[table] = row[0] if row is not None else 0
            snapshot[name] = ("duckdb", duck_counts)
        elif path.suffix == ".parquet":
            snapshot[name] = ("parquet", pq.read_table(path).num_rows)
        elif path.suffix == ".delta":
            snapshot[name] = ("delta", DeltaTable(path).version())
    return snapshot


def _temp_factory(base: Path):
    """Return a TemporaryDirectory substitute rooted under ``base``."""

    def factory(*_args: object, **_kwargs: object) -> tempfile.TemporaryDirectory:
        return tempfile.TemporaryDirectory(dir=str(base))

    return factory


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


def test_telemetry_count_facts_are_event_markers() -> None:
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    assert layer.measure("telemetry_event_count").fact == "telemetry_event_machine_id"
    assert layer.measure("alarm_event_count_measure").fact == "alarm_event_machine_id"
    with pytest.raises(KeyError):
        layer.fact("telemetry_machine_id")
    with pytest.raises(KeyError):
        layer.fact("alarm_machine_id")


def test_shopfloor_planner_supports_intended_semantics(tmp_path: Path) -> None:
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = _layer_for_paths(SemanticLayer.load(SHOPFLOOR_CATALOG), paths)
    with QueryEngine(layer) as engine:
        engine.plan(["component_count"], ["drive_serial_number"])
        engine.plan(
            ["average_cycle_seconds"], ["drive_serial_number", "operation_machine_id"]
        )
        engine.plan(["eol_attempt_pass_rate"], ["drive_serial_number", "station_id"])
        engine.plan(["production_completion_rate"], ["requested_ship_date"])
        engine.plan(
            ["average_temperature_c"],
            ["telemetry_recorded_at", "telemetry_machine_id"],
        )

        with pytest.raises(QueryPlanningError) as raised:
            engine.plan(["average_cycle_seconds"], ["telemetry_machine_id"])
        assert raised.value.code == "no_relationship_path"

        with pytest.raises(QueryPlanningError) as raised:
            engine.plan(["average_temperature_c"], ["operation_machine_id"])
        assert raised.value.code == "no_relationship_path"


def test_layer_for_paths_rebases_every_source(tmp_path: Path) -> None:
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
        name: Path(cast("Any", source.connector).location)
        for name, source in rebased.data_sources.items()
    } == expected
    assert SemanticLayer.load(SHOPFLOOR_CATALOG).data_sources == original.data_sources


def test_runner_does_not_write_repository_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The walkthrough must generate and mutate only temporary data.

    The repository data directory is only ever snapshotted, never created by
    this test: if a developer has materialised ``examples/shopfloor/data`` the
    runner must leave it byte-for-byte identical, and if it is absent the
    runner must not create it. Temporary output is redirected under
    ``tmp_path`` so nothing escapes the test sandbox.
    """
    repository_data = SHOPFLOOR_ROOT / "data"
    before = _logical_source_snapshot(repository_data)
    monkeypatch.setattr(
        "examples.shopfloor.run_example.TemporaryDirectory",
        _temp_factory(tmp_path),
    )
    assert run_main() == 0
    after = _logical_source_snapshot(repository_data)
    assert after == before


def test_rebased_layer_reload_changes_only_attempt_rate(tmp_path: Path) -> None:
    """A rebased temporary layer reloads the temporary retest identically."""
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = _layer_for_paths(SemanticLayer.load(SHOPFLOOR_CATALOG), paths)

    with QueryEngine(layer) as engine:
        before = engine.query(["eol_attempt_pass_rate", "first_pass_yield"])
        append_eol_retest(paths.eol_test_runs)
        engine.reload_source("eol_test_runs")
        after = engine.query(["eol_attempt_pass_rate", "first_pass_yield"])

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
    assert "EOL quality before Delta reload:" in output
    assert "EOL source generation: 0 -> 1" in output
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


def test_main_returns_delta_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from examples.shopfloor import run_example

    def missing_delta(_: Path) -> ShopfloorDataPaths:
        raise DeltaDependencyError(
            "Delta support is required for the shop-floor example; run: uv sync --extra delta"
        )

    monkeypatch.setattr(run_example, "generate_shopfloor_data", missing_delta)
    assert run_example.main() == 1
    assert "uv sync --extra delta" in capsys.readouterr().err


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


#: Exact expected row counts per generated source, asserted both by the physical
#: grain audit and the direct fixture reads below.
_EXPECTED_SOURCE_ROWS: dict[str, int] = {
    "customer_orders": 2,
    "production_orders": 3,
    "serialized_drives": 3,
    "component_consumption": 6,
    "component_lot_inspections": 5,
    "operation_executions": 7,
    "machine_telemetry": 4,
    "eol_test_runs": 3,
}


def test_shopfloor_physical_audit_passes(tmp_path: Path) -> None:
    """Every declared grain and relationship passes an exact full-scan audit."""
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = _layer_for_paths(SemanticLayer.load(SHOPFLOOR_CATALOG), paths)
    report = verify(layer, PhysicalCheck())

    assert report.complete
    assert report.passed
    assert all(outcome.scope == "full_scan" for outcome in report.outcomes)

    grain_outcomes = {
        outcome.check_id: outcome
        for outcome in report.outcomes
        if outcome.check_id.startswith("source.")
    }
    assert set(grain_outcomes) == {
        f"source.{name}.grain" for name in _EXPECTED_SOURCE_ROWS
    }

    # Exact source row counts, and clean grain for every source.
    for name, expected_rows in _EXPECTED_SOURCE_ROWS.items():
        outcome = grain_outcomes[f"source.{name}.grain"]
        assert outcome.status == "passed"
        assert outcome.evidence["row_count"] == expected_rows
        assert outcome.evidence["null_grain_rows"] == 0
        assert outcome.evidence["duplicate_grain_groups"] == 0

    relationship_outcomes = [
        outcome
        for outcome in report.outcomes
        if outcome.check_id.startswith("relationship.")
    ]
    # The catalog declares exactly six safe relationships.
    assert len(relationship_outcomes) == 6
    for outcome in relationship_outcomes:
        assert outcome.status == "passed"
        assert outcome.evidence["orphan_non_null_rows"] == 0


def test_shopfloor_business_rules_hold_in_generated_data(tmp_path: Path) -> None:
    """Fixture-level data semantics not expressed by catalog version 1."""
    paths = generate_shopfloor_data(tmp_path / "data")

    # Production orders (SQLite): completed cannot exceed planned; schedule
    # domain is exactly the documented status set.
    with sqlite3.connect(paths.production_orders_db) as connection:
        connection.row_factory = sqlite3.Row
        production_orders = [
            (row["planned_units"], row["completed_units"], row["schedule_status"])
            for row in connection.execute(
                "select planned_units, completed_units, schedule_status "
                "from production_orders"
            )
        ]
    assert all(completed <= planned for planned, completed, _ in production_orders)
    assert {status for _, _, status in production_orders} <= {
        "on_time",
        "late",
        "open",
    }

    # Serialized drives (DuckDB, read-only): shipment domain.
    with duckdb.connect(str(paths.shopfloor_db), read_only=True) as connection:
        shipment_statuses = [
            row[0]
            for row in connection.execute(
                "select shipment_status from serialized_drives"
            ).fetchall()
        ]
    assert set(shipment_statuses) <= {"shipped", "in_stock"}

    # End-of-line attempts (Delta): attempt numbering, uniqueness, first-pass
    # marker logic, and result domain.
    eol_runs = DeltaTable(paths.eol_test_runs).to_pyarrow_table().to_pylist()
    assert all(cast("int", row["attempt"]) >= 1 for row in eol_runs)
    assert len(
        {
            (cast("str", row["serial_number"]), cast("int", row["attempt"]))
            for row in eol_runs
        }
    ) == len(eol_runs)
    assert all(
        row["is_first_pass"]
        == (cast("int", row["attempt"]) == 1 and row["result"] == "pass")
        for row in eol_runs
    )
    assert {cast("str", row["result"]) for row in eol_runs} <= {"pass", "fail"}

    # Operation executions (Parquet): result domain.
    operation_executions = pq.read_table(paths.operation_executions).to_pylist()
    assert {cast("str", row["result"]) for row in operation_executions} <= {
        "pass",
        "fail",
    }

    # Machine telemetry (Parquet): machine-state domain.
    telemetry = pq.read_table(paths.machine_telemetry).to_pylist()
    assert {cast("str", row["machine_state"]) for row in telemetry} <= {
        "running",
        "idle",
        "alarm",
    }

    # Component lot inspections (Parquet): incoming result maps to disposition.
    inspections = pq.read_table(paths.component_lot_inspections).to_pylist()
    assert all(
        (row["incoming_result"], row["disposition"])
        in {("pass", "released"), ("fail", "quarantined")}
        for row in inspections
    )


def test_shopfloor_compatibility_check_records_planner_rejections(
    tmp_path: Path,
) -> None:
    """A documented mixed-grain rejection is an observed planner result.

    The verification check completes (``passed``) even when a request is
    incompatible: the outcome records ``compatible: False`` with the stable
    ``planner_code`` instead of failing the report.
    """
    paths = generate_shopfloor_data(tmp_path / "data")
    layer = _layer_for_paths(SemanticLayer.load(SHOPFLOOR_CATALOG), paths)
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
        item.evidence.get("planner_code") == "mixed_grain" for item in report.outcomes
    )


def test_business_context_is_four_valid_reference_concepts(tmp_path: Path) -> None:
    """The authored business context composes as four Reference concepts.

    Each reference must declare ``type: Reference`` and must not bind a
    ``selayer_id`` (references are advisory, never execution authority). The
    OKF composer publishes authored references under the canonical
    ``references/`` namespace regardless of the input directory name.
    """
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
        "references/glossary",
        "references/kpi_definitions",
        "references/process_overview",
        "references/quality_policy",
    }
    assert all(
        concept.frontmatter["type"] == "Reference" for concept in references.values()
    )
    assert all(
        "selayer_id" not in concept.frontmatter for concept in references.values()
    )


def test_every_metric_has_complete_curated_overlay(tmp_path: Path) -> None:
    """Every headline metric has a curated overlay with four non-empty sections."""
    output = tmp_path / "knowledge"
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    bundle = OkfBundle.build(
        layer,
        output,
        references_dir=SHOPFLOOR_ROOT / "business_context",
        overlays_dir=SHOPFLOOR_ROOT / "okf_overlays",
    )
    for metric_name in sorted(layer.metrics):
        concept = bundle.concepts[f"metrics/{metric_name}"]
        sections = {
            section.title: section.content.strip() for section in concept.sections
        }
        assert sections["Usage Guidance"]
        assert sections["Examples"]
        assert sections["Caveats"]
        assert sections["Related Concepts"]


_QUERY_FENCE = re.compile(
    r"```json selayer-query\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_ALLOWED_QUERY_KEYS = frozenset({"metrics", "dimensions", "filters"})

#: Exact expected query dimensions per headline metric overlay.
_EXPECTED_METRIC_DIMENSIONS: dict[str, list[str]] = {
    "production_completion_rate": ["schedule_status", "requested_ship_date"],
    "shipped_unit_count": ["customer_region", "product_model"],
    "component_count": ["drive_serial_number", "component_lot_id"],
    "incoming_acceptance_rate": ["supplier_name", "component_type"],
    "average_cycle_seconds": [
        "operation_line_id",
        "operation_machine_id",
        "shift",
        "operation_name",
    ],
    "operation_count": [
        "operation_line_id",
        "operation_machine_id",
        "shift",
        "operation_name",
    ],
    "rework_rate": [
        "operation_line_id",
        "operation_machine_id",
        "shift",
        "operation_name",
    ],
    "energy_per_operation_kwh": [
        "operation_line_id",
        "operation_machine_id",
        "shift",
        "operation_name",
    ],
    "eol_attempt_pass_rate": [
        "drive_serial_number",
        "station_id",
        "product_model",
        "firmware_revision",
    ],
    "first_pass_yield": [
        "drive_serial_number",
        "station_id",
        "product_model",
        "firmware_revision",
    ],
    "alarm_event_count": [
        "telemetry_line_id",
        "telemetry_machine_id",
        "machine_state",
    ],
    "average_temperature_c": [
        "telemetry_line_id",
        "telemetry_machine_id",
        "machine_state",
    ],
}


def test_metric_overlay_query_blocks_are_valid_and_exact(tmp_path: Path) -> None:
    """Each metric overlay Examples section has exactly one safe query request.

    Declarative validation only: parses the fenced ``json selayer-query`` block
    in each composed metric concept and asserts a well-formed request with the
    exact expected grouping dimensions. No query execution occurs.
    """
    output = tmp_path / "knowledge"
    layer = SemanticLayer.load(SHOPFLOOR_CATALOG)
    bundle = OkfBundle.build(
        layer,
        output,
        references_dir=SHOPFLOOR_ROOT / "business_context",
        overlays_dir=SHOPFLOOR_ROOT / "okf_overlays",
    )
    for metric_name in sorted(layer.metrics):
        concept = bundle.concepts[f"metrics/{metric_name}"]
        examples = next(
            section.content
            for section in concept.sections
            if section.title == "Examples"
        )
        matches = tuple(_QUERY_FENCE.finditer(examples))
        assert len(matches) == 1, f"{metric_name} must contain one query request"
        payload = json.loads(matches[0].group("body"))
        assert type(payload) is dict, f"{metric_name} query must be a JSON object"
        assert set(payload) - _ALLOWED_QUERY_KEYS == set(), (
            f"{metric_name} query has invalid keys"
        )
        metrics = payload["metrics"]
        assert (
            type(metrics) is list and metrics and all(type(m) is str for m in metrics)
        ), f"{metric_name} metrics must be a non-empty list of strings"
        dimensions = payload["dimensions"]
        assert type(dimensions) is list and all(type(d) is str for d in dimensions), (
            f"{metric_name} dimensions must be a list of strings"
        )
        assert type(payload["filters"]) is dict, f"{metric_name} filters must be a dict"
        expected = _EXPECTED_METRIC_DIMENSIONS[metric_name]
        assert dimensions == expected, (
            f"{metric_name} dimensions {dimensions} != {expected}"
        )
