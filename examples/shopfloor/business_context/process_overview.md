---
type: Reference
title: Shopfloor process overview
status: stable
x_selayer_document_id: shopfloor.process_overview
x_selayer_owner: manufacturing-analytics
x_selayer_version: "1"
---

# Purpose

This fixture is a deterministic motor-drive teaching process. Every connector
input is regenerated from a single generator so that the catalog, grains,
relationships, and metrics can be verified exactly without external services.

# Order to shipment

A customer order captures demand for a product model. A production order is
opened against that order and schedules planned units. A production order may
produce one or more serialized drives, and an open order can have zero drives;
each serialized drive is identified by a conformed drive serial number from the
serialized-drive registry. A serialized drive ends in either the `shipped` or
`in_stock` shipment state.

# Component genealogy

A serialized drive has fitted components drawn from component lots. Component
consumption is recorded at the fitting grain: one row per fitted component
position, not one row per distinct component identity. Each component lot may
have an incoming lot inspection that records an incoming result and a
disposition.

# Operation execution

Operation executions are recorded one row per execution, including rework
operations. An operation is marked as rework through the `is_rework` flag on
the operation execution. Operation machines and lines are domain-specific to
the operation-execution source and are not joined to telemetry machines.

# End-of-line testing

End-of-line (EOL) testing records attempts. Attempts start at one and are unique
per drive. The EOL attempt pass rate counts attempts: passing attempts divided
by all attempts. First-pass yield counts distinct drives: drives that pass on
attempt one divided by distinct drives with a first attempt. A later passing
retest changes the attempt pass rate but does not change first-pass yield.

# Telemetry

Machine telemetry records independent machine samples. Each telemetry event is
a row scoped to a telemetry machine. There is deliberately no safe
operation-to-telemetry relationship: matching machine string values between
operation executions and telemetry do not establish a safe join.

# Data authority

Catalog YAML is execution authority; this document explains. Generated fields
and `Catalog Definition` come only from the catalog. This reference is advisory
and never changes query planning.
