<!-- markdownlint-disable MD013 -->

# Example Questions Answerable from the E-commerce Semantic Layer

Reference document mapping typical business questions to the specific
semantic elements (`measures`, `metrics`, `dimensions`, `hierarchies`,
`facts`) that answer them. Useful for evaluation suites, dashboard
mock-ups, and verifying layer coverage.

---

## 1. Revenue & Sales Performance

*Uses: `total_revenue`, `order_count`, `average_order_value`, `time_hierarchy`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 1 | What was the total revenue last quarter, and how does it compare to the previous quarter? | `total_revenue`, `order_date` → `time_hierarchy` (quarter) |
| 2 | How has revenue trended month-over-month for the last 12 months? | `total_revenue` by `order_date` → `time_hierarchy` (month) |
| 3 | What is the year-over-year revenue growth (2025 vs 2024)? | `total_revenue` by `order_date` → `time_hierarchy` (year) |
| 4 | What is the average order value (AOV) per month for the last year? | `average_order_value` by `order_date` → month |
| 5 | Which month had the highest revenue so far this year? | `total_revenue` ordered desc by `order_date` → month |
| 6 | What is the rolling 30-day revenue trend? | `total_revenue` with trailing window over `order_date` |
| 7 | Which were the top 10 days by revenue? | `total_revenue` ordered desc by `order_date` → day |

## 2. Order & Operational Analytics

*Uses: `order_count`, `completed_order_count`, `order_completion_rate`,
`order_status`, `payment_method`, `total_shipping`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 8 | What is the order completion rate overall and over the last 90 days? | `order_completion_rate`, `order_date` |
| 9 | Breakdown of orders by status (pending, completed, cancelled, refunded). | `order_count` grouped by `order_status` |
| 10 | Which payment methods have the highest AOV? | `average_order_value` grouped by `payment_method` |
| 11 | What is the average shipping cost per order, by month? | `total_shipping` / `order_count` over `order_date` → month |
| 12 | How many orders were placed today / this week / this month? | `order_count` filtered by `order_date` |
| 13 | What share of orders end up cancelled or refunded, by month? | `order_count` filtered by `order_status`, over `order_date` → month |

## 3. Product Analytics

*Uses: `products`, `product_category`, `product_subcategory`,
`product_hierarchy`, `units_sold`, `total_revenue`,
`product_base_price`, `item_price`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 14 | Top 10 products by units sold. | `units_sold`, `product_id` |
| 15 | Top 10 products by total revenue. | `total_revenue`, `product_id` |
| 16 | Revenue and AOV per product category and subcategory. | `total_revenue`, `average_order_value`, `product_hierarchy` |
| 17 | Which product category has the highest gross margin? | `gross_margin`, `product_category` |
| 18 | Top products by gross margin %. | `gross_margin`, `product_id` |
| 19 | Average selling price vs base price by category (discount effectiveness). | `item_price`, `product_base_price`, `product_category` |
| 20 | Which subcategories are growing fastest vs last year? | `total_revenue`, `product_hierarchy`, YoY comparison |

## 4. Pricing & Discount Analytics

*Uses: `total_discount`, `discount_amount`, `discount_rate`,
`item_price`, `product_base_price`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 21 | What is the overall discount rate (% of revenue discounted away)? | `discount_rate` |
| 22 | Which product categories receive the highest discount rate? | `discount_rate` grouped by `product_category` |
| 23 | How does discount rate vary by month? | `discount_rate` over `order_date` → month |
| 24 | What is the average discount per order? | `total_discount` / `order_count` |
| 25 | What is the average discount amount per order by customer segment? | `total_discount` / `order_count` by `customer_segment` |

## 5. Customer Analytics

*Uses: `customers`, `customer_id`, `customer_segment`, `customer_country`,
`total_revenue`, `order_count`, `average_order_value`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 26 | How many distinct customers placed orders in the last 30 / 90 / 365 days? | distinct `customer_id` over `order_date` window |
| 27 | Revenue per customer segment (VIP, regular, new). | `total_revenue` grouped by `customer_segment` |
| 28 | What is the AOV per customer segment? | `average_order_value` grouped by `customer_segment` |
| 29 | Top 10 countries by revenue, with order count. | `total_revenue`, `order_count` grouped by `customer_country` |
| 30 | Top customers by lifetime spend. | `total_revenue` ordered desc by `customer_id` |
| 31 | How concentrated is revenue: what % of customers drive 80% of sales? | cumulative `total_revenue` ordered desc by `customer_id` (Pareto) |

## 6. Margin & Profitability

*Uses: `gross_margin`, `product_cost_sum`, `total_revenue`,
`product_cost`, `order_amount`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 32 | Overall gross margin % for the last quarter vs previous quarter. | `gross_margin` over `order_date` → quarter |
| 33 | Gross margin per product category. | `gross_margin` grouped by `product_category` |
| 34 | Revenue vs product cost by month. | `total_revenue`, `product_cost_sum` over `order_date` → month |
| 35 | Top 10 most profitable products (highest absolute gross margin). | `total_revenue − product_cost_sum` by `product_id` |

## 7. Cross-Dimensional / Segmentation Matrices

*Composed questions — exercise joins via `relationships`*

| # | Question | Semantic elements |
| --- | ---------- | ------------------- |
| 36 | Revenue and order count by country × quarter matrix. | `total_revenue`, `order_count`, `customer_country`, `time_hierarchy` (quarter) |
| 37 | AOV by payment method × customer segment. | `average_order_value`, `payment_method`, `customer_segment` |
| 38 | Units sold per product category per month. | `units_sold`, `product_category`, `order_date` → month |
| 39 | Discount rate by customer segment by year. | `discount_rate`, `customer_segment`, `time_hierarchy` (year) |
| 40 | Gross margin by country × product category. | `gross_margin`, `customer_country`, `product_category` |

---

## ⚠️ NOT directly answerable from this semantic layer

The following data sources are registered but have **no facts, measures,
or dimensions defined for them**, so these question types would require
extending the layer first:

- `campaigns` — campaign-level spend, reach, ROI
- `marketing_touches` — attribution / first-touch / last-touch analysis
- `website_visits` — funnel from visit → order

### Example questions that need extension

| # | Question | Missing layer elements |
| --- | ---------- | ------------------------ |
| M1 | Which campaigns drove the most revenue last month? | need `campaign_id`/`campaign_name` dimension + join to orders |
| M2 | What is the marketing ROI per campaign? | need campaign-cost fact/measure |
| M3 | Funnel: website visits → orders → revenue by campaign | need fact(s) on `website_visits` and `marketing_touches` |
| M4 | Which marketing touch is most associated with completed orders? | need `marketing_touches` dimension (channel, campaign, touch type) |
| M5 | Top traffic sources by conversion rate | need `website_visits` dimensions |

Once those sources are wired up with their own facts/measures/dimensions
and a relationship to `orders` (likely via `customer_id` or a session id),
all of M1–M5 become answerable in the same style as the questions above.
