---
type: Selayer Metric
title: Production completion rate
description: Completed units as a share of planned units
selayer_id: metric.production_completion_rate
generated:
  by: process:selayer-okf
  fingerprint: 4cc0d0974f64d1478978de194f50325bac7a49ce53101d0f0f92cd967a896620
status: stable
---

# Catalog Definition

Semantic ID: `metric.production_completion_rate`

Declared measures: `total_completed_units`, `total_planned_units`

Expression: `total_completed_units / nullif(total_planned_units, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
