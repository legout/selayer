---
type: Reference
title: Shopfloor quality policy
status: stable
x_selayer_document_id: shopfloor.quality_policy
x_selayer_owner: manufacturing-analytics
x_selayer_version: "1"
---

# Purpose

This reference states the exact business rules tested by the shopfloor
fixture. The catalog is execution authority; this document explains the
quality invariants that the generated data satisfies.

# Inspection disposition rules

- A passing incoming inspection maps to `released`.
- A failing incoming inspection maps to `quarantined`.

# Rework

- A rework operation is identified by the `is_rework` flag on the operation
  execution, not by a separate operation source.

# End-of-line testing rules

- EOL attempts start at one and are unique per drive.
- First pass means attempt one and result `pass`.
- A later passing retest changes the attempt pass rate but not first-pass
  yield.

# Production completion

- Completed units cannot exceed planned units in this fixture.
