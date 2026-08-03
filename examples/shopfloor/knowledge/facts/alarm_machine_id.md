---
type: Selayer Fact
title: Alarm machine id
description: Machine identifier during an alarm state
selayer_id: fact.alarm_machine_id
generated:
  by: process:selayer-okf
  fingerprint: 8e99a39e9f925eba872275bd5121ef271dbd4b14e5c1495bc267fd362828ed9a
status: stable
---

# Catalog Definition

Semantic ID: `fact.alarm_machine_id`

Source: `machine_telemetry`

Data type: `string`

Expression: `if(machine_telemetry.machine_state = 'alarm', machine_telemetry.machine_id, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
