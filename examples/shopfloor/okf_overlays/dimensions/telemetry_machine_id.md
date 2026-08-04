---
selayer_id: dimension.telemetry_machine_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The telemetry machine identifier is recorded by machine telemetry. It scopes
each telemetry event to a telemetry-local machine.

# Caveats

Telemetry machine identity is event-local. It is not joined to operation machine
identity. Matching machine string values between telemetry and operation
executions do not establish a safe join.

# Related Concepts

- [machine_telemetry](../sources/machine_telemetry.md)
