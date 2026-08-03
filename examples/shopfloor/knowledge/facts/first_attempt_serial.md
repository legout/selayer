---
type: Selayer Fact
title: First attempt serial
description: Drive serial number on its first end-of-line attempt
selayer_id: fact.first_attempt_serial
generated:
  by: process:selayer-okf
  fingerprint: 6e2bcbc2fe071143e463eaec457e2221e67840e1f49931364a7a4ec89ce13e86
status: stable
---

# Catalog Definition

Semantic ID: `fact.first_attempt_serial`

Source: `eol_test_runs`

Data type: `string`

Expression: `if(eol_test_runs.attempt = 1, eol_test_runs.serial_number, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
