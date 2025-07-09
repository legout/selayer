# test_semantic_layer.py
import os
import pandas as pd
import polars as pl
import duckdb
from selayer import (
    SemanticLayer, DataSource, Fact, Measure, Dimension, 
    Hierarchy, Metric, Relationship, QueryEngine
)

def create_ecommerce_semantic_layer():
    """Create a semantic layer for the e-commerce sample data"""
    sl = SemanticLayer(
        name="E-commerce Analytics",
        description="Semantic layer for e-commerce analytics"
    )
    
    # Add data sources
    sl.add_data_source(DataSource(
        name="orders",
        type="parquet",
        path="data/orders.parquet"
    ))

    sl.add_data_source(DataSource(
        name="order_items",
        type="parquet",
        path="data/order_items.parquet"
    ))

    sl.add_data_source(DataSource(
        name="customers",
        type="parquet",
        path="data/customers.parquet"
    ))

    sl.add_data_source(DataSource(
        name="products",
        type="parquet",
        path="data/products.parquet"
    ))

    sl.add_data_source(DataSource(
        name="campaigns",
        type="parquet",
        path="data/campaigns.parquet"
    ))

    sl.add_data_source(DataSource(
        name="marketing_touches",
        type="parquet",
        path="data/marketing_touches.parquet"
    ))

    sl.add_data_source(DataSource(
        name="website_visits",
        type="parquet",
        path="data/website_visits.parquet"
    ))
    
    # Add facts
    sl.add_fact(Fact(
        name="order_amount",
        description="Total amount of each order in USD",
        data_type="decimal",
        source="orders",
        column="amount",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="order_total_amount",
        description="Total amount including shipping and discounts",
        data_type="decimal",
        source="orders",
        column="total_amount",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="discount_amount",
        description="Discount amount applied to each order",
        data_type="decimal",
        source="orders",
        column="discount_amount",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="shipping_cost",
        description="Shipping cost for each order",
        data_type="decimal",
        source="orders",
        column="shipping_cost",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="item_quantity",
        description="Quantity of each product in an order",
        data_type="integer",
        source="order_items",
        column="quantity",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="item_price",
        description="Price of each product in an order",
        data_type="decimal",
        source="order_items",
        column="price",
        is_additive=False  # Not additive across product dimension
    ))

    sl.add_fact(Fact(
        name="item_total",
        description="Total price for each line item",
        data_type="decimal",
        source="order_items",
        column="total",
        is_additive=True
    ))

    sl.add_fact(Fact(
        name="product_base_price",
        description="Base price of each product",
        data_type="decimal",
        source="products",
        column="base_price",
        is_additive=False
    ))

    sl.add_fact(Fact(
        name="product_cost",
        description="Cost of each product",
        data_type="decimal",
        source="products",
        column="cost",
        is_additive=False
    ))
    
    # Add measures
    sl.add_measure(Measure(
        name="total_revenue",
        description="Sum of all order amounts",
        fact="order_amount",
        aggregation="sum"
    ))

    sl.add_measure(Measure(
        name="total_discount",
        description="Sum of all discounts",
        fact="discount_amount",
        aggregation="sum"
    ))

    sl.add_measure(Measure(
        name="total_shipping",
        description="Sum of all shipping costs",
        fact="shipping_cost",
        aggregation="sum"
    ))

    sl.add_measure(Measure(
        name="order_count",
        description="Count of orders",
        fact="order_amount",
        aggregation="count_distinct"
    ))

    sl.add_measure(Measure(
        name="completed_order_count",
        description="Count of completed orders",
        fact="order_amount",
        aggregation="count_distinct",
        filter_expression="orders.status = 'completed'"
    ))

    sl.add_measure(Measure(
        name="units_sold",
        description="Total quantity of items sold",
        fact="item_quantity",
        aggregation="sum"
    ))

    sl.add_measure(Measure(
        name="product_cost_sum",
        description="Sum of product costs",
        fact="product_cost",
        aggregation="sum"
    ))
    
    # Add dimensions
    sl.add_dimension(Dimension(
        name="customer_id",
        description="Unique customer identifier",
        data_type="string",
        source="customers",
        column="id"
    ))

    sl.add_dimension(Dimension(
        name="customer_segment",
        description="Customer segment",
        data_type="string",
        source="customers",
        column="segment"
    ))

    sl.add_dimension(Dimension(
        name="customer_country",
        description="Customer country",
        data_type="string",
        source="customers",
        column="country"
    ))

    sl.add_dimension(Dimension(
        name="product_id",
        description="Unique product identifier",
        data_type="string",
        source="products",
        column="id"
    ))

    sl.add_dimension(Dimension(
        name="product_category",
        description="Product category",
        data_type="string",
        source="products",
        column="category"
    ))

    sl.add_dimension(Dimension(
        name="product_subcategory",
        description="Product subcategory",
        data_type="string",
        source="products",
        column="subcategory",
        hierarchies=["product_hierarchy"]
    ))

    sl.add_dimension(Dimension(
        name="order_date",
        description="Date of the order",
        data_type="date",
        source="orders",
        column="created_at",
        hierarchies=["time_hierarchy"]
    ))

    sl.add_dimension(Dimension(
        name="order_status",
        description="Status of the order",
        data_type="string",
        source="orders",
        column="status"
    ))

    sl.add_dimension(Dimension(
        name="payment_method",
        description="Payment method used",
        data_type="string",
        source="orders",
        column="payment_method"
    ))
    
    # Add hierarchies
    sl.add_hierarchy(Hierarchy(
        name="time_hierarchy",
        description="Time-based hierarchy for analysis",
        levels=["year", "quarter", "month", "day"]
    ))

    sl.add_hierarchy(Hierarchy(
        name="product_hierarchy",
        description="Product category hierarchy",
        levels=["product_category", "product_subcategory"]
    ))
    
    # Add metrics
    sl.add_metric(Metric(
        name="average_order_value",
        description="Average order value",
        expression="{{total_revenue}} / {{order_count}}",
        measures=["total_revenue", "order_count"]
    ))

    sl.add_metric(Metric(
        name="discount_rate",
        description="Average discount rate",
        expression="{{total_discount}} / {{total_revenue}}",
        measures=["total_discount", "total_revenue"]
    ))

    sl.add_metric(Metric(
        name="gross_margin",
        description="Gross margin percentage",
        expression="({{total_revenue}} - {{product_cost_sum}}) / {{total_revenue}}",
        measures=["total_revenue", "product_cost_sum"]
    ))

    sl.add_metric(Metric(
        name="order_completion_rate",
        description="Percentage of orders that are completed",
        expression="{{completed_order_count}} / {{order_count}}",
        measures=["completed_order_count", "order_count"]
    ))
    
    # Add relationships
    sl.add_relationship(Relationship(
        name="customer_orders",
        source="customers",
        target="orders",
        type="one_to_many",
        source_column="id",
        target_column="customer_id"
    ))

    sl.add_relationship(Relationship(
        name="order_items_rel",
        source="orders",
        target="order_items",
        type="one_to_many",
        source_column="id",
        target_column="order_id"
    ))

    sl.add_relationship(Relationship(
        name="product_order_items",
        source="products",
        target="order_items",
        type="one_to_many",
        source_column="id",
        target_column="product_id"
    ))
    
    return sl

