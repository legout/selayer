---
selayer_id: measure.first_pass_unit_count
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This measure counts distinct serialized drives that pass their first end-of-line
attempt. It aggregates the first-pass marker fact by counting distinct non-null
serials.

# Caveats

This measure counts distinct first-pass drives. A later passing retest does not
change the first-pass result.

# Related Concepts

- [first_pass_serial](../facts/first_pass_serial.md)
- [first_pass_yield](../metrics/first_pass_yield.md)
