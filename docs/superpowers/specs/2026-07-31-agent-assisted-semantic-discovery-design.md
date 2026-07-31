# Agent-assisted semantic discovery design

## Purpose

Design a safe, auditable workflow that helps a domain expert and data engineer discover, correct, and enrich a selayer semantic model from source structure, exact verification evidence, normalized business documents, a read-only external knowledge base, and an adaptive interview.

The workflow uses an Agent Skill for reasoning and a standalone `selayer-discovery` package for deterministic state, evidence, proposal, verification, approval, and apply operations. Core selayer remains agent-free and authoritative for catalog validation, planning, source access, physical audits, and OKF composition.

## Status

Approved design.

The following decisions were approved during design review:

- facilitated discovery with domain experts and data engineers;
- one named approver per session;
- an explicit apply command after approval;
- normalized Markdown and text inputs in version 1;
- a read-only external knowledge-provider interface;
- local drafts and no discovery-time wiki writes;
- an Agent Skill plus standalone companion package;
- no LLM SDK in the companion package;
- aggregate source profiles plus policy-approved redacted samples;
- agent-proposed sample policy activated by the named approver;
- an adaptive interview with required gates;
- a bounded business question and source scope per session;
- append-only answer corrections;
- approval by atomic dependency groups;
- unresolved conflicts block only dependent groups;
- additions, edits, and deprecations, but no hard deletion or ID rename;
- first-class catalog deprecation metadata;
- approved summaries are versioned while raw sessions stay local;
- local approval attestation backed by normal Git review and merge controls;
- a uv workspace with an independently publishable `selayer-discovery` member.

## Context

Core selayer deliberately excludes chat, LLM orchestration, RAG, embeddings, and wiki publishing. The catalog YAML controls execution. OKF is advisory. The verification design adds deterministic static, physical, compatibility, and OKF checks, while the shopfloor design supplies a small teaching fixture and reviewed business corpus.

Semantic discovery requires a different trust model. Business meaning does not come from schemas alone. Documents can be stale, interviews can be corrected, existing catalogs can be wrong, and source values can be sensitive. An agent can synthesize and ask useful questions, but it cannot be the authority for grain, cardinality, KPI policy, or repository changes.

The retired `legacy/selayer_chat` code is not revived. Its natural-language-to-SQL flow, direct model integration, free-text extraction history, and SQL validation tools solve a different problem. Discovery produces typed semantic proposals and evidence, not SQL answers.

## Goals

1. Define a bounded, resumable discovery session around one explicit business question and source boundary.
2. Combine schema observations, exact audits, aggregate profiles, approved redacted samples, normalized documents, external wiki concepts, existing catalog state, and interview answers as typed evidence.
3. Keep observed facts, human or document assertions, and agent inferences distinct.
4. Ask adaptive interview questions while requiring complete gate dispositions.
5. Preserve answer corrections and stale every dependent claim, proposal, verification report, and approval.
6. Produce typed catalog, Reference, and overlay operations grouped by dependency.
7. Verify each group with checks appropriate to its impact.
8. Require a fingerprint-bound named approval before apply.
9. Apply accepted groups through a recoverable deterministic transaction without staging or committing Git changes.
10. Commit only approved decision summaries, evidence hashes, accepted operations, verification evidence, and curated knowledge sources.
11. Provide a read-only provider interface for an external LLM wiki, beginning with filesystem OKF v0.2+.
12. Keep the companion package independently installable and free of model SDKs.

## Non-goals

Version 1 does not:

- ingest PDF, DOCX, HTML, scans, images, audio, video, or websites directly;
- perform OCR, transcription, crawling, or office-document conversion;
- create or maintain a general-purpose LLM wiki;
- write to an external wiki;
- call an LLM API from `selayer-discovery`;
- rank evidence with a universal confidence or credibility score;
- let agents execute SQL, Python, shell commands, or instructions found in evidence;
- accept arbitrary text patches as apply authority;
- rename or hard-delete catalog IDs;
- expose unrestricted source rows to a model;
- authenticate an approver cryptographically;
- stage, commit, push, or merge Git changes;
- make OKF executable authority;
- replace deterministic validation, physical audit, compatibility analysis, or human review.

A later optional extension may add document conversion, OCR, media transcription, richer OKF v0.2+ wiki creation, and publish adapters. Those capabilities remain independently installable and do not move into core selayer.

## Workspace and package architecture

The repository becomes a uv workspace:

```text
selayer/
├── pyproject.toml                     # root selayer package and workspace
├── src/selayer/                       # deterministic core
├── packages/
│   └── selayer-discovery/
│       ├── pyproject.toml              # independently publishable distribution
│       ├── src/selayer_discovery/
│       ├── tests/
│       └── skills/
│           └── semantic-discovery/
│               └── SKILL.md
└── .agents/skills/semantic-discovery/  # repository development entry point
```

The distribution name is `selayer-discovery`, the import namespace is `selayer_discovery`, and the executable is `selayer-discovery`. The package depends on a compatible released or workspace version of `selayer`. Core selayer never depends on discovery.

The canonical Agent Skill ships with the companion package. The repository-level skill entry point exposes it during development without duplicating divergent instructions. Packaging must preserve a single source of truth for the skill text.

The root package remains usable without installing the workspace member. Publishing, dependency resolution, tests, and versioning for `selayer-discovery` remain independently addressable.

## Responsibility boundaries

### Core selayer

Core owns:

