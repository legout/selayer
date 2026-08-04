---
selayer_id: fact.telemetry_event_machine_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

This fact is a non-null machine marker for one telemetry event. It serves as the
counting unit for telemetry-event measures.

# Caveats

The telemetry marker fact is a non-null row marker scoped to telemetry events.
It is not a conformed machine fact. No safe operation-event join exists.

# Related Concepts

- [machine_telemetry](../sources/machine_telemetry.md)
- [telemetry_event_count](../measures/telemetry_event_count.md)
