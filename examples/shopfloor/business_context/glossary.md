---
type: Reference
title: Shopfloor glossary
status: stable
x_selayer_document_id: shopfloor.glossary
x_selayer_owner: manufacturing-analytics
x_selayer_version: "1"
---

# Purpose

This glossary gives one canonical meaning to every term used by the shopfloor
fixture. Matching string values never establish a safe join; only catalog
relationships and declared grains are authoritative.

# Terms

- **Customer order**: a demand record for a product model from a customer.
- **Production order**: a manufacturing order with planned and completed units
  scheduled against a customer order.
- **Serialized drive**: a finished motor drive with a unique conformed drive
  serial number, produced under a production order.
- **Fitted component**: a component installed in a serialized drive, recorded
  at the fitting grain (one row per fitted position).
- **Component lot**: a supplier lot of components that may receive an incoming
  inspection.
- **Operation execution**: one recorded step in building a serialized drive,
  including rework; one row per execution.
- **Rework operation**: an operation execution flagged by `is_rework`.
- **EOL attempt**: one end-of-line test attempt for a serialized drive;
  attempts start at one and are unique per drive.
- **First-pass unit**: a drive that passes its first end-of-line attempt.
- **Telemetry event**: one independent machine sample row in the telemetry
  source, scoped to a telemetry machine.
- **Alarm event**: a telemetry event whose machine state is `alarm`.
- **Operation machine**: a machine recorded by an operation execution; a
  domain-specific identity not joined to telemetry.
- **Telemetry machine**: a machine recorded by a telemetry event; a
  domain-specific identity not joined to operation machines.
- **Conformed drive serial number**: the canonical drive identity shared
  across production, components, operations, and EOL testing.
- **Grain**: the exact set of columns that makes a source row unique;
  declared in the catalog and enforced by physical verification.
