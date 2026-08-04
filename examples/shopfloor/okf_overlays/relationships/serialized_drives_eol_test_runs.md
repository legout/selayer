---
selayer_id: relationship.serialized_drives_eol_test_runs
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This relationship connects serialized drives (parent) to end-of-line test runs
(child). Source grain: serialized_drives [serial_number]. Target grain:
eol_test_runs [eol_test_run_id]. The safe traversal direction is from the many
side (EOL attempts) to the one side (serialized drives). Each EOL attempt
belongs to exactly one drive.

# Caveats

EOL attempts start at one and are unique per drive. Declaration does not replace
physical audit.

# Related Concepts

- [serialized_drives](../sources/serialized_drives.md)
- [eol_test_runs](../sources/eol_test_runs.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
