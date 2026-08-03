---
type: Selayer Data Source
title: Production orders
selayer_id: source.production_orders
generated:
  by: process:selayer-okf
  fingerprint: 07bf255d7d9243c9ef0d4701a438c73c6025507d908da820fb30daa5c56d673c
status: stable
---

# Catalog Definition

Semantic ID: `source.production_orders`

Connector: sqlite

Schema fingerprint: 2156ceda7ca0bbc7ef92a60063dd13918a9e11d77e7065dbd556da29fad5c3c2

Grain: production_order_id

Schema:

- production_order_id: utf8 (required)
- customer_order_id: utf8 (required)
- product_model: utf8 (required)
- routing: utf8 (required)
- planned_units: int64 (required)
- completed_units: int64 (required)
- schedule_status: utf8 (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
