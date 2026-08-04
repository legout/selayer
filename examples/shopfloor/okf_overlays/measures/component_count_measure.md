---
selayer_id: measure.component_count_measure
sources:
  - resource: /references/process_overview.md
  - resource: /references/kpi_definitions.md
---

# Usage Guidance

This measure counts fitted component rows across serialized drives. It
aggregates the component serial-number fact by counting non-null rows.

# Caveats

This measure counts fitted rows, not distinct component identity. A single
component lot may supply multiple fitted positions.

# Related Concepts

- [component_consumption](../sources/component_consumption.md)
- [component_count](../metrics/component_count.md)
