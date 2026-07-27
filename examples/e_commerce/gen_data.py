# sample_data_generator.py
import os
import random
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from faker import Faker

# Initialize Faker for generating realistic data
fake = Faker()
Faker.seed(42)
np.random.seed(42)
random.seed(42)

# Create output directory
os.makedirs("data", exist_ok=True)


def generate_customers(num_customers=1000):
    """Generate sample customer data"""
    customers = []

    segments = ["Premium", "Standard", "Basic"]
    acquisition_channels = [
        "Organic Search",
        "Paid Search",
        "Social Media",
        "Email",
        "Referral",
        "Direct",
    ]

    for i in range(num_customers):
        customer_id = str(uuid.uuid4())
        registration_date = fake.date_time_between(start_date="-3y", end_date="now")

        customers.append(
            {
                "id": customer_id,
                "email": fake.email(),
                "name": fake.name(),
                "segment": np.random.choice(segments, p=[0.2, 0.5, 0.3]),
                "acquisition_channel": np.random.choice(acquisition_channels),
                "registration_date": registration_date,
                "country": fake.country_code(),
                "city": fake.city(),
                "is_active": random.random() > 0.1,  # 90% of customers are active
                "lifetime_value": round(
                    random.uniform(0, 2000), 2
                ),  # Initial LTV value
            }
        )

    return pd.DataFrame(customers)


def generate_products(num_products=200):
    """Generate sample product data"""
    products = []

    categories = [
        "Electronics",
        "Clothing",
        "Home & Kitchen",
        "Books",
        "Sports",
        "Beauty",
    ]
    subcategories = {
        "Electronics": ["Phones", "Laptops", "Audio", "Accessories", "Cameras"],
        "Clothing": ["Men's", "Women's", "Children's", "Shoes", "Accessories"],
        "Home & Kitchen": ["Furniture", "Cookware", "Decor", "Bedding", "Appliances"],
        "Books": [
            "Fiction",
            "Non-fiction",
            "Educational",
            "Comics",
            "Children's Books",
        ],
        "Sports": [
            "Fitness",
            "Outdoor",
            "Team Sports",
            "Water Sports",
            "Winter Sports",
        ],
        "Beauty": ["Skincare", "Makeup", "Haircare", "Fragrance", "Tools"],
    }

    for i in range(num_products):
        product_id = str(uuid.uuid4())
        category = np.random.choice(categories)
        subcategory = np.random.choice(subcategories[category])

        base_price = random.uniform(10, 1000)
        margin = random.uniform(0.2, 0.6)  # 20-60% margin

        products.append(
            {
                "id": product_id,
                "name": fake.catch_phrase(),
                "category": category,
                "subcategory": subcategory,
                "base_price": round(base_price, 2),
                "cost": round(base_price * (1 - margin), 2),
                "in_stock": random.randint(0, 100),
                "supplier_id": random.randint(1, 20),
                "created_at": fake.date_time_between(start_date="-5y", end_date="now"),
                "is_active": random.random() > 0.15,  # 85% of products are active
            }
        )

    return pd.DataFrame(products)


