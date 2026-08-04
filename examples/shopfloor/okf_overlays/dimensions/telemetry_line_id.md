---
selayer_id: dimension.telemetry_line_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The telemetry line identifier is recorded by machine telemetry. It groups
telemetry events by the line on which the machine operates.

# Caveats

Telemetry line identity is event-local. It is not joined to operation line
identity. Matching line string values between telemetry and operation executions
do not establish a safe join.

# Related Concepts

- [machine_telemetry](../sources/machine_telemetry.md)