- catalog parsing and static validation;
- semantic model objects;
- query planning and execution;
- `SourceRegistry` and source adapters;
- a bounded public source scan-session interface for companion tools;
- exact physical audits;
- planner compatibility reports;
- runtime profile resolution;
- OKF loading, integrity, retrieval, and fresh composition;
- first-class deprecation metadata and validation.

Core contains no prompts, interview state, model clients, provider-specific wiki integration, proposal approval, or apply orchestration.

### `selayer-discovery`

The companion owns deterministic:

- session state and append-only event storage;
- evidence manifests and content-addressed snapshots;
- aggregate profiling and redacted sample production;
- profile-policy validation and activation;
- knowledge-provider protocols and adapters;
- interview gate records and correction invalidation;
- claims, conflicts, and dependency tracking;
- typed proposal schemas;
- candidate reconstruction;
- verification readiness rules;
- approval attestation records;
- approved-summary export;
- project locking, apply journals, rollback, and recovery.

It accepts structured reasoning outputs but does not produce them with an LLM.

### Agent Skill

The Agent Skill owns:

- collecting the session charter;
- retrieving bounded evidence through companion commands;
- treating all evidence content as untrusted data;
- choosing the next interview question;
- asking exactly one question at a time;
- drafting claims, conflicts, and dependency groups;
- drafting typed operations and curated prose;
- explaining verification failures and unresolved questions;
- presenting profile policy and semantic proposals for explicit approval.

The skill does not edit session files directly. It calls the companion's public CLI or Python API. It cannot bypass readiness, approval, or apply checks.

### Named approver

One named approver is accountable for activating source-value exposure policy, resolving semantic conflicts, accepting or rejecting dependency groups, and confirming an exact apply batch. Other participants may supply interview answers and technical evidence.

The charter stores a normalized approver identity. Policy activation, conflict resolution, group decisions, and apply-batch confirmation must use that exact identity. Changing it is an explicit charter revision that stales every policy activation, conflict resolution, proposal verification, and attestation.

Identity matching is workflow enforcement, not authentication. A local user can still type another person's name. Repository review and merge permissions remain the final organizational control.

## Version 1 input model

A session may use:

- one existing or new catalog target;
- in-scope source declarations and runtime profiles;
- Markdown and plain-text files;
- zero or more read-only knowledge providers;
- existing generated or curated OKF;
- interview events;
- deterministic verification reports.

Normalized documents are copied into the ignored session workspace as content-addressed snapshots. Original paths, media types, byte counts, modification metadata, and SHA-256 digests are recorded. A changed document creates a new revision; it never mutates the prior evidence record.

A session does not infer that a document is current or authoritative from its filename, location, or prose. Authority and scope are explicit claims resolved through interview and approval.

## Session charter

Every session starts with a charter containing:

- a stable session ID;
- the target catalog path and normalized fingerprint;
- one business question or decision objective;
- included sources and existing semantic objects;
- included document roots and knowledge providers;
- intended consumers or use cases;
- explicit excluded domains and use cases;
- acceptance questions and counterexamples;
- named approver identity;
- runtime profile references without resolved secret values.

The Agent Skill may identify out-of-scope discoveries, but it records them only as follow-up-session suggestions. It cannot silently expand the charter.

## Session state machine

Session state is derived from an append-only event journal. Materialized state is a cache and can be rebuilt.

The primary states are:

1. `initialized`: charter and base fingerprints recorded.
2. `intake`: schemas, documents, provider resources, and existing model state captured.
3. `sample_policy_pending`: aggregate profile exists and value-exposure policy awaits approval.
4. `interviewing`: activated evidence context and adaptive interview gates are in progress.
5. `drafting`: claims, conflicts, and typed change sets are being assembled.
6. `review_ready`: at least one dependency group satisfies all readiness rules.
7. `approved`: at least one ready group has a current approval attestation.
8. `applied`: one or more approved groups were applied successfully.
9. `closed`: accepted, rejected, deferred, and blocked groups have final dispositions for this session.

A session may return from `drafting` or `review_ready` to `interviewing` when new questions arise. It may contain ready and blocked groups simultaneously.

Dependency-group states are:

- `draft`;
- `blocked`;
- `ready`;
- `accepted`;
- `rejected`;
- `deferred`;
- `stale`;
- `applied`.

State transitions are validated by the companion. Direct file edits do not create valid transitions.

## Local and committed artifacts

Raw discovery artifacts live under an ignored project directory:

```text
.selayer/discovery/sessions/<session-id>/
├── session.yaml
├── events.jsonl
├── evidence/
│   ├── manifest.jsonl
│   └── snapshots/<sha256>
├── profile/
│   ├── schema.json
│   ├── aggregates.json
│   ├── sample-policy.proposed.yaml
│   ├── sample-policy.active.yaml
│   └── redacted-samples.json
├── interview/events.jsonl
├── claims.jsonl
├── conflicts.jsonl
├── change-sets/<change-set-id>/
│   ├── proposal.yaml
│   ├── candidate-catalog.yaml
│   ├── reference-drafts/
│   ├── overlay-drafts/
│   └── verification.json
└── approvals.jsonl
```

The local workspace may contain normalized source text, redacted values, complete interview answers, provider snapshots, model drafts, and recovery backups. It is never committed.

Approved summaries export to a configured versioned directory, defaulting to:

```text
semantic_changes/<date>-<slug>/
├── decision.md
├── proposal.yaml
├── evidence.lock.json
├── catalog.patch
├── references/
├── overlays/
├── verification.json
└── approval.json
```

