---
type: Reference
title: Shopfloor KPI definitions
status: stable
x_selayer_document_id: shopfloor.kpi_definitions
x_selayer_owner: manufacturing-analytics
x_selayer_version: "1"
---

# Purpose

This reference defines the twelve headline metrics of the shopfloor fixture
using the exact formulas declared in the catalog. The catalog is execution
authority; this table explains the grain, numerator, denominator, unit, and
zero behavior of each metric.

# Metric table

| Metric | Grain | Numerator or aggregate | Denominator | Unit | Zero behavior |
|---|---|---|---|---|---|
| `production_completion_rate` | production order | `total_completed_units` (sum of completed units) | `total_planned_units` (sum of planned units) | ratio | null when planned units are zero |
| `shipped_unit_count` | serialized drive | `shipped_units` (count_distinct shipped drives) | none | distinct drives | zero when no drive is shipped |
| `component_count` | fitted component | `component_count_measure` (count of fitted component rows) | none | fitted rows | zero when no component is fitted |
| `incoming_acceptance_rate` | component lot | `accepted_component_lot_count` (count_distinct accepted lots) | `inspected_component_lot_count` (count_distinct inspected lots) | ratio | null when no lot is inspected |
| `average_cycle_seconds` | operation execution | `average_cycle_seconds_measure` (avg of cycle seconds) | none | seconds per execution | null when no operation exists |
| `operation_count` | operation execution | `operation_count_measure` (count_distinct execution IDs) | none | distinct executions | zero when no operation exists |
| `rework_rate` | operation execution | `rework_operation_count` (count_distinct rework executions) | `operation_count_measure` (count_distinct executions) | ratio | null when no operation exists |
| `energy_per_operation_kwh` | operation execution | `total_operation_energy_kwh` (sum of energy) | `operation_count_measure` (count_distinct executions) | kWh per execution | null when no operation exists |
| `eol_attempt_pass_rate` | EOL attempt | `passed_eol_attempt_count` (count_distinct passing attempts) | `eol_attempt_count` (count_distinct attempts) | ratio | null when no attempt exists |
| `first_pass_yield` | EOL attempt | `first_pass_unit_count` (count_distinct first-pass drives) | `first_attempt_unit_count` (count_distinct drives with attempt one) | ratio | null when no first attempt exists |
| `alarm_event_count` | telemetry event | `alarm_event_count_measure` (count of alarm telemetry events) | none | alarm events | zero when no alarm event exists |
| `average_temperature_c` | telemetry event | `average_temperature_c_measure` (avg of temperature) | none | degrees Celsius | null when no telemetry exists |

# Counting semantics

- `component_count` counts fitted component rows, not distinct component identity.
- EOL pass rate counts attempts, not drives.
- First-pass yield counts distinct drives that pass on attempt one.
- Alarm count counts telemetry events in alarm state.
