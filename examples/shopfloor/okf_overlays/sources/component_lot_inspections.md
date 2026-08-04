---
selayer_id: source.component_lot_inspections
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

The component-lot-inspection source records incoming quality checks for
component lots. One row per inspected lot is the declared grain; each row
carries an incoming result and a disposition.

# Caveats

An incoming pass maps to released; an incoming fail maps to quarantined.
Quarantined lots can have zero consumption rows because no components are
fitted from a rejected lot.