The committed summary contains accepted dependency groups only. It records evidence resource IDs, revisions, selectors, and hashes, but not raw document excerpts, source samples, complete transcripts, credentials, or model prompts.

`catalog.patch` is a review preview. Typed operations remain apply authority.

## Artifact schema and fingerprints

Every machine-readable artifact has an explicit schema version. Version 1 artifacts use canonical JSON for hashing regardless of their presentation format:

- UTF-8;
- sorted mapping keys;
- preserved list order where order is semantic;
- normalized numbers and booleans;
- no timestamps, filesystem paths, or presentation text in semantic fingerprints unless explicitly part of the record.

SHA-256 fingerprints bind:

- the charter;
- base catalog;
- source declarations;
- runtime profile references;
- documents and provider revisions;
- aggregate profile;
- active sample policy;
- interview answers and corrections;
- claims and conflict resolutions;
- each dependency group;
- reconstructed candidate;
- verification report;
- approval attestation.

Changing an input marks all transitive dependents stale. A stale artifact cannot be made current by editing its status. It must be reconstructed or rerun from current inputs.

## Evidence model

### Evidence records

An evidence record identifies a source snapshot:

```text
record_id
kind
source_reference
source_revision
content_hash
media_type
selector_model
captured_at
size
```

Supported kinds include:

- `catalog`;
- `schema`;
- `aggregate_profile`;
- `redacted_sample`;
- `physical_audit`;
- `compatibility_report`;
- `document`;
- `knowledge_resource`;
- `interview_answer`;
- `verification_report`.

Selectors are kind-specific and immutable, such as a JSON Pointer, table and column name, line range, OKF resource and section, interview event ID, or verification outcome ID.

### Claims

Claims separate evidence from interpretation. Each claim has:

- a stable claim ID;
- a subject;
- a plain declarative statement;
- a class: `observed`, `asserted`, or `inferred`;
- supporting evidence selectors;
- contradicting claim IDs where known;
- state: `current`, `superseded`, `rejected`, or `stale`;
- creator and event references.

`observed` claims come from deterministic schema, profile, audit, or planner results. They are authoritative only for what was measured. A uniqueness scan can establish uniqueness in the scanned snapshot; it cannot establish the business meaning of the key.

`asserted` claims come from people, documents, external knowledge, or existing catalog intent. They may disagree.

`inferred` claims are agent hypotheses. They never satisfy a proposal's evidence requirement by themselves.

The system does not assign a universal confidence number. It does not automatically rank an interview above a handbook, or a current catalog above a process owner. Physical observations resolve physical questions only. The named approver resolves semantic disagreements.

### Conflicts

A conflict records:

- subject;
- involved claim IDs;
- why they cannot all hold for the proposal;
- affected dependency-group IDs;
- state: `unresolved` or `resolved`;
- resolution statement;
- resolving answer or evidence ID;
- resolver identity and timestamp.

Semantic conflict resolution requires the charter's current named approver. Deterministic conflicts, such as a stale fingerprint or failed physical check, are resolved only by new passing evidence rather than an attestation. An unresolved conflict blocks only dependency groups that cite it or depend on affected claims. Independent groups may continue.

## Aggregate profiling and redacted samples

### Local exact profile

The companion profiles in-scope sources through core source adapters and runtime profiles. It never opens connectors itself through ad hoc code.

Discovery adds one narrow deterministic core seam that is separate from verification:

```python
class SourceScanSession(Protocol):
    consistency: SourceConsistency
    snapshot_id: str | None
    schema: TableSchema

    def iter_batches(
        self,
        columns: tuple[str, ...],
        *,
        batch_size: int,
    ) -> Iterator[pa.RecordBatch]: ...

    def recheck_snapshot(self) -> bool: ...


SourceRegistry.open_scan_session(
    source_name,
    *,
    runtime_profile,
) -> ContextManager[SourceScanSession]
```

The interface exposes typed batches and snapshot metadata, not a raw connector handle or arbitrary SQL. Adapters remain responsible for connection setup, runtime credentials, schema normalization, transaction lifetime, cancellation, and cleanup. Profiling algorithms remain in `selayer-discovery`; this does not add profiling to the verification orchestrator.

The aggregate profile performs a full exact scan and records:

- source row count;
- null count per field;
- exact distinct count per field;
- numeric and temporal minimum and maximum where valid;
- declared-grain null and duplicate outcomes;
- source consistency mode and snapshot identity when available;
- `passed`, `failed`, `skipped`, or `unavailable` outcomes.

Aggregate profiling is evidence, not a replacement for `PhysicalCheck`. Physical audits remain the authority for declared grains and relationships.

Each adapter reports one consistency mode:

- `reopenable_snapshot`: a file digest, Delta version, time-travel version, or equivalent token can reacquire the same revision during verification and apply;
- `transaction_snapshot`: one scan session is internally consistent, but its revision cannot be reacquired after the session closes;
- `live`: the adapter cannot prove that scans observed one stable revision.

Version 1 allows source-data evidence to authorize an executable source, schema, grain, relationship, type, or expression change only from a `reopenable_snapshot`. Apply reacquires the exact token, calls `recheck_snapshot()`, and reruns mandatory evidence against that revision.

`transaction_snapshot` and `live` profiles may guide interview questions, asserted claims, planning-only cases, and advisory Reference or overlay changes. They cannot satisfy the non-inferred evidence requirement for a data-dependent executable operation. Such a group reports `discovery.source.snapshot_unavailable` and remains blocked until the operator supplies a reopenable source version or immutable extract.

