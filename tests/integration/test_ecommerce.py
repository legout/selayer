from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import yaml

from selayer import QueryEngine, QueryPlanningError, SemanticLayer


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Create deterministic parquet fixtures and a catalog pointing at them."""
    data = tmp_path / "data"
    data.mkdir()
    pl.DataFrame(
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
            "discount_amount": [10.0, 5.0, 20.0],
            "amount": [100.0, 50.0, 200.0],
            "total_amount": [105.0, 48.0, 190.0],
        }
    ).write_parquet(data / "orders.parquet")
    pl.DataFrame(
        {
            "order_id": ["O1", "O1", "O2", "O3"],
            "product_id": ["P1", "P2", "P1", "P3"],
            "quantity": [2, 1, 1, 4],
            "price": [30.0, 40.0, 50.0, 50.0],
            "total": [60.0, 40.0, 50.0, 200.0],
        }
    ).write_parquet(data / "order_items.parquet")
    pl.DataFrame(
        {
            "id": ["P1", "P2", "P3"],
            "category": ["Books", "Electronics", "Books"],
            "subcategory": ["Fiction", "Audio", "Reference"],
            "cost": [20.0, 25.0, 30.0],
        }
    ).write_parquet(data / "products.parquet")
    pl.DataFrame(
        {
            "id": ["C1", "C2"],
            "segment": ["retail", "enterprise"],
            "country": ["US", "CA"],
        }
    ).write_parquet(data / "customers.parquet")

    catalog = cast(
        dict[str, Any],
        yaml.safe_load(
            (Path(__file__).parents[2] / "ecommerce_semantic_layer.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )
    for name in ("orders", "order_items", "products", "customers"):
        catalog["data_sources"][name]["path"] = str(data / f"{name}.parquet")
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
        ],        ["product_category"],
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
        "average_order_value": float(
            cast(Any, orders["amount"].mean()) or 0
        ),
        "discount_rate": float(orders["discount_amount"].sum() or 0) / revenue,
        "order_completion_rate": (
            orders.filter(pl.col("status") == "completed").height / orders.height
        ),
    }
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(
        list(expected), ["order_status"]
    ).select(list(expected))
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
        items.join(products, left_on="product_id", right_on="id")
        .join(orders, left_on="order_id", right_on="id")
        .join(customers, left_on="customer_id", right_on="id")
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


@pytest.mark.parametrize(
    ("metrics", "dimensions", "error_code"),
    [
        (("average_order_value", "gross_margin"), (), "mixed_grain"),
        (("average_order_value",), ("product_category",), "row_expanding_path"),
    ],
)
def test_unsupported_example_combinations_have_stable_errors(
    root: Path,
    metrics: tuple[str, ...],
    dimensions: tuple[str, ...],
    error_code: str,
) -> None:
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    with pytest.raises(QueryPlanningError) as error:
        engine.plan(list(metrics), list(dimensions))
    assert error.value.code == error_code


@pytest.mark.parametrize(
    ("metrics", "dimensions"),
    [
        (("average_order_value",), ("customer_segment",)),
        (("average_order_value",), ("order_date",)),
        (("gross_margin",), ("product_category",)),
        (("gross_margin",), ("customer_segment",)),
        (("average_item_price",), ("order_date",)),
        (("units_sold",), ("customer_country",)),
    ],
)
def test_supported_metric_dimension_combinations_execute(
    root: Path, metrics: tuple[str, ...], dimensions: tuple[str, ...]
) -> None:
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    result = engine.query(list(metrics), list(dimensions))
    assert result.columns == [*dimensions, *metrics]
    assert result.height > 0
