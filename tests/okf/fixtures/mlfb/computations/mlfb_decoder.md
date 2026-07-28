---
type: Attested Computation
title: Synthetic MLFB decoder
runtime: python
status: stable
parameters:
  - {name: mlfb, type: string, required: true}
executor:
  resource: ../references/mlfb_coding_guide.md
  receipt: [decoded_value]
attester:
  resource: ../references/mlfb_coding_guide.md
---

# Computation

    def decode(mlfb: str) -> str:
        # Illustrative only; contains no proprietary decoding logic.
        return mlfb

# Meaning

This illustrative fixture records that an approved decoder may interpret documented
positions. It contains no executable or proprietary decoding logic.
