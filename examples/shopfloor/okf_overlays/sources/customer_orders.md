---
selayer_id: source.customer_orders
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The customer-order source captures demand for a product model. One row per
customer order is the declared grain; the source feeds order-intent dimensions
such as customer region and requested ship date.

# Caveats

Customer orders are parent rows to production orders. A customer order may have
zero production orders. This source does not carry execution or shipment facts.
