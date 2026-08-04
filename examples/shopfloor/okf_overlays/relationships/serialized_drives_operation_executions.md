---
selayer_id: relationship.serialized_drives_operation_executions
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

This relationship connects serialized drives (parent) to operation executions
(child). The safe traversal direction is from the many side (operation
executions) to the one side (serialized drives). An operation execution belongs
to exactly one drive.

# Caveats

One drive may have several operation executions, including rework. Declaration
does not replace physical audit.

# Related Concepts

- [serialized_drives](../sources/serialized_drives.md)
- [operation_executions](../sources/operation_executions.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
