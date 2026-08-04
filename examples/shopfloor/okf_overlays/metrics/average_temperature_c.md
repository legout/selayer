---
selayer_id: metric.average_temperature_c
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Report the average sampled machine temperature in degrees Celsius across
telemetry events.

# Examples

```json selayer-query
{"metrics":["average_temperature_c"],"dimensions":["telemetry_machine_id"],"filters":{}}
```

# Caveats

Sampling frequency affects the average. Machines sampled more often contribute
more rows, so the unweighted average reflects sampling cadence rather than true
time-weighted temperature. No safe operation-event join exists.

# Related Concepts

- [average_temperature_c_measure](../measures/average_temperature_c_measure.md)
- [telemetry_machine_id](../dimensions/telemetry_machine_id.md)
