---
type: Selayer Data Source
title: Customer orders
selayer_id: source.customer_orders
generated:
  by: process:selayer-okf
  fingerprint: 90bb309e03a7e09478274d8f12adabf060b41efd2a2e2f4d2fb77e3017f447ce
status: stable
---

# Catalog Definition

Semantic ID: `source.customer_orders`

Connector: csv

Schema fingerprint: ce3e901ec6fee2e2b9ab1d333aae7df4bd515b440d97d81cf9594791b141459a

Grain: customer_order_id

Schema:

- customer_order_id: utf8 (required)
- customer_name: utf8 (required)
- customer_region: utf8 (required)
- requested_ship_date: date32 (required)
- product_model: utf8 (required)
- order_status: utf8 (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