def generate_orders(customers_df, products_df, num_orders=5000):
    """Generate sample order data"""
    orders = []
    order_items = []

    statuses = ["completed", "shipped", "processing", "cancelled", "returned"]
    status_weights = [0.7, 0.1, 0.1, 0.05, 0.05]  # 70% completed, 10% shipped, etc.

    payment_methods = [
        "Credit Card",
        "PayPal",
        "Bank Transfer",
        "Apple Pay",
        "Google Pay",
    ]

    # Get customer and product IDs
    customer_ids = customers_df["id"].tolist()
    product_ids = products_df["id"].tolist()
    product_prices = dict(zip(products_df["id"], products_df["base_price"]))

    # Some customers will have multiple orders
    orders_per_customer = np.random.poisson(2, size=len(customer_ids))

    order_id_counter = 1
    for i, customer_id in enumerate(customer_ids):
        num_customer_orders = max(1, orders_per_customer[i])  # At least 1 order

        for _ in range(num_customer_orders):
            if order_id_counter > num_orders:
                break

            order_id = f"ORD-{order_id_counter:06d}"
            order_date = fake.date_time_between(
                start_date=customers_df.loc[
                    customers_df["id"] == customer_id, "registration_date"
                ].iloc[0],
                end_date="now",
            )

            status = np.random.choice(statuses, p=status_weights)

            # If order is returned or cancelled, add a reason
            reason = None
            if status in ["returned", "cancelled"]:
                reasons = [
                    "Changed mind",
                    "Found better price",
                    "Damaged",
                    "Wrong item",
                    "Delayed shipping",
                ]
                reason = np.random.choice(reasons)

            # Generate shipping info
            shipping_cost = round(random.uniform(5, 20), 2)

            # Determine if discount was applied
            has_discount = random.random() < 0.3  # 30% of orders have a discount
            discount_code = fake.word().upper() if has_discount else None
            discount_amount = round(random.uniform(5, 30), 2) if has_discount else 0

            # Create order
            orders.append(
                {
                    "id": order_id,
                    "customer_id": customer_id,
                    "created_at": order_date,
                    "status": status,
                    "payment_method": np.random.choice(payment_methods),
                    "shipping_cost": shipping_cost,
                    "discount_code": discount_code,
                    "discount_amount": discount_amount,
                    "reason": reason,
                    "is_first_purchase": order_id_counter
                    <= len(customer_ids),  # First purchase for each customer
                }
            )

            # Generate 1-5 items per order
            num_items = random.randint(1, 5)
            order_products = np.random.choice(
                product_ids, size=num_items, replace=False
            )

            # Calculate order total
            order_total = 0

            for product_id in order_products:
                quantity = random.randint(1, 3)
                price = product_prices[product_id]

                # Apply some random price variation (sales, etc.)
                price_multiplier = random.uniform(0.9, 1.1)
                final_price = round(price * price_multiplier, 2)

                item_total = final_price * quantity
                order_total += item_total

                order_items.append(
                    {
                        "order_id": order_id,
                        "product_id": product_id,
                        "quantity": quantity,
                        "price": final_price,
                        "total": item_total,
                    }
                )

            # Update the order with the total
            orders[-1]["amount"] = round(order_total, 2)
            orders[-1]["total_amount"] = round(
                order_total + shipping_cost - discount_amount, 2
            )

            order_id_counter += 1

            if order_id_counter > num_orders:
                break

    return pd.DataFrame(orders), pd.DataFrame(order_items)


def generate_marketing_campaigns(num_campaigns=50):
    """Generate sample marketing campaign data"""
    campaigns = []

    campaign_types = ["Email", "Social Media", "Search", "Display", "Affiliate"]

    for i in range(num_campaigns):
        start_date = fake.date_time_between(start_date="-2y", end_date="now")
        end_date = start_date + timedelta(days=random.randint(7, 90))

        budget = round(random.uniform(1000, 50000), 2)

        campaigns.append(
            {
                "id": f"CAMP-{i + 1:03d}",
                "name": fake.bs(),
                "type": np.random.choice(campaign_types),
                "start_date": start_date,
                "end_date": end_date,
                "budget": budget,
                "spend": round(budget * random.uniform(0.8, 1.1), 2),  # Actual spend
                "impressions": random.randint(10000, 1000000),
                "clicks": random.randint(100, 50000),
                "conversions": random.randint(10, 5000),
            }
        )

    return pd.DataFrame(campaigns)


