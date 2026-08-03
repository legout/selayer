---
type: Selayer Data Source
title: Eol test runs
selayer_id: source.eol_test_runs
generated:
  by: process:selayer-okf
  fingerprint: 49716d1a28a4693ef5c9bffd99dc2f77ccafe74b60ba4799b89c33fca39f3a3c
status: stable
---

# Catalog Definition

Semantic ID: `source.eol_test_runs`

Connector: delta

Schema fingerprint: a61005d4142ff2d43f7c6b78d847b5dfaffe198edb3ce80529fbeccc9b233a4d

Grain: eol_test_run_id

Schema:

- eol_test_run_id: utf8 (required)
- serial_number: utf8 (required)
- station_id: utf8 (required)
- attempt: int64 (required)
- result: utf8 (required)
- is_first_pass: boolean (required)
- input_voltage_v: float64 (required)
- output_voltage_v: float64 (required)
- power_w: float64 (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
