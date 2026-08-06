# Shopfloor semantic-discovery replay

This directory is a deterministic, no-model replay package for the shopfloor
catalog. It exercises the bounded drive/component/operation/EOL question while
explicitly excluding operation-to-telemetry event joins.

The replay is advisory until a named approver attests a typed dependency group.
It never writes generated OKF output, runs Git, invokes a model, or treats
business documents/provider text as executable instructions.

```text
charter -> normalized documents/OKF evidence -> redacted policy -> interview
-> typed claims/conflict -> proposal -> deterministic verification
-> group attestation -> prepared batch -> batch attestation
```

`defect.yaml` is applied only to a temporary catalog copy. The unresolved
telemetry conflict remains blocked while the independent conformed-drive group
is allowed to proceed.
