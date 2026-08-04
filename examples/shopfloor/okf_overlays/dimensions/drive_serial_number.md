---
selayer_id: dimension.drive_serial_number
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The conformed drive serial number identifies a serialized drive from the
serialized-drive registry. It is the shared identity anchor across component
genealogy, operation executions, and end-of-line tests.

# Caveats

This conformed identity comes from the serialized-drive registry only. Matching
serial-number strings in other sources reference the same drive through declared
relationships, not through string equality alone.

# Related Concepts

- [serialized_drives](../sources/serialized_drives.md)