def test_semantic_layer():
    """Test the semantic layer with sample queries"""
    # Check if sample data exists
    if not os.path.exists("data/orders.parquet"):
        print("Sample data not found. Please run sample_data_generator.py first.")
        return
    
    # Create semantic layer
    sl = create_ecommerce_semantic_layer()
    
    # Save and load the semantic layer
    sl.save("ecommerce_semantic_layer.yaml")
    loaded_sl = SemanticLayer.load("ecommerce_semantic_layer.yaml")
    
    # Generate Mermaid diagram
    mermaid_diagram = loaded_sl.to_mermaid()
    with open("ecommerce_semantic_layer.mermaid", "w") as f:
        f.write(mermaid_diagram)
    
    # Create query engine
    engine = QueryEngine(loaded_sl, engine_type="duckdb")
    
    # Run some test queries
    print("Running test queries...")
    
    # Query 1: Revenue by customer segment
    print("\nQuery 1: Revenue by customer segment")
    result1 = engine.query(
        metrics=["total_revenue", "average_order_value"],
        dimensions=["customer_segment"]
    )
    print(result1)
    
    # Query 2: Revenue by product category
    print("\nQuery 2: Revenue by product category")
    result2 = engine.query(
        metrics=["total_revenue", "units_sold", "gross_margin"],
        dimensions=["product_category"]
    )
    print(result2)
    
    # Query 3: Order completion rate by payment method
    print("\nQuery 3: Order completion rate by payment method")
    result3 = engine.query(
        metrics=["order_count", "completed_order_count", "order_completion_rate"],
        dimensions=["payment_method"]
    )
    print(result3)
    
    # Query 4: Discount analysis
    print("\nQuery 4: Discount analysis by customer segment")
    result4 = engine.query(
        metrics=["total_revenue", "total_discount", "discount_rate"],
        dimensions=["customer_segment"]
    )
    print(result4)

if __name__ == "__main__":
    test_semantic_layer()