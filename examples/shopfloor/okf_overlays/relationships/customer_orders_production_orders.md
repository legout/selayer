---
selayer_id: relationship.customer_orders_production_orders
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

This relationship connects customer orders (parent) to production orders
(child). Source grain: customer_orders [customer_order_id]. Target grain:
production_orders [production_order_id]. The safe traversal direction is from
the many side (production orders) to the one side (customer orders). A
production order belongs to exactly one customer order.

# Caveats

A customer order can have zero production orders. Declaration does not replace
physical audit: the relationship must be verified against actual data before
relying on referential integrity.

# Related Concepts

- [customer_orders](../sources/customer_orders.md)
- [production_orders](../sources/production_orders.md)
