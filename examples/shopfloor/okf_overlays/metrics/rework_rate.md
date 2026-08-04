---
selayer_id: metric.rework_rate
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Report the share of operation executions that are rework operations. This ratio
counts rework executions divided by all executions at the operation-execution
grain.

# Examples

```json selayer-query
{"metrics":["rework_rate"],"dimensions":["operation_line_id","operation_machine_id","shift","operation_name"],"filters":{}}
```

# Caveats

Rework executions divided by all executions. One drive may have several
operations, so a high rework rate on a machine may involve few distinct drives.

# Related Concepts

- [rework_operation_count](../measures/rework_operation_count.md)
- [operation_count_measure](../measures/operation_count_measure.md)
- [operation_machine_id](../dimensions/operation_machine_id.md)
