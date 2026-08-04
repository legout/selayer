---
selayer_id: dimension.operation_machine_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The operation machine identifier is recorded by operation executions. It groups
production operations by the machine on which they were performed.

# Caveats

Operation machine identity is operation-local. It is not joined to telemetry
machine identity. Matching machine string values between operation executions
and telemetry do not establish a safe join.

# Related Concepts

- [operation_executions](../sources/operation_executions.md)
