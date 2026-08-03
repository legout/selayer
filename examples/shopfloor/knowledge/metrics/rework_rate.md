---
type: Selayer Metric
title: Rework rate
description: Rework operations as a share of all operations
selayer_id: metric.rework_rate
generated:
  by: process:selayer-okf
  fingerprint: e77c2c43f5c962cd3305618494eb58546ef1664cc248c1e07d66f33339fe4376
status: stable
---

# Catalog Definition

Semantic ID: `metric.rework_rate`

Declared measures: `rework_operation_count`, `operation_count_measure`

Expression: `rework_operation_count / nullif(operation_count_measure, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
