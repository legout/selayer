---
selayer_id: metric.energy_per_operation_kwh
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Report the average energy consumed per operation execution in kilowatt-hours.
This metric is a ratio of total operation energy to distinct operation count.

# Examples

```json selayer-query
{"metrics":["energy_per_operation_kwh"],"dimensions":["operation_machine_id"],"filters":{}}
```

# Caveats

Ratio of total energy to distinct operation count. The denominator counts
execution IDs, not drives, so operations grouped by machine reflect per-execution
energy intensity.

# Related Concepts

- [total_operation_energy_kwh](../measures/total_operation_energy_kwh.md)
- [operation_count_measure](../measures/operation_count_measure.md)
- [operation_machine_id](../dimensions/operation_machine_id.md)
