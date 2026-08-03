---
type: Selayer Fact
title: Passed eol test run id
description: Identifier of a passing end-of-line test run
selayer_id: fact.passed_eol_test_run_id
generated:
  by: process:selayer-okf
  fingerprint: 2d4f457f4c77101359e80b9f7e1ad55cfdac7ac5768d30902e52fc1631a52225
status: stable
---

# Catalog Definition

Semantic ID: `fact.passed_eol_test_run_id`

Source: `eol_test_runs`

Data type: `string`

Expression: `if(eol_test_runs.result = 'pass', eol_test_runs.eol_test_run_id, null)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
