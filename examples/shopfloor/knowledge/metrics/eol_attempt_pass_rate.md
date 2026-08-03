---
type: Selayer Metric
title: Eol attempt pass rate
description: Passing end-of-line attempts as a share of all attempts
selayer_id: metric.eol_attempt_pass_rate
generated:
  by: process:selayer-okf
  fingerprint: c2c291a38687f2700216b52191076d6f285b13ccad3776fac179c896faf13343
status: stable
---

# Catalog Definition

Semantic ID: `metric.eol_attempt_pass_rate`

Declared measures: `passed_eol_attempt_count`, `eol_attempt_count`

Expression: `passed_eol_attempt_count / nullif(eol_attempt_count, 0)`

# Usage Guidance

# Examples

# Caveats

# Related Concepts
