"""Shared fixtures for the immutable catalog and query tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.sources.profiles import MappingArrowProviderResolver

VALID_CATALOG_YAML = """\
version: 1
name: ecommerce
label: E-commerce Analytics
description: Semantic model for the example store
data_sources:
  orders:
    type: parquet
    location: data/orders.parquet
    grain: [id]
    schema:
      fields:
        - {name: id, type: utf8, nullable: false}
        - {name: customer_id, type: utf8, nullable: true}
        - name: created_at
          type:
            timestamp: {unit: ns}
          nullable: true
        - {name: status, type: utf8, nullable: true}
        - {name: payment_method, type: utf8, nullable: true}
        - {name: shipping_cost, type: float64, nullable: true}
        - {name: discount_code, type: utf8, nullable: true}
        - {name: discount_amount, type: float64, nullable: true}
        - {name: reason, type: utf8, nullable: true}
        - {name: is_first_purchase, type: boolean, nullable: true}
        - {name: amount, type: float64, nullable: true}
        - {name: total_amount, type: float64, nullable: true}
  order_items:
    type: parquet
    location: data/order_items.parquet
    grain: [order_id, product_id]
    schema:
      fields:
        - {name: order_id, type: utf8, nullable: false}
        - {name: product_id, type: utf8, nullable: false}
        - {name: quantity, type: int64, nullable: true}
        - {name: price, type: float64, nullable: true}
        - {name: total, type: float64, nullable: true}
  products:
    type: parquet
    location: data/products.parquet
    grain: [id]
    schema:
      fields:
        - {name: id, type: utf8, nullable: false}
        - {name: name, type: utf8, nullable: true}
        - {name: category, type: utf8, nullable: true}
        - {name: subcategory, type: utf8, nullable: true}
        - {name: base_price, type: float64, nullable: true}
        - {name: cost, type: float64, nullable: true}
        - {name: in_stock, type: int64, nullable: true}
        - {name: supplier_id, type: int64, nullable: true}
        - name: created_at
          type:
            timestamp: {unit: ns}
          nullable: true
        - {name: is_active, type: boolean, nullable: true}
dimensions:
  product_category:
    source: products
    column: category
    data_type: string
    description: Product category
  order_date:
    source: orders
    column: created_at
    data_type: timestamp
    description: Order creation time
facts:
  item_revenue:
    source: order_items
    expression: order_items.total
    data_type: decimal
    description: Revenue recorded on one order item
  item_cost:
    source: order_items
    expression: order_items.quantity * products.cost
    data_type: decimal
    description: Extended product cost for one order item
measures:
  total_item_revenue:
    fact: item_revenue
    aggregation: sum
    description: Item revenue
  total_item_cost:
    fact: item_cost
    aggregation: sum
    description: Extended item cost
metrics:
  gross_margin:
    expression: >-
      (total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)
    measures: [total_item_revenue, total_item_cost]
    description: Gross margin ratio
relationships:
  product_order_items:
    source: products
    target: order_items
    type: one_to_many
    source_column: id
    target_column: product_id
"""


@pytest.fixture
def root() -> Path:
    """Return the repository root for integration fixtures."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def valid_catalog_path(tmp_path: Path) -> Path:
    """A path to a fully valid schema-version-1 catalog file."""
    path = tmp_path / "layer.yaml"
    path.write_text(VALID_CATALOG_YAML, encoding="utf-8")
    return path


@pytest.fixture
def require_docker() -> None:
    """Fail in CI when Docker is unavailable; skip that setup locally."""

    try:
        import docker

        available = bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001
        available = False
    if available:
        return
    if os.environ.get("CI") == "true":
        raise RuntimeError("Docker is unavailable in CI")
    pytest.skip("Docker daemon is not available")


@pytest.fixture
def valid_layer(valid_catalog_path: Path) -> SemanticLayer:
    """A loaded valid semantic layer backed by the example parquet fixtures."""
    return SemanticLayer.load(valid_catalog_path)


@pytest.fixture
def arrow_providers() -> MappingArrowProviderResolver:
    """An empty arrow-provider resolver for catalogs without pyarrow sources."""
    return MappingArrowProviderResolver({})