def generate_marketing_touches(customers_df, campaigns_df, num_touches=10000):
    """Generate marketing touch points for customers"""
    touches = []

    customer_ids = customers_df["id"].tolist()
    campaign_ids = campaigns_df["id"].tolist()
    campaign_dates = dict(
        zip(
            campaigns_df["id"],
            zip(campaigns_df["start_date"], campaigns_df["end_date"]),
        )
    )

    for i in range(num_touches):
        campaign_id = np.random.choice(campaign_ids)
        start_date, end_date = campaign_dates[campaign_id]

        touches.append(
            {
                "id": i + 1,
                "customer_id": np.random.choice(customer_ids),
                "campaign_id": campaign_id,
                "touch_date": fake.date_time_between(
                    start_date=start_date, end_date=end_date
                ),
                "channel": np.random.choice(["Email", "Web", "Mobile", "Social"]),
                "interaction": np.random.choice(
                    ["View", "Click", "Conversion", "None"], p=[0.6, 0.3, 0.05, 0.05]
                ),
            }
        )

    return pd.DataFrame(touches)


def generate_website_visits(customers_df, num_visits=20000):
    """Generate website visit data"""
    visits = []

    customer_ids = customers_df["id"].tolist()
    pages = [
        "/home",
        "/products",
        "/category/electronics",
        "/category/clothing",
        "/product/detail",
        "/cart",
        "/checkout",
        "/account",
        "/support",
    ]

    for i in range(num_visits):
        # Some visits are from non-customers (anonymous)
        is_customer = random.random() < 0.7  # 70% of visits are from customers
        customer_id = np.random.choice(customer_ids) if is_customer else None

        visit_date = fake.date_time_between(start_date="-1y", end_date="now")

        # Generate 1-10 page views per visit
        num_pages = np.random.geometric(0.3)
        num_pages = min(max(1, num_pages), 10)

        session_id = str(uuid.uuid4())

        for j in range(num_pages):
            page = np.random.choice(pages)
            time_on_page = round(
                random.expovariate(1 / 60), 1
            )  # Average 60 seconds per page

            visits.append(
                {
                    "id": f"{i + 1}-{j + 1}",
                    "session_id": session_id,
                    "customer_id": customer_id,
                    "visit_date": visit_date + timedelta(seconds=j * time_on_page),
                    "page": page,
                    "time_on_page": time_on_page,
                    "device": np.random.choice(
                        ["Desktop", "Mobile", "Tablet"], p=[0.6, 0.3, 0.1]
                    ),
                    "browser": np.random.choice(
                        ["Chrome", "Safari", "Firefox", "Edge"]
                    ),
                    "is_bounce": j == 0 and num_pages == 1,
                    "referrer": np.random.choice(
                        ["Google", "Facebook", "Direct", "Email", "Other"],
                        p=[0.4, 0.2, 0.2, 0.1, 0.1],
                    ),
                }
            )

    return pd.DataFrame(visits)


def generate_customer_support_tickets(customers_df, orders_df, num_tickets=2000):
    """Generate customer support ticket data"""
    tickets = []

    customer_ids = customers_df["id"].tolist()
    order_ids = orders_df["id"].tolist()

    ticket_types = ["Question", "Problem", "Return", "Complaint", "Feedback"]
    statuses = ["Open", "In Progress", "Resolved", "Closed"]
    priorities = ["Low", "Medium", "High", "Urgent"]

    for i in range(num_tickets):
        customer_id = np.random.choice(customer_ids)

        # 80% of tickets are related to an order
        has_order = random.random() < 0.8
        order_id = np.random.choice(order_ids) if has_order else None

        created_date = fake.date_time_between(start_date="-1y", end_date="now")

        # Determine status and closed date
        status = np.random.choice(statuses)
        closed_date = None
        if status in ["Resolved", "Closed"]:
            closed_date = created_date + timedelta(days=random.randint(1, 14))

        tickets.append(
            {
                "id": f"TICK-{i + 1:05d}",
                "customer_id": customer_id,
                "order_id": order_id,
                "created_at": created_date,
                "closed_at": closed_date,
                "type": np.random.choice(ticket_types),
                "subject": fake.sentence(),
                "status": status,
                "priority": np.random.choice(priorities),
                "response_time_hours": random.randint(1, 72)
                if status != "Open"
                else None,
                "satisfaction_score": random.randint(1, 5)
                if status in ["Resolved", "Closed"]
                else None,
            }
        )

    return pd.DataFrame(tickets)


