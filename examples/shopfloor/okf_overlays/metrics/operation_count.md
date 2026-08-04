---
selayer_id: metric.operation_count
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Count the number of distinct operation executions. This metric counts execution
IDs, not drives; a single drive may have several operations including rework.

# Examples

```json selayer-query
{"metrics":["operation_count"],"dimensions":["operation_line_id","operation_machine_id","shift","operation_name"],"filters":{}}
```

# Caveats

Counts distinct execution IDs, not drives. One drive may have several
operations, so the count of operations always exceeds the count of drives when
rework or multi-step routings are present.

# Related Concepts

- [operation_count_measure](../measures/operation_count_measure.md)
- [operation_machine_id](../dimensions/operation_machine_id.md)
