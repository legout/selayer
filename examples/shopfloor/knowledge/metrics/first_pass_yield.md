---
type: Selayer Metric
title: First pass yield
description: First-pass yield across drives
selayer_id: metric.first_pass_yield
generated:
  by: process:selayer-okf
  fingerprint: d1613394d87584d4cb58aeb2e9bd424858d5ab400d0bb7ede0ee2f6984b0d986
status: stable
---

# Catalog Definition

Semantic ID: `metric.first_pass_yield`

Declared measures: `first_pass_unit_count`, `first_attempt_unit_count`

Expression: `first_pass_unit_count / nullif(first_attempt_unit_count, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
