---
type: Selayer Fact
title: Shipped drive serial
description: Serial number of a shipped drive
selayer_id: fact.shipped_drive_serial
generated:
  by: process:selayer-okf
  fingerprint: e7904157f304c02027533fffb2c91beb5359063596c7437785e4f1047b9566e2
status: stable
---

# Catalog Definition

Semantic ID: `fact.shipped_drive_serial`

Source: `serialized_drives`

Data type: `string`

Expression: `if(serialized_drives.shipment_status = 'shipped', serialized_drives.serial_number, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
