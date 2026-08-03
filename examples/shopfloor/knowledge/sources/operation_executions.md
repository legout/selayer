---
type: Selayer Data Source
title: Operation executions
selayer_id: source.operation_executions
generated:
  by: process:selayer-okf
  fingerprint: 953189aa404954f7c2d78d77af2b271a062f153e9084dc78ed823bc0b2dd3c8e
status: stable
---

# Catalog Definition

Semantic ID: `source.operation_executions`

Connector: parquet

Schema fingerprint: 16bd99f2d95c252d1458d9a7827ff8ee830d004a0e3fb1b8bc7773873e747c44

Grain: operation_execution_id

Schema:

- operation_execution_id: utf8 (required)
- serial_number: utf8 (required)
- operation_name: utf8 (required)
- line_id: utf8 (required)
- machine_id: utf8 (required)
- shift: utf8 (required)
- cycle_seconds: float64 (required)
- energy_kwh: float64 (required)
- max_torque_nm: float64 (required)
- max_temperature_c: float64 (required)
- result: utf8 (required)
- is_rework: boolean (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
