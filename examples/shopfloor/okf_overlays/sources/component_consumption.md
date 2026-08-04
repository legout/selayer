---
selayer_id: source.component_consumption
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The component-consumption source records fitted component positions across
serialized drives. The declared grain is one row per fitted component position,
including serial number, component lot, and fitted position. This source uses
a Parquet connector.

# Caveats

Fitted position is part of the grain. Component consumption counts fitted rows,
not distinct component identity. A single component lot may supply multiple
fitted positions across different drives.
