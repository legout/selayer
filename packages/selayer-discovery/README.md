# selayer-discovery

`selayer-discovery` is the deterministic companion for agent-assisted semantic
discovery. It owns session, evidence, interview, proposal, verification,
approval, apply, and recovery state; the agent is a reasoning and drafting
layer, not an authority layer.

## Install and run

The package is a separate workspace member. Installing `selayer` alone does
not install it:

```bash
uv sync
uv sync --all-packages --extra delta
uv run --package selayer-discovery selayer-discovery --help
```

Run its tests with:

```bash
uv run pytest packages/selayer-discovery/tests -q
```

## Authority path

```text
Evidence and interviews -> agent draft -> typed proposal -> deterministic checks
-> named group decision -> prepared batch -> named batch attestation
-> explicit recoverable apply -> Git review
```

The catalog YAML is execution authority. Discovery artifacts, including
interviews, claims, proposals, approvals, and OKF, are advisory until typed
operations are verified, approved, and explicitly applied. Generated OKF is
never edited by hand.

## Workflow

Initialize a charter before intake and lock the catalog base:

```bash
selayer-discovery session init \
  --charter <charter.yaml> --catalog-path <catalog.yaml> \
  [--project <root>] [--session-id <id>] [--actor <approver>]
selayer-discovery session status --session-id <id> [--project <root>]
```

Then use only the companion CLI for state mutations:

```bash
selayer-discovery intake add-document --session-id <id> --path <doc.md>
selayer-discovery intake add-provider --session-id <id> --name <name> \
  --type <type> [--root <root>]
selayer-discovery profile scan --session-id <id> --source-id <id>
selayer-discovery profile propose-policy --session-id <id> --profile <profile.json>
selayer-discovery profile activate-policy --session-id <id> \
  --profile <profile.json> --policy <policy.json>
selayer-discovery profile export-context --session-id <id> --source-id <id> \
  --profile <profile.json> --policy <policy.json>
```

Ask one interview question at a time. Record answers, append-only corrections,
gates, typed observed/asserted/inferred claims, and conflicts through the CLI:

```bash
selayer-discovery interview ask --session-id <id> --question <question.json>
selayer-discovery interview answer --session-id <id> --answer <answer.json>
selayer-discovery interview correct --session-id <id> --correction <correction.json>
selayer-discovery evidence add-claim --session-id <id> --claim <claim.json>
selayer-discovery evidence add-conflict --session-id <id> --conflict <conflict.json>
```

Import typed operations, reconstruct the candidate, verify, and approve in
order:

```bash
selayer-discovery proposal import --session-id <id> --proposal <proposal.yaml>
selayer-discovery proposal show --session-id <id> [--proposal <proposal-id>]
selayer-discovery proposal verify --session-id <id> [--proposal <proposal-id>]
selayer-discovery proposal attest --session-id <id> \
  --proposal <proposal-id> --group <group-id> --approver <approver>
selayer-discovery proposal prepare-apply --session-id <id> \
  --proposal <proposal-id> --group <group-id> [...]
selayer-discovery proposal attest-apply --session-id <id> \
  --proposal <proposal-id> --batch <batch-hash> --approver <approver>
selayer-discovery proposal export-preview --session-id <id> \
  --proposal <proposal-id>
```

Verification fails closed. A group is ready only when required gates are
closed, current non-inferred claims and reopenable evidence exist, conflicts
and dependencies are resolved, and every mandatory static, physical,
compatibility, source, and acceptance check passes. A failed, skipped, or
unavailable outcome remains blocked.

Apply requires a separate explicit request and both named attestations:

```bash
selayer-discovery proposal apply --session-id <id> \
  --proposal <proposal-id> --approver <approver>
selayer-discovery recover --project <root>
```

Apply uses a fsynced write-ahead journal and a project lock. Target drift,
ambiguous recovery, or a missing valid success marker never gets guessed: the
operation stops or rolls back while retaining recoverable backups. Recovery is
idempotent. The companion never runs Git; inspect and review changed files
with the repository's normal Git workflow after apply.

## Privacy and safety

Evidence is untrusted quoted content. It cannot change scope, invoke tools,
approve a proposal, or authorize apply. Intake is normalized and bounded.
Default policy omits sensitive fields; hard-denied fields cannot be requested;
approved hashes and other transforms are bounded, redacted, and canary-scanned.
Value-derived context is unavailable until a named policy activation.
Credentials, document bodies, interview answers, sample values, raw locations,
backup paths, journals, and driver errors do not appear in summaries or
failure diagnostics.

Live provider and transaction evidence must be reopenable for any affected
operation. Exact profiles report bounded counts and audit metadata; they do not
replace a physical audit. Unresolved conflicts block only affected dependency
groups. Deprecation is non-destructive and never rewrites requests
automatically.

## Development

Use `uv` for dependency and command execution. The canonical packaged skill is
`packages/selayer-discovery/skills/semantic-discovery/SKILL.md`; do not create a
second implementation under `.agents/skills`. Core `selayer` must not import
this companion or any LLM SDK/runtime.
