---
type: Selayer Fact
title: First pass serial
description: Drive serial number that passed on the first attempt
selayer_id: fact.first_pass_serial
generated:
  by: process:selayer-okf
  fingerprint: be5970613c4d1316d21220dd237fe6793e566e582299921478fa07a92b4369e7
status: stable
---

# Catalog Definition

Semantic ID: `fact.first_pass_serial`

Source: `eol_test_runs`

Data type: `string`

Expression: `if(eol_test_runs.is_first_pass = true, eol_test_runs.serial_number, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