This restriction is intentional. Version 1 does not claim that a short-lived database transaction remains authoritative across a human approval interval. A later adapter design may add durable exported snapshots or warehouse time-travel tokens.

Exact profiling has bounded resource behavior without weakening exactness. The operator configures a per-source timeout, defaulting to 900 seconds, and may cancel a scan. Timeout, cancellation, adapter cost refusal, or partial query failure yields `unavailable`; partial aggregates never create claims. Reopenable snapshots may resume content-addressed field batches against the same snapshot token. Live and transaction snapshots restart from the beginning. Concurrency is bounded by adapter and project policy.

No raw values are exported to the Agent Skill during this phase.

### Proposed exposure policy

A conservative local classifier inspects field metadata, types, aggregate statistics, and local value patterns. It emits suggested classifications without returning raw values to the model.

The Agent Skill converts these suggestions into a proposed policy. The policy defaults every field to `omit` and allows only:

- `omit`: no value or value-derived token leaves the local profiler;
- `redact`: expose only structural tokens such as null or non-null;
- `hash`: expose session-salted hashes for equality analysis;
- `bucket`: expose approved numeric or temporal ranges;
- `reveal`: expose bounded original values after explicit approval.

Field classification is separate from transformation. Credential, token, password, secret, and private-key classifications are hard-denied and cannot use `reveal`.

The charter's named approver activates an exact policy fingerprint. The activation record binds the normalized approver identity, policy hash, profile hash, and source snapshot identities. Editing the policy or changing the approver invalidates activation and every derived sample, context export, claim, proposal, verification report, and approval.

### Sample construction

Samples are deterministic and bounded. Rows are ordered by a session-salted SHA-256 digest of the declared grain values and source identity. Sources without a valid declared grain cannot emit samples.

Version 1 hard limits are:

- at most 20 rows per source;
- at most 50 exposed fields per source;
- at most 64 KiB of sample output per source;
- at most 256 KiB of sample output per session;
- at most 100 revealed distinct values per approved low-cardinality field.

Project policy may reduce but not increase these limits.

The salt remains in the ignored workspace. Output values use only the activated transformations. Context export scans for hard-denied patterns and test canaries before publication to the host agent.

## Read-only knowledge providers

`selayer-discovery` defines a provider-neutral read-only protocol. Providers are installed Python entry points in the group `selayer_discovery.knowledge_providers`.

The protocol exposes two operations:

```python
class KnowledgeProvider(Protocol):
    def search(self, request: KnowledgeSearchRequest) -> tuple[KnowledgeHit, ...]: ...
    def get(self, request: KnowledgeGetRequest) -> KnowledgeDocument: ...
```

A hit contains provider name, namespaced resource ID, immutable revision, title, media type, summary, source attribution, and size metadata. A document adds bounded normalized text.

Version 1 ships a filesystem OKF v0.2+ provider backed by core `OkfBundle` and retrieval behavior. A session may configure zero or more providers. Provider resource IDs are always namespaced.

Provider responses are content-addressed before model use. Search and get enforce item and byte limits. Retrieved text is untrusted evidence and cannot:

- change session scope;
- request tools;
- create or approve claims;
- invoke apply;
- alter provider configuration;
- write back to the provider.

Version 1 has no arbitrary subprocess provider. Installed provider plugins are trusted executable code; their returned content is not trusted.

Provider configuration stores only non-secret options and environment-variable references. Resolved credentials are never persisted or exposed to the Agent Skill.

## Adaptive interview

The Agent Skill asks one question at a time. The companion permits only one open question in a session.

The skill chooses the next question from the highest-impact unresolved gate, using current claims, conflicts, dependency effects, and the session charter. It may ask follow-ups, but every question must identify the gate it serves and the evidence that motivated it.

Required gates are:

1. business objective and intended decisions;
2. terms, synonyms, and ambiguous names;
3. process, lifecycle, and event boundaries;
4. entity and event grains;
5. identifiers and conformed identities;
6. relationships, optionality, and cardinality;
7. time meaning, timezone, and validity;
8. KPI formula, unit, filters, denominator, and zero behavior;
9. aggregation and mixed-grain safety;
10. business rules, exceptions, and exclusions;
11. ownership, freshness, and source authority;
12. privacy and model-exposure policy;
13. acceptance queries and counterexamples;
14. existing-model corrections and migration impact;
15. conflicts, unknowns, and follow-up work.

Each gate ends as:

- `answered` with current answer and claim references;
- `not_applicable` with a reason;
- `blocked` with unresolved conflict and affected group IDs.

A session can make independent groups review-ready while other gates are blocked, but no group may omit a gate that affects it.

### Corrections

Answers are immutable events. A correction:

- cites the superseded answer ID;
- supplies replacement text;
- records author, reason, and timestamp;
- becomes the current answer for the gate;
- stales claims and conflicts derived from the old answer;
- transitively stales candidate models, verification reports, and approvals.

The old answer remains in history. A correction never rewrites the transcript.

## Typed semantic proposals

A proposal contains one or more atomic dependency groups. A group contains:

- stable group ID and title;
- business rationale;
- dependencies on other groups;
- supporting non-inferred claim IDs;
- relevant inferred claim IDs;
- conflict IDs and resolution state;
- acceptance and counterexample query cases;
- typed operations;
- deterministic impact flags;
- candidate and verification fingerprints.

### Catalog operations

Version 1 supports:

- `catalog.add`;
- `catalog.edit`;
- `catalog.deprecate`.