def generate_inventory_snapshots(products_df, num_days=90):
    """Generate daily inventory snapshots"""
    snapshots = []

    product_ids = products_df["id"].tolist()
    initial_stock = dict(zip(products_df["id"], products_df["in_stock"]))

    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=num_days)

    current_date = start_date
    while current_date <= end_date:
        for product_id in product_ids:
            # Simulate inventory changes
            if current_date == start_date:
                stock = initial_stock[product_id]
            else:
                # Get previous day's stock
                prev_stock = next(
                    (
                        s["stock"]
                        for s in snapshots
                        if s["product_id"] == product_id
                        and s["date"] == current_date - timedelta(days=1)
                    ),
                    initial_stock[product_id],
                )

                # Simulate sales and restocks
                sales = max(
                    0, int(np.random.poisson(0.5))
                )  # Average 0.5 units sold per day
                restock = int(
                    np.random.poisson(0.2) * 10
                )  # Occasional restocks of ~10 units

                stock = max(0, prev_stock - sales + restock)

            snapshots.append(
                {
                    "product_id": product_id,
                    "date": current_date,
                    "stock": stock,
                    "low_stock_alert": stock < 5,
                    "out_of_stock": stock == 0,
                }
            )

        current_date += timedelta(days=1)

    return pd.DataFrame(snapshots)


def main():
    print("Generating sample data...")

    # Generate base datasets
    print("Generating customers...")
    customers = generate_customers(num_customers=1000)

    print("Generating products...")
    products = generate_products(num_products=200)

    print("Generating orders...")
    orders, order_items = generate_orders(customers, products, num_orders=5000)

    print("Generating marketing campaigns...")
    campaigns = generate_marketing_campaigns(num_campaigns=50)

    print("Generating marketing touches...")
    marketing_touches = generate_marketing_touches(
        customers, campaigns, num_touches=10000
    )

    print("Generating website visits...")
    website_visits = generate_website_visits(customers, num_visits=20000)

    print("Generating support tickets...")
    support_tickets = generate_customer_support_tickets(
        customers, orders, num_tickets=2000
    )

    print("Generating inventory snapshots...")
    inventory = generate_inventory_snapshots(products, num_days=90)

    # Save datasets
    print("Saving datasets...")
    customers.to_parquet("data/customers.parquet", index=False)
    products.to_parquet("data/products.parquet", index=False)
    orders.to_parquet("data/orders.parquet", index=False)
    order_items.to_parquet("data/order_items.parquet", index=False)
    campaigns.to_parquet("data/campaigns.parquet", index=False)
    marketing_touches.to_parquet("data/marketing_touches.parquet", index=False)
    website_visits.to_parquet("data/website_visits.parquet", index=False)
    support_tickets.to_parquet("data/support_tickets.parquet", index=False)
    inventory.to_parquet("data/inventory.parquet", index=False)

    # Also save as CSV for easier inspection
    customers.to_csv("data/customers.csv", index=False)
    products.to_csv("data/products.csv", index=False)
    orders.to_csv("data/orders.csv", index=False)
    order_items.to_csv("data/order_items.csv", index=False)

    print("Sample data generation complete!")
    print(f"Generated {len(customers)} customers")
    print(f"Generated {len(products)} products")
    print(f"Generated {len(orders)} orders with {len(order_items)} order items")
    print(
        f"Generated {len(campaigns)} marketing campaigns with {len(marketing_touches)} customer touches"
    )
    print(f"Generated {len(website_visits)} website visits")
    print(f"Generated {len(support_tickets)} support tickets")
    print(f"Generated {len(inventory)} inventory records")


if __name__ == "__main__":
    main()
