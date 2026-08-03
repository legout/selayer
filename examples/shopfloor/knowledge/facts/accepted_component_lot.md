---
type: Selayer Fact
title: Accepted component lot
description: Component lot that passed incoming inspection
selayer_id: fact.accepted_component_lot
generated:
  by: process:selayer-okf
  fingerprint: d5c7556b791deacb6a8c3d510dda01a1edbbd0b68b61004102066f75aa2c5ece
status: stable
---

# Catalog Definition

Semantic ID: `fact.accepted_component_lot`

Source: `component_lot_inspections`

Data type: `string`

Expression: `if(component_lot_inspections.incoming_result = 'pass', component_lot_inspections.component_lot_id, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