Each operation contains:

- operation ID;
- fully qualified target ID;
- normalized before state or an absent marker;
- before-state hash;
- complete normalized after state;
- changed-field set derived by the companion;
- evidence claim IDs;
- dependency-group IDs.

Agents do not supply the impact flags. The companion derives flags such as:

- source or connector changed;
- schema changed;
- grain changed;
- relationship changed;
- data type changed;
- expression changed;
- aggregation changed;
- metric formula changed;
- ID deprecated.

Version 1 rejects hard deletion, ID rename, unknown catalog keys, and edits outside the target object.

### Knowledge operations

Version 1 supports:

- `reference.create`;
- `reference.update`;
- `overlay.create`;
- `overlay.update`.

Reference operations target reviewed authored Reference source directories. Overlay operations may change only fields and curated sections permitted by `OkfBundle.build()`. They cannot edit generated frontmatter, generated relationships, `Catalog Definition`, or generated output directories.

Document and interview evidence may support a Reference or overlay draft, but the draft remains an agent inference until approved.

### Review preview

The companion reconstructs a complete candidate catalog, References, overlays, and generated OKF in a candidate directory. It renders a deterministic catalog patch and knowledge diff for review. These diffs are previews, not executable input.

## First-class deprecation in core selayer

Version 1 discovery needs a non-destructive migration mechanism. Core catalog objects gain:

```yaml
status: active | deprecated
replaced_by: <fully-qualified-semantic-id>  # optional
```

`status` defaults to `active`. The fields apply to data sources, dimensions, facts, measures, metrics, and relationships.

Static validation enforces:

- `replaced_by` is allowed only when status is `deprecated`;
- the replacement exists;
- the replacement has the same semantic kind;
- an object cannot replace itself;
- replacement chains contain no cycles;
- deprecated relationships retain valid source and target declarations;
- active catalogs without the fields retain current behavior.

Deprecated IDs continue to plan and execute. Deprecation is migration metadata, not removal. Core behavior changes only in reporting:

- catalog validation emits a non-blocking deprecation diagnostic;
- compatibility reports identify query cases that use deprecated objects and their replacement IDs;
- OKF generation emits deprecated status and a replacement link when present;
- retrieval retains deprecated concepts so migration guidance remains discoverable.

Query result semantics do not change. Version 1 does not automatically rewrite requests to replacements.

## Verification readiness

A dependency group is review-ready only when:

- every affecting interview gate has a disposition;
- every operation cites at least one current non-inferred claim;
- every cited evidence revision is current;
- no unresolved conflict affects the group;
- all dependencies are ready or accepted;
- candidate reconstruction succeeds;
- every mandatory verification outcome is complete and passed;
- no mandatory outcome is skipped or unavailable;
- the proposal, candidate, evidence, and verification fingerprints match.

Mandatory checks are derived from impact flags:

| Change | Required verification |
|---|---|
| Any catalog operation | static catalog validation of the reconstructed candidate |
| Source, schema, or grain | exact full-scan source grain audit against a reopenable snapshot |
| Relationship | exact cardinality and referential-integrity audit in the declared direction |
| Dimension or fact type/expression | static expression, source-column, and type checks |
| Measure or metric expression | static checks, scoped compatibility cases, and acceptance queries |
| Deprecation | replacement-graph validation and affected compatibility cases |
| Reference or overlay | fresh atomic OKF build, strict integrity validation, and curated-content policy |

Verification uses core public APIs. The companion does not reproduce planner, adapter, catalog, or OKF logic.

### Acceptance query cases

Proposal cases use semantic `QueryRequest` values, never raw SQL. A case is one of:

- expected compatible plan;
- expected planner rejection with stable code;
- optional execution assertion over approved fixture or runtime data.

Execution assertions may check schema, row count, null behavior, or exact/tolerant scalar values. Verification reports store assertion outcomes and result digests, not unrestricted result rows.

Changing an acceptance case changes the group fingerprint and invalidates prior verification and approval.

## Approval model

The charter's named approver chooses `accepted`, `rejected`, or `deferred` for each ready dependency group. The companion rejects a decision whose normalized actor identity does not match the current charter.

A group approval attestation records:

- schema version;
- session and group IDs;
- approver identity;
- decision and optional reason;
- charter fingerprint;
- base catalog fingerprint;
- active sample-policy fingerprint;
- evidence-lock fingerprint;
- proposal-group fingerprint;
- candidate fingerprint;
- verification fingerprint;
- attestation timestamp;
- a fixed statement explaining that the record is not a digital signature.

The companion refuses to attest a blocked, stale, or incompletely verified group. Any bound fingerprint change invalidates the attestation.

Group approval authorizes semantic intent and the exact typed operations. It does not authorize an arbitrary combination of groups. Before apply, `proposal prepare-apply` receives an explicit dependency-closed set of accepted group IDs. It rejects two groups that target the same semantic object or curated section; overlapping operations must be combined into one atomic dependency group. It reconstructs one batch candidate from the common base, reruns the union of mandatory checks, and creates a batch preview.

The named approver then creates an apply-batch attestation bound to:

- the ordered group IDs and group-attestation hashes;
- dependency closure;
- common base catalog and authored-knowledge hashes;
- combined candidate hash;
- combined verification hash;
- approved-summary hash.

Changing the selection or applying another batch changes the base and stales every unapplied group and batch attestation. Those groups must be rebased, reconstructed, reverified, and re-attested. Approval does not write repository files. Apply remains a separate explicit operation.

