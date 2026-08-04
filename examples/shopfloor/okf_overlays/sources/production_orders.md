---
selayer_id: source.production_orders
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The production-order source records planned and completed units against a
customer order. One row per production order is the declared grain; the source
feeds schedule-status and product-model dimensions and the completion-rate
metric.

# Caveats

A production order may produce zero serialized drives when it is still open.
Completed units cannot exceed planned units in this fixture. This source is a
parent of serialized drives.
