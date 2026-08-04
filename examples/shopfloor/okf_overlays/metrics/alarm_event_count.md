---
selayer_id: metric.alarm_event_count
sources:
  - resource: /references/kpi_definitions.md
  - resource: /references/process_overview.md
---

# Usage Guidance

Count the number of telemetry events recorded in the alarm machine state. This
metric counts telemetry rows where the machine state is alarm.

# Examples

```json selayer-query
{"metrics":["alarm_event_count"],"dimensions":["telemetry_machine_id"],"filters":{}}
```

# Caveats

No safe operation-event join exists. Telemetry alarm rows cannot be attributed
to a specific operation execution because matching machine string values do not
establish a safe join.

# Related Concepts

- [alarm_event_count_measure](../measures/alarm_event_count_measure.md)
- [telemetry_machine_id](../dimensions/telemetry_machine_id.md)
