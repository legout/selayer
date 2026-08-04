---
selayer_id: fact.alarm_event_machine_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

This fact is a non-null machine marker for one telemetry event in alarm state.
It is null outside alarm state and serves as the counting unit for alarm-event
measures.

# Caveats

The alarm marker fact is a non-null row marker scoped to telemetry events. It
is not a conformed machine fact. No safe operation-event join exists.

# Related Concepts

- [machine_telemetry](../sources/machine_telemetry.md)
- [alarm_event_count_measure](../measures/alarm_event_count_measure.md)
