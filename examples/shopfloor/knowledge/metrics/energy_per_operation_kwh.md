---
type: Selayer Metric
title: Energy per operation kwh
description: Average energy per operation in kWh
selayer_id: metric.energy_per_operation_kwh
generated:
  by: process:selayer-okf
  fingerprint: 1a7c3f5cf7bd23b1675762f87df0ce253b5fa0eaabbf932677de395349258c04
status: stable
---

# Catalog Definition

Semantic ID: `metric.energy_per_operation_kwh`

Declared measures: `total_operation_energy_kwh`, `operation_count_measure`

Expression: `total_operation_energy_kwh / nullif(operation_count_measure, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
