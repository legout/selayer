---
selayer_id: metric.incoming_acceptance_rate
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

Measure the share of inspected component lots that passed incoming inspection.
This ratio is computed at the component-lot grain.

# Examples

```json selayer-query
{"metrics":["incoming_acceptance_rate"],"dimensions":["supplier_name","component_type"],"filters":{}}
```

# Caveats

Distinct accepted lots divided by distinct inspected lots. A quarantined lot may
still have zero consumption rows, so acceptance is independent of usage.

# Related Concepts

- [accepted_component_lot_count](../measures/accepted_component_lot_count.md)
- [inspected_component_lot_count](../measures/inspected_component_lot_count.md)
