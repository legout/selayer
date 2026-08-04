---
selayer_id: source.operation_executions
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The operation-execution source records production operations on serialized
drives. The declared grain is one row per operation execution, including rework
operations. Each row carries cycle time, energy, and an is-rework flag. This
source uses a Parquet connector.

# Caveats

One drive may have several operation executions, including rework. Operation
machines and lines are domain-specific to this source and are not joined to
telemetry machines.
