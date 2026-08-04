---
selayer_id: measure.alarm_event_count_measure
sources:
  - resource: /references/process_overview.md
  - resource: /references/kpi_definitions.md
---

# Usage Guidance

This measure counts telemetry events in alarm state. It aggregates the
alarm-event marker fact by counting non-null rows.

# Caveats

This measure counts events, not drives or machines. No safe operation-event
join exists.

# Related Concepts

- [alarm_event_machine_id](../facts/alarm_event_machine_id.md)
- [alarm_event_count](../metrics/alarm_event_count.md)
