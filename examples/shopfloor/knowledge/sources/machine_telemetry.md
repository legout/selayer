---
type: Selayer Data Source
title: Machine telemetry
selayer_id: source.machine_telemetry
generated:
  by: process:selayer-okf
  fingerprint: cce77843e7b1296fa53f93d82072e66edba95dda767ea9af27e0fd608ae30fe9
status: stable
---

# Catalog Definition

Semantic ID: `source.machine_telemetry`

Connector: parquet

Schema fingerprint: 1089b24666f59f60e54fd4f139959ec5273345a948f550a82480a59d4c7ee04b

Grain: machine_id, recorded_at

Schema:

- machine_id: utf8 (required)
- recorded_at: timestamp[ns] (required)
- line_id: utf8 (required)
- machine_state: utf8 (required)
- temperature_c: float64 (required)
- power_kw: float64 (required)

# Usage Guidance

# Examples

# Caveats

# Related Concepts
