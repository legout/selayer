---
selayer_id: dimension.operation_line_id
sources:
  - resource: /references/process_overview.md
  - resource: /references/glossary.md
---

# Usage Guidance

The operation line identifier is recorded by operation executions. It groups
production operations by the line on which they were performed.

# Caveats

Operation line identity is operation-local. It is not joined to telemetry line
identity. Matching line string values between operation executions and telemetry
do not establish a safe join.

# Related Concepts

- [operation_executions](../sources/operation_executions.md)
