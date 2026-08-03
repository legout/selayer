---
type: Selayer Data Source
title: Serialized drives
selayer_id: source.serialized_drives
generated:
  by: process:selayer-okf
  fingerprint: 726e6f469154341e449158fefb58221bf2155e23a6792ccafb5a1a651ebdedd4
status: stable
---

# Catalog Definition

Semantic ID: `source.serialized_drives`

Connector: duckdb

Schema fingerprint: 8f7331cbba4ee08734251f204cae852945fd62cc4f5506ea96b139f08ba2bfd5

Grain: serial_number

Schema:

- serial_number: utf8 (required)
- production_order_id: utf8 (required)
- product_model: utf8 (required)
- bom_revision: utf8 (required)
- firmware_revision: utf8 (required)
- completion_status: utf8 (required)
- shipment_status: utf8 (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