## Apply transaction

`selayer-discovery proposal apply` accepts one current apply-batch attestation. It performs these steps:

1. acquire a project-scoped apply lock;
2. reject concurrent apply or recovery work;
3. verify target paths remain within configured project roots and are not escaping symbolic links;
4. compare every target file with the attested common-base fingerprint;
5. validate group decisions, dependency closure, non-overlap, and apply-batch attestation;
6. reconstruct the combined candidate only from typed operations;
7. recheck source snapshot identities and rerun all mandatory verification;
8. require the canonical candidate, verification, and approved-summary hashes to match the batch attestation;
9. stage every new target in a sibling candidate directory and fsync the staged files and directory;
10. create content-addressed backups with restrictive permissions;
11. write a journal entry for every target containing target path, expected old hash or absent marker, backup path, staged path, expected new hash, and `pending` state;
12. fsync all backups, the complete journal, and their containing directories before the first target mutation;
13. before each replacement, write and fsync `next_target`; replace the target; fsync the target and containing directory; then record and fsync `replaced` with the observed new hash;
14. after all replacements, write and fsync a success marker containing the final target hashes;
15. record the applied session event and fsync it;
16. remove recovery backups only after the durable success marker and applied event exist.

The transaction never edits generated OKF output. It writes authored References and overlays, then the normal fresh OKF build produces disposable output.

Cross-file replacement is recoverable rather than falsely described as filesystem-atomic. If an exception occurs before the durable success marker, apply attempts rollback in reverse target order. It restores a target only when its current hash matches the journal's expected new hash, leaves an already restored old hash unchanged, verifies every restored old hash, and records a durable `rolled_back` marker. If a target matches neither old nor new hash, recovery stops with a conflict and preserves backups for manual inspection.

After a process crash, every mutation command refuses to continue until `selayer-discovery recover` runs. Without a valid success marker, recovery always rolls back; it never guesses that a partial transaction should complete. With a valid success marker, recovery verifies all new hashes and finalizes the applied event when that event is absent. Repeated recovery is idempotent.

Apply does not inspect or modify unrelated dirty files. It never invokes Git, stages files, creates commits, or pushes branches.

## Public companion interfaces

The exact implementation may split modules, but the public concepts are:

```python
SessionStore
SessionCharter
EvidenceRecord
EvidenceClaim
EvidenceConflict
AggregateProfile
SamplePolicy
KnowledgeProvider
InterviewQuestion
InterviewAnswer
InterviewGate
ChangeSet
CatalogOperation
KnowledgeOperation
VerificationBundle
ApprovalAttestation
ApplyJournal
```

The CLI is machine-readable first. Every command supports deterministic JSON output and stable error codes. Planned command groups are:

```text
selayer-discovery session init|status|close
selayer-discovery intake add-document|add-provider|snapshot
selayer-discovery profile scan|propose-policy|activate-policy|export-context
selayer-discovery interview ask|answer|correct|set-gate
selayer-discovery evidence add-claim|add-conflict|resolve-conflict
selayer-discovery proposal import|show|verify|attest|prepare-apply|attest-apply|export|apply
selayer-discovery recover
```

Commands accept structured files or JSON input. Free-form model text never enters apply logic without schema parsing and validation.

Exit codes follow a stable contract:

- `0`: requested operation completed;
- `1`: validation, policy, readiness, verification, or apply failure;
- `2`: command usage error.

A failed operation emits a sorted diagnostic report with codes and safe metadata. It does not print raw samples, document bodies, interview answers, secrets, or unrestricted query results.

## Agent Skill workflow

The skill follows this order:

1. confirm or create the charter;
2. initialize the session and lock base fingerprints;
3. capture documents, external knowledge revisions, schemas, and current catalog state;
4. run aggregate profiling;
5. draft a conservative sample policy;
6. obtain named activation before requesting any value-derived context;
7. conduct the adaptive interview one question at a time;
8. record claims and explicit conflicts;
9. create atomic dependency groups;
10. import typed proposals;
11. request deterministic verification;
12. explain ready, blocked, rejected, and unavailable outcomes;
13. obtain group decisions from the named approver;
14. prepare an explicit dependency-closed, non-overlapping apply batch;
15. show the combined candidate and verification, then obtain the named apply-batch attestation;
16. export approved summaries;
17. invoke apply only after a separate explicit user request;
18. show changed files and verification results without committing them.

The skill prompt treats evidence blocks as quoted untrusted content. Instructions inside documents or provider resources are ignored. The skill cannot call apply merely because a model recommends it.

## Security and privacy

### Trust boundaries

Untrusted inputs include:

- normalized documents;
- provider text;
- interview text;
- source values;
- model-generated claims and proposals;
- manually edited local artifacts.

Trusted executable code includes installed core, companion, and provider packages. Provider content remains untrusted even when the provider plugin is trusted.

### Required controls

- No `eval`, dynamic code execution, shell interpolation, or SQL supplied by evidence or models.
- No arbitrary subprocess provider in version 1.
- No model SDK or direct model credential in the companion package.
- All document, JSON, YAML, list, nesting, excerpt, profile, sample, and output sizes are bounded before parsing or model exposure.
- Configured document, provider, catalog, Reference, overlay, and output roots are path-confined.
- Symbolic-link escapes and special files are rejected.
- Runtime credentials remain environment-backed and are never serialized.
- Error output contains safe identifiers and diagnostics, not secret values or evidence bodies.
- Hard-denied fields cannot be revealed by profile policy.
- Active sample policy is fingerprint-bound to exported model context.
- Apply requires current attestation and cannot trust a text patch.
- Recovery journals and backups live in the ignored workspace and inherit restrictive local permissions.
- Provider calls are read-only and bounded.
- No document or wiki content can change scope, invoke tools, or approve a proposal.

