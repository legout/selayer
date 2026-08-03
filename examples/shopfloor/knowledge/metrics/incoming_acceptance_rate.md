---
type: Selayer Metric
title: Incoming acceptance rate
description: Accepted lots as a share of inspected lots
selayer_id: metric.incoming_acceptance_rate
generated:
  by: process:selayer-okf
  fingerprint: d5494df52bd108a9c50b32e2b3377fe8846992f6795abeb0278630981f26b9f5
status: stable
---

# Catalog Definition

Semantic ID: `metric.incoming_acceptance_rate`

Declared measures: `accepted_component_lot_count`, `inspected_component_lot_count`

Expression: `accepted_component_lot_count / nullif(inspected_component_lot_count, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
