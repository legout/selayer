---
selayer_id: relationship.serialized_drives_component_consumption
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

This relationship connects serialized drives (parent) to component consumption
(child). The safe traversal direction is from the many side (component
consumption, one row per fitted position) to the one side (serialized drives).

# Caveats

A serialized drive has one or more fitted component rows. Declaration does not
replace physical audit.

# Related Concepts

- [serialized_drives](../sources/serialized_drives.md)
- [component_consumption](../sources/component_consumption.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