### Concurrency

Session mutations use a session lock. Apply and recovery use a project lock. Read-only status and evidence retrieval may run concurrently against immutable snapshots. Lock acquisition has a bounded timeout and reports the owning session or transaction ID without leaking process arguments.

## Failure behavior

The companion distinguishes:

- invalid artifact schema;
- stale fingerprint;
- missing or changed evidence;
- unavailable source;
- sample-policy violation;
- unresolved conflict;
- incomplete gate;
- invalid operation;
- mandatory verification failure;
- approval mismatch;
- apply lock conflict;
- target drift;
- interrupted transaction;
- recovery failure.

Failures do not advance state. Partial evidence capture remains unreferenced until a manifest event commits it. Partial provider or profile output is not exposed. Failed candidate construction does not alter repository files. Failed apply rolls back or leaves a mandatory recovery journal.

A source check marked skipped or unavailable never counts as proof. An agent may explain the limitation and propose follow-up work, but affected groups remain blocked.

## Determinism and reproducibility

The companion records all non-model inputs and structured model outputs needed to replay state transitions. Package tests and proposal verification never require a live model.

Deterministic behavior includes:

- canonical hashes;
- sorted diagnostics;
- stable operation ordering;
- stable dependency traversal;
- content-addressed snapshots;
- stable sample selection for a fixed source snapshot and session salt;
- candidate reconstruction from typed operations;
- verification reports whose semantic hash excludes wall-clock metadata;
- idempotent recovery.

Interview wording and model reasoning are not expected to be reproducible. Their accepted structured outputs and evidence references are.

## Test strategy

### Workspace and package tests

- Root `uv sync`, root selayer-only installation, and standalone `selayer-discovery` installation all resolve.
- Core selayer has no reverse dependency on discovery.
- The Agent Skill has one canonical packaged source.
- `SourceScanSession` exposes typed batches and consistency metadata without exposing raw connector handles.
- File and Delta adapters return reopenable tokens; transaction-only and live adapters report their weaker modes honestly.
- Scan sessions close transactions and connections on success, timeout, cancellation, and iteration failure.
- `recheck_snapshot()` detects changed reopenable sources.

### Session tests

- Valid and invalid state transitions.
- Materialized state rebuild from events.
- Concurrent mutation rejection.
- Charter scope cannot expand without an explicit charter revision that stales dependents.

### Fingerprint and invalidation tests

- Changed catalog, document, provider revision, source snapshot, sample policy, answer, claim, conflict resolution, proposal, or query case stales exact transitive dependents.
- Presentation-only timestamps do not alter semantic hashes.
- Manually changing status cannot revive stale artifacts.

### Evidence tests

- Observed, asserted, and inferred claims remain distinct.
- Inferred-only operations never become ready.
- Unresolved conflicts block only affected groups.
- Semantic conflict resolution requires the charter's named approver.
- Deterministic failures require new passing evidence and cannot be attested away.
- Changing the named approver stales prior conflict resolutions.
- Selectors resolve against the recorded revision.
- Approved exports contain hashes and selectors but no raw bodies.

### Profile and privacy tests

- Aggregate profiles use full scans and exact counts.
- Reopenable snapshot tokens reacquire the exact revision for profile, verification, and apply.
- Transaction snapshots are internally consistent but cannot authorize data-dependent executable changes after the scan closes.
- Live and transaction-only sources block executable source, grain, relationship, type, and expression changes.
- Timeout, cancellation, cost refusal, and partial failure produce `unavailable` without promotable partial claims.
- Resume accepts only content-addressed batches from the same reopenable snapshot.
- Missing valid grain blocks samples.
- Default policy omits every value.
- Policy activation requires the charter's named approver and binds the exact fingerprint.
- Changing the named approver invalidates activation and all derived artifacts.
- Canary secrets, credentials, private keys, email addresses, names, serials, and free text cannot escape under omit, redact, hash, or bucket policies.
- Row, field, value, and byte caps are enforced before context export.
- A changed policy invalidates every derived artifact.

### Provider tests

- Filesystem OKF search and get return namespaced immutable revisions.
- Item, document, and byte caps are enforced.
- Provider content cannot create commands or state transitions.
- Provider failures create unavailable evidence, not empty success.
- Multiple providers cannot collide resource IDs.

### Interview tests

- Only one open question exists.
- Every question maps to a gate and evidence reason.
- Corrections preserve prior answers and stale dependents.
- `not_applicable` requires a reason.
- Blocked gates identify affected groups.

### Proposal tests

- Add, edit, and deprecate operations reconstruct expected candidates.
- Deletes, renames, unknown keys, target escapes, and arbitrary patches are rejected.
- Derived impact flags match normalized before and after state.
- Dependency cycles are rejected.
- Knowledge operations cannot overwrite generated OKF fields or sections.

### Deprecation tests

- Existing catalogs default to active.
- Valid same-kind replacement chains pass.
- Missing, cross-kind, self, and cyclic replacements fail.
- Deprecated IDs still plan and execute.
- Catalog diagnostics, compatibility reports, and generated OKF surface migration metadata.

### Verification tests

- Every impact flag triggers its mandatory checks.
- Failed, skipped, and unavailable mandatory checks block readiness.
- Planner rejection cases complete as expected observations when the code matches.
- Result reports contain digests and assertions, not unrestricted rows.

