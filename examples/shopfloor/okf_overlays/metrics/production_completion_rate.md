---
selayer_id: metric.production_completion_rate
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Track completed units as a share of planned units at the production-order
grain. This ratio answers whether manufacturing throughput is meeting the
planned schedule.

# Examples

```json selayer-query
{"metrics":["production_completion_rate"],"dimensions":["schedule_status","requested_ship_date"],"filters":{}}
```

# Caveats

Production-order ratio; do not group through child serialized drives. The
numerator and denominator sum over production orders, not individual drives, so
drilling below the production-order grain changes the meaning.

# Related Concepts

- [total_completed_units](../measures/total_completed_units.md)
- [total_planned_units](../measures/total_planned_units.md)
