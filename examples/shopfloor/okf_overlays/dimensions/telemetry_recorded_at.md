---
selayer_id: dimension.telemetry_recorded_at
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The telemetry recorded-at timestamp marks when each telemetry event was sampled.
It is the only timestamp-type time dimension in the fixture.

# Caveats

Telemetry recorded-at is a sample timestamp, not an operation time. Sampling
frequency affects averages computed from telemetry events.

# Related Concepts

- [machine_telemetry](../sources/machine_telemetry.md)
