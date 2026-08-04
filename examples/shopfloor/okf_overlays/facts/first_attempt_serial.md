---
selayer_id: fact.first_attempt_serial
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This fact marks a serialized drive's first end-of-line attempt. It is non-null
only on attempt one and serves as the counting unit for first-attempt unit
measures.

# Caveats

First-attempt markers count distinct drives with a first attempt, not attempts.
Each drive has exactly one first attempt.

# Related Concepts

- [eol_test_runs](../sources/eol_test_runs.md)
- [first_attempt_unit_count](../measures/first_attempt_unit_count.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
