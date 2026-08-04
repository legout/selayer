---
selayer_id: measure.telemetry_event_count
sources:
  - resource: /references/process_overview.md
  - resource: /references/kpi_definitions.md
---

# Usage Guidance

This measure counts telemetry events. It aggregates the telemetry-event marker
fact by counting non-null rows.

# Caveats

This measure counts events, not drives or machines. Sampling frequency affects
averages computed from telemetry events.

# Related Concepts

- [telemetry_event_machine_id](../facts/telemetry_event_machine_id.md)
- [average_temperature_c](../metrics/average_temperature_c.md)
