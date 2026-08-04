---
selayer_id: source.machine_telemetry
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The machine-telemetry source records independent machine samples. The declared
grain is one row per telemetry event, identified by a telemetry-local machine
identifier and a sample timestamp. Each event carries a machine state and a
temperature reading. This source uses a Parquet connector.

# Caveats

Telemetry samples do not join to operations. There is deliberately no safe
operation-to-telemetry relationship. Matching machine string values between
operation executions and telemetry do not establish a safe join.
