---
type: Selayer Fact
title: Rework operation execution id
description: Identifier of a rework operation execution
selayer_id: fact.rework_operation_execution_id
generated:
  by: process:selayer-okf
  fingerprint: 6b8c555ea4242984963fbaf2cf4a1abc21b237893ddd3a14665683661b0faca5
status: stable
---

# Catalog Definition

Semantic ID: `fact.rework_operation_execution_id`

Source: `operation_executions`

Data type: `string`

Expression: `if(operation_executions.is_rework = true, operation_executions.operation_execution_id, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
