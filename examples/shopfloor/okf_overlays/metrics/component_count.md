---
selayer_id: metric.component_count
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Count the number of fitted component rows across serialized drives. This metric
counts fitted rows at the component-consumption grain, not distinct component
identity.

# Examples

```json selayer-query
{"metrics":["component_count"],"dimensions":["drive_serial_number","component_lot_id"],"filters":{}}
```

# Caveats

Counts fitted rows, not distinct component identity. A single component lot may
supply multiple fitted positions across different drives.

# Related Concepts

- [component_count_measure](../measures/component_count_measure.md)
