"""Shared fixtures for the immutable catalog tests."""

from __future__ import annotations

from pathlib import Path

import pytest

VALID_CATALOG_YAML = """\
version: 1
name: ecommerce
label: E-commerce Analytics
description: Semantic model for the example store
data_sources:
  orders:
    type: parquet
    path: data/orders.parquet
    grain: [id]
  order_items:
    type: parquet
    path: data/order_items.parquet
    grain: [order_id, product_id]
  products:
    type: parquet
    path: data/products.parquet
    grain: [id]
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
def valid_catalog_path(tmp_path: Path) -> Path:
    """A path to a fully valid schema-version-1 catalog file."""
    path = tmp_path / "layer.yaml"
    path.write_text(VALID_CATALOG_YAML, encoding="utf-8")
    return path
