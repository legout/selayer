---
okf_version: "0.2"
---

# Shop-floor Motor Drive Analytics

Grain-aware semantic model for the deterministic shop-floor example

# Sources

- [Component consumption](sources/component_consumption.md)
- [Component lot inspections](sources/component_lot_inspections.md)
- [Customer orders](sources/customer_orders.md)
- [Eol test runs](sources/eol_test_runs.md)
- [Machine telemetry](sources/machine_telemetry.md)
- [Operation executions](sources/operation_executions.md)
- [Production orders](sources/production_orders.md)
- [Serialized drives](sources/serialized_drives.md)

# Dimensions

- [Bom revision](dimensions/bom_revision.md)
- [Component lot id](dimensions/component_lot_id.md)
- [Component type](dimensions/component_type.md)
- [Customer region](dimensions/customer_region.md)
- [Drive serial number](dimensions/drive_serial_number.md)
- [Firmware revision](dimensions/firmware_revision.md)
- [Line id](dimensions/line_id.md)
- [Machine id](dimensions/machine_id.md)
- [Machine state](dimensions/machine_state.md)
- [Operation name](dimensions/operation_name.md)
- [Product model](dimensions/product_model.md)
- [Routing](dimensions/routing.md)
- [Schedule status](dimensions/schedule_status.md)
- [Shift](dimensions/shift.md)
- [Station id](dimensions/station_id.md)
- [Supplier name](dimensions/supplier_name.md)
- [Telemetry line id](dimensions/telemetry_line_id.md)

# Facts

- [Accepted component lot](facts/accepted_component_lot.md)
- [Alarm machine id](facts/alarm_machine_id.md)
- [Completed units](facts/completed_units.md)
- [Component serial number](facts/component_serial_number.md)
- [Cycle seconds](facts/cycle_seconds.md)
- [Drive serial number](facts/drive_serial_number.md)
- [Energy kwh](facts/energy_kwh.md)
- [Eol test run id](facts/eol_test_run_id.md)
- [First attempt serial](facts/first_attempt_serial.md)
- [First pass serial](facts/first_pass_serial.md)
- [Inspected component lot](facts/inspected_component_lot.md)
- [Operation execution id](facts/operation_execution_id.md)
- [Passed eol test run id](facts/passed_eol_test_run_id.md)
- [Planned units](facts/planned_units.md)
- [Rework operation execution id](facts/rework_operation_execution_id.md)
- [Shipped drive serial](facts/shipped_drive_serial.md)
- [Telemetry machine id](facts/telemetry_machine_id.md)
- [Temperature c](facts/temperature_c.md)

# Measures

- [Accepted component lot count](measures/accepted_component_lot_count.md)
- [Alarm event count measure](measures/alarm_event_count_measure.md)
- [Average cycle seconds measure](measures/average_cycle_seconds_measure.md)
- [Average temperature c measure](measures/average_temperature_c_measure.md)
- [Component count measure](measures/component_count_measure.md)
- [Eol attempt count](measures/eol_attempt_count.md)
- [First attempt unit count](measures/first_attempt_unit_count.md)
- [First pass unit count](measures/first_pass_unit_count.md)
- [Inspected component lot count](measures/inspected_component_lot_count.md)
- [Operation count measure](measures/operation_count_measure.md)
- [Passed eol attempt count](measures/passed_eol_attempt_count.md)
- [Rework operation count](measures/rework_operation_count.md)
- [Shipped units](measures/shipped_units.md)
- [Telemetry event count](measures/telemetry_event_count.md)
- [Total completed units](measures/total_completed_units.md)
- [Total operation energy kwh](measures/total_operation_energy_kwh.md)
- [Total planned units](measures/total_planned_units.md)

# Metrics

- [Alarm event count](metrics/alarm_event_count.md)
- [Average cycle seconds](metrics/average_cycle_seconds.md)
- [Average temperature c](metrics/average_temperature_c.md)
- [Component count](metrics/component_count.md)
- [Energy per operation kwh](metrics/energy_per_operation_kwh.md)
- [Eol attempt pass rate](metrics/eol_attempt_pass_rate.md)
- [First pass yield](metrics/first_pass_yield.md)
- [Incoming acceptance rate](metrics/incoming_acceptance_rate.md)
- [Operation count](metrics/operation_count.md)
- [Production completion rate](metrics/production_completion_rate.md)
- [Rework rate](metrics/rework_rate.md)
- [Shipped unit count](metrics/shipped_unit_count.md)

# Relationships

- [Component lot inspections component consumption](relationships/component_lot_inspections_component_consumption.md)
- [Customer orders production orders](relationships/customer_orders_production_orders.md)
- [Production orders serialized drives](relationships/production_orders_serialized_drives.md)
- [Serialized drives component consumption](relationships/serialized_drives_component_consumption.md)
- [Serialized drives eol test runs](relationships/serialized_drives_eol_test_runs.md)
- [Serialized drives operation executions](relationships/serialized_drives_operation_executions.md)
