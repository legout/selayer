---
selayer_id: source.eol_test_runs
sources:
  - resource: /references/process_overview.md
  - resource: /references/quality_policy.md
---

# Usage Guidance

The end-of-line test source records EOL test attempts for serialized drives.
The declared grain is one row per EOL attempt, identified by drive serial number
and attempt number. Attempts start at one and are unique per drive. This source
uses a Delta connector.

# Caveats

EOL data distinguishes attempts from drives. The attempt pass rate counts
attempts, not drives. First-pass yield counts distinct drives that pass on
attempt one. A later passing retest changes the attempt pass rate without
changing first-pass yield.
