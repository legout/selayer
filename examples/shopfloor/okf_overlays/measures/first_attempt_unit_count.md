---
selayer_id: measure.first_attempt_unit_count
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

This measure counts distinct serialized drives with a first end-of-line attempt.
It aggregates the first-attempt marker fact by counting distinct non-null
serials.

# Caveats

This measure counts distinct drives, not attempts. Each drive has exactly one
first attempt.

# Related Concepts

- [first_attempt_serial](../facts/first_attempt_serial.md)
- [eol_attempt_pass_rate](../metrics/eol_attempt_pass_rate.md)
- [first_pass_yield](../metrics/first_pass_yield.md)