### Approval tests

- Blocked, stale, or incomplete groups cannot be attested.
- Group decisions require the charter's normalized approver identity.
- Every bound fingerprint is recorded.
- Any fingerprint or approver change invalidates approval.
- Accepted, rejected, and deferred groups remain independent.
- Apply preparation requires an explicit dependency-closed group set with one common base.
- Cross-group semantic-target and curated-section overlap is rejected.
- The combined candidate, verification report, summary, and ordered group list are batch-attested.
- Applying one batch stales every remaining group and batch based on the old catalog.

### Apply and recovery tests

- Apply reconstructs the exact attested batch from typed operations, not preview patches.
- Target or source-snapshot drift is detected before replacement.
- Unrelated dirty files are untouched.
- No target changes before fsynced backups and a complete fsynced write-ahead journal exist.
- Journal entries contain expected old and new hashes and durable per-target progress.
- Injected failure before and after every replacement and fsync step restores original files.
- A target matching neither expected old nor new hash stops recovery without destroying backups.
- A crash without a success marker always rolls back; a valid success marker verifies and finalizes.
- Recovery is idempotent after no mutation, partial replacement, full replacement, rollback, and finalization.
- No Git command runs.
- Generated OKF output is not edited in place.

### Prompt-injection tests

Documents, wiki pages, and interview answers containing instructions to change scope, reveal secrets, invoke tools, approve proposals, alter policy, or run apply remain inert evidence text.

### Shopfloor acceptance

A deterministic shopfloor scenario will:

1. start from a catalog with a known semantic defect and a bounded business question;
2. ingest the reviewed shopfloor text corpus and filesystem OKF bundle;
3. run exact aggregate profiles;
4. activate a redacted sample policy;
5. replay structured interview answers and one correction;
6. identify a conflict that blocks only one dependency group;
7. propose an independent conformed-identity correction and curated overlays;
8. run static, physical, compatibility, acceptance-query, and OKF checks;
9. attest and apply the accepted group;
10. show that the corrected catalog and knowledge sources pass verification;
11. show that raw session data and samples are absent from Git-visible outputs.

No acceptance test calls a live model.

## Delivery stages

### Stage 0: workspace and deterministic core seams

- Convert the repository to a uv workspace without changing root package installation behavior.
- Add `status` and `replaced_by` to core catalog objects.
- Add validation, compatibility notices, and OKF generation behavior.
- Add the bounded public `SourceScanSession` adapter seam with consistency mode, snapshot token, typed batch iteration, cancellation, cleanup, and snapshot recheck behavior.

### Stage 1: deterministic session and evidence foundation

- Create the companion package, event store, canonical hashing, artifact schemas, diagnostics, and workspace policy.
- Add normalized text intake and approved-summary export.

### Stage 2: profiling and external knowledge

- Add exact aggregate profiles, conservative classification, sample policy, activation, redacted deterministic samples, and model-context export.
- Add the provider protocol and filesystem OKF provider.

### Stage 3: interview and evidence reasoning records

- Add gates, questions, answers, corrections, claims, conflicts, dependencies, and stale propagation.
- Add the Agent Skill workflow for charter, intake, policy approval, and interviews.

### Stage 4: typed proposals and verification

- Add catalog and knowledge operation schemas, candidate reconstruction, impact derivation, acceptance cases, and verification readiness.
- Add dependency-group review views.

### Stage 5: attestation, export, apply, and recovery

- Add named group decisions, dependency-closed batch preparation, batch attestation, and approved-summary export.
- Add project locking, fsynced write-ahead recovery journals, hash-guarded rollback, idempotent recovery, and explicit apply.
- Complete the Agent Skill approval and apply flow.

### Stage 6: shopfloor teaching workflow

- Add a replayable discovery scenario to the hardened shopfloor example.
- Document external wiki configuration, privacy policy, interview correction, blocked groups, approval, apply, and Git review.

## Documentation

Documentation must explain:

- the authority boundary between core catalog, OKF, discovery agent, deterministic companion, and named approver;
- how to install only selayer or install the discovery workspace package;
- version 1 document and provider limits;
- how aggregate profiling and redacted samples differ from physical audit;
- how to author and activate sample policy;
- how interviews, corrections, claims, and conflicts work;
- why approval is not authentication;
- how typed operations, verification, apply, rollback, and recovery work;
- which artifacts remain ignored and which approved summaries enter Git;
- how a future standalone wiki-building extension can plug in without changing core authority.

## Relationship to other designs

This design depends on:

- `docs/superpowers/specs/2026-07-31-selayer-verification-design.md` for deterministic catalog, physical, compatibility, runtime-profile, and OKF checks;
- `docs/superpowers/specs/2026-07-31-shopfloor-example-hardening-design.md` for the teaching fixture, reviewed business corpus, curated overlays, and acceptance values;
- `docs/superpowers/specs/2026-07-28-okf-retrieval-credibility-design.md` for bounded attribution and credibility signals without invented scoring;
- `docs/superpowers/specs/2026-07-27-okf-context-provider-design.md` for bounded OKF context behavior.

The work must follow the verification implementation before it relies on those public interfaces. It may implement workspace conversion and core deprecation independently if root package behavior remains unchanged.

A later design will cover the optional standalone wiki-building extension for richer document and media ingestion. That extension may produce OKF v0.2+ resources but remains outside core selayer and outside version 1 discovery apply authority.
