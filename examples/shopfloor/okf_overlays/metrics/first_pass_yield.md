---
selayer_id: metric.first_pass_yield
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

Report the share of distinct drives that pass end-of-line testing on the first
attempt. This metric counts distinct first-pass drives divided by distinct
drives with an attempt one.

# Examples

```json selayer-query
{"metrics":["first_pass_yield"],"dimensions":["drive_serial_number","station_id","product_model","firmware_revision"],"filters":{}}
```

# Caveats

Unit metric, not attempt metric. A later passing retest changes the attempt
pass rate but leaves first-pass yield unchanged because it counts distinct
drives, not attempts.

# Related Concepts

- [first_pass_unit_count](../measures/first_pass_unit_count.md)
- [first_attempt_unit_count](../measures/first_attempt_unit_count.md)
- [eol_attempt_pass_rate](../metrics/eol_attempt_pass_rate.md)
