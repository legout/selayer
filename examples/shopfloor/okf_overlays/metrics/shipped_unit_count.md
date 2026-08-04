---
selayer_id: metric.shipped_unit_count
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Count the number of serialized drives that have reached the shipped state. This
metric counts distinct shipped drives, not customer orders or planned units.

# Examples

```json selayer-query
{"metrics":["shipped_unit_count"],"dimensions":["customer_region"],"filters":{}}
```

# Caveats

Counts distinct shipped drives, not orders or planned units. A customer order
may correspond to multiple production orders and drives.

# Related Concepts

- [shipped_units](../measures/shipped_units.md)
