---
selayer_id: relationship.production_orders_serialized_drives
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

This relationship connects production orders (parent) to serialized drives
(child). The safe traversal direction is from the many side (serialized drives)
to the one side (production orders). A serialized drive is produced by exactly
one production order.

# Caveats

A production order can have zero serialized drives when it is still open.
Declaration does not replace physical audit.

# Related Concepts

- [production_orders](../sources/production_orders.md)
- [serialized_drives](../sources/serialized_drives.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
