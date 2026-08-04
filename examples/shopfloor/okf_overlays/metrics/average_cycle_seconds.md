---
selayer_id: metric.average_cycle_seconds
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Report the average cycle duration in seconds per operation execution. This
metric is non-additive: averaging across groups does not preserve the overall
average when group sizes differ.

# Examples

```json selayer-query
{"metrics":["average_cycle_seconds"],"dimensions":["operation_line_id","operation_machine_id","shift","operation_name"],"filters":{}}
```

# Caveats

Average is non-additive; combining averages across different operation groups
can mislead when execution counts differ. The metric is scoped to the
operation-execution grain.

# Related Concepts

- [average_cycle_seconds_measure](../measures/average_cycle_seconds_measure.md)
- [operation_machine_id](../dimensions/operation_machine_id.md)
