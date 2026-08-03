from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from selayer import QueryEngine, QueryPlanningError, SemanticLayer


def _write_fixture_parquet(
    path: Path, schema: pa.Schema, columns: dict[str, object]
) -> None:
    """Write a fixture parquet whose physical schema matches ``schema`` exactly.

    Columns are typed against ``schema`` (``pa.string`` rather than Polars'
    ``large_string``) so the registry's ``compare_schemas`` physical-drift check
    observes the declared Arrow types and reports no false
    ``utf8``/``large_utf8`` type mismatch.  ``compare_schemas`` is *not* weakened:
    the fixture data is simply written with PyArrow against the declared schema.
    """
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array(columns[field.name], field.type) for field in schema],
            schema=schema,
        ),
        path,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Create deterministic parquet fixtures and a catalog pointing at them."""
    data = tmp_path / "data"
    data.mkdir()
    # Every fixture parquet carries the full physical column set and order
    # declared in ``examples/e_commerce/schemas/*.yaml``.  They are written
    # with PyArrow against an explicit ``pa.string``/``pa.float64``/... schema
    # (not Polars) so the on-disk physical schema reads back as the declared
    # Arrow types: Polars writes strings as ``large_string``, which the
    # registry's ``compare_schemas`` physical-drift check correctly flags as a
    # ``utf8``/``large_utf8`` type mismatch.  Timestamps are tz-naive ``ns`` to
    # match the declared ``timestamp: {unit: ns}``.
    _write_fixture_parquet(
        data / "orders.parquet",
        pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("customer_id", pa.string()),
                pa.field("created_at", pa.timestamp("ns")),
                pa.field("status", pa.string()),
                pa.field("payment_method", pa.string()),
                pa.field("shipping_cost", pa.float64()),
                pa.field("discount_code", pa.string()),
                pa.field("discount_amount", pa.float64()),
                pa.field("reason", pa.string()),
                pa.field("is_first_purchase", pa.bool_()),
                pa.field("amount", pa.float64()),
                pa.field("total_amount", pa.float64()),
            ]
        ),
        {
            "id": ["O1", "O2", "O3"],
            "customer_id": ["C1", "C1", "C2"],
            "created_at": [
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
                datetime(2025, 1, 1, tzinfo=UTC),
            ],
            "status": ["completed", "processing", "completed"],
            "payment_method": ["card", "cash", "card"],
            "shipping_cost": [15.0, 3.0, 10.0],
            "discount_code": ["WELCOME", None, "SAVE10"],
            "discount_amount": [10.0, 5.0, 20.0],
            "reason": [None, "Changed mind", None],
            "is_first_purchase": [True, False, False],
            "amount": [100.0, 50.0, 200.0],
            "total_amount": [105.0, 48.0, 190.0],
        },
    )
    _write_fixture_parquet(
        data / "order_items.parquet",
        pa.schema(
            [
                pa.field("order_id", pa.string(), nullable=False),
                pa.field("product_id", pa.string(), nullable=False),
                pa.field("quantity", pa.int64()),
                pa.field("price", pa.float64()),
                pa.field("total", pa.float64()),
            ]
        ),
        {
            "order_id": ["O1", "O1", "O2", "O3"],
            "product_id": ["P1", "P2", "P1", "P3"],
            "quantity": [2, 1, 1, 4],
            "price": [30.0, 40.0, 50.0, 50.0],
            "total": [60.0, 40.0, 50.0, 200.0],
        },
    )
    _write_fixture_parquet(
        data / "products.parquet",
        pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("name", pa.string()),
                pa.field("category", pa.string()),
                pa.field("subcategory", pa.string()),
                pa.field("base_price", pa.float64()),
                pa.field("cost", pa.float64()),
                pa.field("in_stock", pa.int64()),
                pa.field("supplier_id", pa.int64()),
                pa.field("created_at", pa.timestamp("ns")),
                pa.field("is_active", pa.bool_()),
            ]
        ),
        {
            "id": ["P1", "P2", "P3"],
            "name": ["Widget", "Gadget", "Gizmo"],
            "category": ["Books", "Electronics", "Books"],
            "subcategory": ["Fiction", "Audio", "Reference"],
            "base_price": [25.0, 30.0, 35.0],
            "cost": [20.0, 25.0, 30.0],
            "in_stock": [10, 5, 8],
            "supplier_id": [1, 2, 3],
            "created_at": [
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 1, 2, tzinfo=UTC),
                datetime(2024, 1, 3, tzinfo=UTC),
            ],
            "is_active": [True, True, True],
        },
    )
    _write_fixture_parquet(
        data / "customers.parquet",
        pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("email", pa.string()),
                pa.field("name", pa.string()),
                pa.field("segment", pa.string()),
                pa.field("acquisition_channel", pa.string()),
                pa.field("registration_date", pa.timestamp("ns")),
                pa.field("country", pa.string()),
                pa.field("city", pa.string()),
                pa.field("is_active", pa.bool_()),
                pa.field("lifetime_value", pa.float64()),
            ]
        ),
        {
            "id": ["C1", "C2"],
            "email": ["c1@example.com", "c2@example.com"],
            "name": ["Alice", "Bob"],
            "segment": ["retail", "enterprise"],
            "acquisition_channel": ["Direct", "Referral"],
            "registration_date": [
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 6, 1, tzinfo=UTC),
            ],
            "country": ["US", "CA"],
            "city": ["NYC", "Toronto"],
            "is_active": [True, True],
            "lifetime_value": [500.0, 1500.0],
        },
    )

    catalog = cast(
        dict[str, Any],
        yaml.safe_load(
            (Path(__file__).parents[2] / "ecommerce_semantic_layer.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )
    repo = Path(__file__).parents[2]
    for name in ("orders", "order_items", "products", "customers"):
        source = catalog["data_sources"][name]
        source["location"] = str(data / f"{name}.parquet")
        # The relocated catalog cannot resolve schema_ref relative to tmp_path,
        # so inline the referenced schema document from the repository.
        schema_ref = cast(str, source.pop("schema_ref"))
        source["schema"] = yaml.safe_load(
            (repo / schema_ref).read_text(encoding="utf-8")
        )
    catalog_path = tmp_path / "ecommerce_semantic_layer.yaml"
    catalog_text = cast(str, yaml.safe_dump(catalog, sort_keys=False))
    catalog_path.write_text(catalog_text, encoding="utf-8")
    return tmp_path


def expected_product_metrics(root: Path) -> pl.DataFrame:
    items = pl.read_parquet(root / "data/order_items.parquet")
    products = pl.read_parquet(root / "data/products.parquet")
    return (
        items.join(products, left_on="product_id", right_on="id")
        .with_columns((pl.col("quantity") * pl.col("cost")).alias("item_cost"))
        .group_by("category")
        .agg(
            pl.col("total").sum().alias("total_item_revenue"),
            pl.col("item_cost").sum().alias("total_item_cost"),
            pl.col("quantity").sum().alias("units_sold"),
        )
        .with_columns(
            (pl.col("total_item_revenue") / pl.col("units_sold")).alias(
                "average_item_price"
            ),
            (
                (pl.col("total_item_revenue") - pl.col("total_item_cost"))
                / pl.col("total_item_revenue")
            ).alias("gross_margin"),
        )
        .sort("category")
    )


def test_cross_kind_local_names_execute_with_distinct_internal_aliases(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "events.csv"
    pl.DataFrame({"total": [10, 10, 20], "value": [10, 10, 20]}).write_csv(data_path)
    catalog_path = tmp_path / "colliding_names.yaml"
    catalog_path.write_text(
        cast(
            str,
            yaml.safe_dump(
                {
                    "version": 1,
                    "name": "colliding_names",
                    "data_sources": {
                        "events": {
                            "type": "csv",
                            "location": str(data_path),
                            "grain": ["total", "value"],
                            "schema": {
                                "fields": [
                                    {
                                        "name": "total",
                                        "type": "int64",
                                        "nullable": False,
                                    },
                                    {
                                        "name": "value",
                                        "type": "int64",
                                        "nullable": False,
                                    },
                                ]
                            },
                        }
                    },
                    "dimensions": {
                        "total": {
                            "source": "events",
                            "column": "total",
                            "data_type": "integer",
                        }
                    },
                    "facts": {
                        "total": {
                            "source": "events",
                            "data_type": "integer",
                            "expression": "events.value",
                        }
                    },
                    "measures": {"total": {"fact": "total", "aggregation": "sum"}},
                    "metrics": {
                        "m": {"measures": ["total"], "expression": "total"},
                        "total": {"measures": ["total"], "expression": "total"},
                    },
                    "relationships": {},
                },
                sort_keys=False,
            ),
        ),
        encoding="utf-8",
    )
    layer = SemanticLayer.load(catalog_path)

    with QueryEngine(layer) as engine:
        result = engine.query(["m"], ["total"]).sort("total")

        assert result.columns == ["total", "m"]
        assert result["total"].to_list() == [10, 20]
        assert result["m"].to_list() == [20, 20]

        with pytest.raises(QueryPlanningError) as caught:
            engine.plan(["total"], ["total"])

    assert caught.value.code == "duplicate_output_name"
    assert caught.value.message == (
        "requested dimension and metric share output name 'total'"
    )


def test_catalog_is_schema_version_one_and_executes_from_repo_path() -> None:
    repo = Path(__file__).parents[2]
    layer = SemanticLayer.load(repo / "ecommerce_semantic_layer.yaml")
    assert layer.version == 1
    assert all(source.grain for source in layer.data_sources.values())
    assert layer.facts["item_cost"].source == "order_items"
    with QueryEngine(layer) as engine:
        actual = engine.query(["gross_margin"], ["product_category"]).sort(
            "product_category"
        )
    expected = expected_product_metrics(repo)
    assert actual["product_category"].to_list() == expected["category"].to_list()
    assert actual["gross_margin"].to_list() == pytest.approx(
        expected["gross_margin"].to_list()
    )


def test_product_category_metrics_match_independent_polars(root: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(
        [
            "total_item_revenue",
            "total_item_cost",
            "units_sold",
            "average_item_price",
            "gross_margin",
        ],
        ["product_category"],
    ).sort("product_category")
    expected = expected_product_metrics(root)
    assert actual["product_category"].to_list() == expected["category"].to_list()
    for column in (
        "total_item_revenue",
        "total_item_cost",
        "units_sold",
        "average_item_price",
        "gross_margin",
    ):
        assert actual[column].to_list() == pytest.approx(expected[column].to_list())


def test_overall_order_metrics_match_independent_polars(root: Path) -> None:
    orders = pl.read_parquet(root / "data/orders.parquet")
    revenue = float(orders["amount"].sum() or 0)
    expected = {
        "total_revenue": revenue,
        "order_count": orders.height,
        "average_order_value": float(cast(Any, orders["amount"].mean()) or 0),
        "discount_rate": float(orders["discount_amount"].sum() or 0) / revenue,
        "order_completion_rate": (
            orders.filter(pl.col("status") == "completed").height / orders.height
        ),
    }
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(list(expected), ["order_status"]).select(list(expected))
    totals = engine.query(list(expected))
    for column, value in expected.items():
        assert totals[column].item() == pytest.approx(value)
    assert actual.height == orders["status"].n_unique()


def test_item_metrics_by_customer_and_order_date_match_polars(root: Path) -> None:
    items = pl.read_parquet(root / "data/order_items.parquet")
    orders = pl.read_parquet(root / "data/orders.parquet")
    products = pl.read_parquet(root / "data/products.parquet")
    customers = pl.read_parquet(root / "data/customers.parquet")
    expected = (
        items.join(products.select("id", "cost"), left_on="product_id", right_on="id")
        .join(
            orders.select("id", "customer_id", "created_at"),
            left_on="order_id",
            right_on="id",
        )
        .join(customers.select("id", "segment"), left_on="customer_id", right_on="id")
        .with_columns((pl.col("quantity") * pl.col("cost")).alias("item_cost"))
        .group_by("segment", "created_at")
        .agg(
            pl.col("total").sum().alias("total_item_revenue"),
            pl.col("item_cost").sum().alias("total_item_cost"),
        )
        .with_columns(
            (
                (pl.col("total_item_revenue") - pl.col("total_item_cost"))
                / pl.col("total_item_revenue")
            ).alias("gross_margin")
        )
        .sort("segment", "created_at")
    )
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(
        ["gross_margin", "total_item_revenue"], ["customer_segment", "order_date"]
    ).sort("customer_segment", "order_date")
    assert actual["customer_segment"].to_list() == expected["segment"].to_list()
    assert actual["order_date"].to_list() == expected["created_at"].to_list()
    assert actual["gross_margin"].to_list() == pytest.approx(
        expected["gross_margin"].to_list()
    )
    assert actual["total_item_revenue"].to_list() == pytest.approx(
        expected["total_item_revenue"].to_list()
    )


def test_filtered_item_results_are_numerically_correct(root: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(
        ["total_item_revenue", "units_sold", "gross_margin"],
        ["product_category"],
        {"product_category": "Books"},
    )
    assert actual["product_category"].to_list() == ["Books"]
    assert actual["total_item_revenue"].item() == pytest.approx(310.0)
    assert actual["units_sold"].item() == pytest.approx(7)
    assert actual["gross_margin"].item() == pytest.approx(130 / 310)


# These sets are deliberately explicit: the exhaustive cases below are generated
# from the loaded catalog, and this equality check makes a catalog addition fail
# until its independently calculated expectations and classification are added.
_ORDER_METRICS = {
    "total_revenue",
    "order_count",
    "total_discount",
    "total_shipping",
    "completed_order_count",
    "average_order_value",
    "discount_rate",
    "order_completion_rate",
}
_ITEM_METRICS = {
    "total_item_revenue",
    "total_item_cost",
    "units_sold",
    "average_item_price",
    "gross_margin",
}
_CUSTOMER_DIMENSIONS = {"customer_id", "customer_segment", "customer_country"}
_PRODUCT_DIMENSIONS = {"product_id", "product_category", "product_subcategory"}
_ORDER_DIMENSIONS = {"order_date", "order_status", "payment_method"}


def _expected_metric_by_dimension(
    root: Path, metric: str, dimension: str
) -> pl.DataFrame:
    """Calculate one complete result using Polars, independently of selayer."""
    orders = pl.read_parquet(root / "data/orders.parquet")
    dimension_columns = {
        "customer_id": "customer_id",
        "customer_segment": "segment",
        "customer_country": "country",
        "product_id": "product_id",
        "product_category": "category",
        "product_subcategory": "subcategory",
        "order_date": "created_at",
        "order_status": "status",
        "payment_method": "payment_method",
    }
    if metric in _ORDER_METRICS:
        frame = orders
    else:
        items = pl.read_parquet(root / "data/order_items.parquet")
        products = pl.read_parquet(root / "data/products.parquet")
        customers = pl.read_parquet(root / "data/customers.parquet")
        frame = (
            items.join(
                products.select("id", "category", "subcategory", "cost"),
                left_on="product_id",
                right_on="id",
            )
            .join(orders, left_on="order_id", right_on="id", suffix="_order")
            .join(
                customers.select("id", "segment", "country"),
                left_on="customer_id",
                right_on="id",
                suffix="_customer",
            )
            .with_columns((pl.col("quantity") * pl.col("cost")).alias("item_cost"))
        )
    if dimension in _CUSTOMER_DIMENSIONS and metric in _ORDER_METRICS:
        frame = frame.join(
            pl.read_parquet(root / "data/customers.parquet"),
            left_on="customer_id",
            right_on="id",
            suffix="_customer",
        )
    source_column = dimension_columns[dimension]
    if metric in _ORDER_METRICS:
        grouped = frame.group_by(source_column).agg(
            pl.col("amount").sum().alias("total_revenue"),
            pl.col("id").n_unique().alias("order_count"),
            pl.col("discount_amount").sum().alias("total_discount"),
            pl.col("shipping_cost").sum().alias("total_shipping"),
            pl.col("id")
            .filter(pl.col("status") == "completed")
            .n_unique()
            .alias("completed_order_count"),
        )
        grouped = grouped.with_columns(
            (pl.col("total_revenue") / pl.col("order_count")).alias(
                "average_order_value"
            ),
            (pl.col("total_discount") / pl.col("total_revenue")).alias("discount_rate"),
            (pl.col("completed_order_count") / pl.col("order_count")).alias(
                "order_completion_rate"
            ),
        )
    else:
        grouped = frame.group_by(source_column).agg(
            pl.col("total").sum().alias("total_item_revenue"),
            pl.col("item_cost").sum().alias("total_item_cost"),
            pl.col("quantity").sum().alias("units_sold"),
        )
        grouped = grouped.with_columns(
            (pl.col("total_item_revenue") / pl.col("units_sold")).alias(
                "average_item_price"
            ),
            (
                (pl.col("total_item_revenue") - pl.col("total_item_cost"))
                / pl.col("total_item_revenue")
            ).alias("gross_margin"),
        )
    return grouped.select(pl.col(source_column).alias(dimension), pl.col(metric)).sort(
        dimension
    )


def _metric_anchor(layer: SemanticLayer, metric: str) -> str:
    measure = layer.metrics[metric].measures[0]
    return layer.facts[layer.measures[measure].fact].source


def test_every_catalog_metric_dimension_pair_is_numerically_verified(
    root: Path,
) -> None:
    layer = SemanticLayer.load(root / "ecommerce_semantic_layer.yaml")
    assert set(layer.metrics) == _ORDER_METRICS | _ITEM_METRICS
    assert set(layer.dimensions) == (
        _CUSTOMER_DIMENSIONS | _PRODUCT_DIMENSIONS | _ORDER_DIMENSIONS
    )
    engine = QueryEngine(layer)
    for metric in layer.metrics:
        for dimension in layer.dimensions:
            is_fan_out = metric in _ORDER_METRICS and dimension in _PRODUCT_DIMENSIONS
            if is_fan_out:
                with pytest.raises(QueryPlanningError) as error:
                    engine.plan([metric], [dimension])
                assert error.value.code == "row_expanding_path"
                continue
            actual = engine.query([metric], [dimension]).sort(dimension)
            expected = _expected_metric_by_dimension(root, metric, dimension)
            assert actual.columns == [dimension, metric]
            assert actual.height == expected.height
            assert actual[dimension].to_list() == expected[dimension].to_list()
            assert actual[metric].to_list() == pytest.approx(
                expected[metric].to_list()
            ), f"{metric} by {dimension}"


def test_every_mixed_order_item_metric_pair_has_stable_error(root: Path) -> None:
    layer = SemanticLayer.load(root / "ecommerce_semantic_layer.yaml")
    order_metrics = [
        metric for metric in layer.metrics if _metric_anchor(layer, metric) == "orders"
    ]
    item_metrics = [
        metric
        for metric in layer.metrics
        if _metric_anchor(layer, metric) == "order_items"
    ]
    assert set(order_metrics) == _ORDER_METRICS
    assert set(item_metrics) == _ITEM_METRICS
    engine = QueryEngine(layer)
    for order_metric in order_metrics:
        for item_metric in item_metrics:
            with pytest.raises(QueryPlanningError) as error:
                engine.plan([order_metric, item_metric])
            assert error.value.code == "mixed_grain"
