---
selayer_id: fact.first_pass_serial
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This fact marks a serialized drive that passes its first end-of-line attempt.
It is non-null only on attempt one with a pass result and serves as the counting
unit for first-pass unit measures.

# Caveats

First-pass markers count distinct drives that pass on attempt one. A later
passing retest does not change the first-pass result.

# Related Concepts

- [eol_test_runs](../sources/eol_test_runs.md)
- [first_pass_unit_count](../measures/first_pass_unit_count.md)
- [drive_serial_number](../dimensions/drive_serial_number.md)
