---
selayer_id: metric.eol_attempt_pass_rate
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

Report the share of end-of-line test attempts that pass. This metric counts
passing attempts divided by all attempts at the EOL-attempt grain.

# Examples

```json selayer-query
{"metrics":["eol_attempt_pass_rate"],"dimensions":["drive_serial_number"],"filters":{}}
```

# Caveats

A retest can change the rate without changing unit yield. A later passing
retest increases the attempt pass rate but does not affect first-pass yield,
which counts distinct drives.

# Related Concepts

- [passed_eol_attempt_count](../measures/passed_eol_attempt_count.md)
- [eol_attempt_count](../measures/eol_attempt_count.md)
- [first_pass_yield](../metrics/first_pass_yield.md)
