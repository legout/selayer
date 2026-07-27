from pathlib import Path

import polars as pl
import pytest

from selayer import (
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
)


@pytest.fixture
def semantic_layer(tmp_path: Path) -> SemanticLayer:
    customers_path = tmp_path / "customers.parquet"
    orders_path = tmp_path / "orders.parquet"
    pl.DataFrame(
        {
            "id": [1, 2],
            "name": ["O'Reilly", "Example Corp"],
        }
    ).write_parquet(customers_path)
    pl.DataFrame(
        {
            "id": [10, 11, 12],
            "customer_id": [1, 1, 2],
            "amount": [10.0, 20.0, 5.0],
        }
    ).write_parquet(orders_path)

    layer = SemanticLayer(name="test", description="Test semantic layer")
    layer.add_data_source(
        DataSource(name="customers", type="parquet", path=str(customers_path))
    )
    layer.add_data_source(
        DataSource(name="orders", type="parquet", path=str(orders_path))
    )
    layer.add_fact(
        Fact(
            name="order_amount",
            description="Order amount",
            data_type="number",
            source="orders",
            column="amount",
        )
    )
    layer.add_measure(
        Measure(
            name="total_amount",
            description="Total order amount",
            fact="order_amount",
            aggregation="sum",
        )
    )
    layer.add_metric(
        Metric(
            name="revenue",
            description="Revenue",
            expression="{{total_amount}}",
            measures=["total_amount"],
        )
    )
    layer.add_dimension(
        Dimension(
            name="customer_id",
            description="Customer identifier",
            data_type="integer",
            source="customers",
            column="id",
        )
    )
    layer.add_dimension(
        Dimension(
            name="customer_name",
            description="Customer name",
            data_type="string",
            source="customers",
            column="name",
        )
    )
    layer.add_relationship(
        Relationship(
            name="orders_customers",
            source="orders",
            target="customers",
            type="many_to_one",
            source_column="customer_id",
            target_column="id",
        )
    )
    return layer
