---
selayer_id: relationship.component_lot_inspections_component_consumption
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This relationship connects component lot inspections (parent) to component
consumption (child). The safe traversal direction is from the many side
(component consumption) to the one side (component lot inspections). Fitted
components reference a component lot.

# Caveats

Quarantined lots can have zero consumption rows because no components are
fitted from a rejected lot. Declaration does not replace physical audit.

# Related Concepts

- [component_lot_inspections](../sources/component_lot_inspections.md)
- [component_consumption](../sources/component_consumption.md)
