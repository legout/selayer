---
selayer_id: source.serialized_drives
sources:
  - resource: /references/process_overview.md
---

# Usage Guidance

The serialized-drive registry owns the conformed drive serial number. One row
per serialized drive is the declared grain; this source is the identity anchor
for component genealogy, operation executions, and end-of-line tests.

# Caveats

Serialized drives are children of production orders. A drive ends in either the
shipped or in-stock shipment state. Matching serial-number strings in other
sources reference the same drive only through declared relationships, not string
equality alone.
