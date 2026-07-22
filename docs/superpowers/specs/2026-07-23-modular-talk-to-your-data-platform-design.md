# Modular “Talk to Your Data” Platform Design

**Status:** Approved design
**Date:** 2026-07-23

## 1. Purpose

Evolve `selayer` from the current promising MVP into a modular, local-or-team business-intelligence platform with these properties:

- `selayer` is a standalone Python distribution for semantic contracts and knowledge bundles.
- `selayer-chat` is an optional agentic analysis distribution rather than part of the semantic core.
- The same capabilities are usable from the owned UI, standard chat UIs, Python, CLI, MCP, Claude Code, and OpenCode.
- Agents propose plans and changes; deterministic modules validate and execute them.
- The local edition is frictionless and does not require authentication or hosted Git.
- The trusted-team edition adds role enforcement, shared state, and private GitLab publication.

The product identity is **platform first**. No UI or agent harness is the system's primary interface.

## 2. Current-state observations

The repository currently has one tracked package module, `src/selayer/__init__.py`, containing the semantic model, serialization, Mermaid output, source loading, join discovery, SQL generation, and query execution in approximately 550 lines.

The uncommitted MVP adds `selayer_chat`, four thin UI applications, PydanticAI orchestration, and a second DuckDB execution path. The current `AnalyticalBackend` performs catalog loading, source registration, schema projection, agent construction, SQL policy checks, and execution. The existing timeout measures elapsed time only after DuckDB returns and therefore cannot cancel an expensive query.

The refactor must first preserve and characterize useful MVP behavior. It must not simply move current coupling into more packages.

## 3. Accepted decisions

1. Use a capability-centered uv workspace with a small number of deep packages.
2. Raise the workspace to Python 3.14.
3. Keep selayer YAML authoritative for executable semantic meaning.
4. Use OKF as an enriching knowledge representation and portable exchange format.
5. Support local and trusted single-tenant server profiles over one application core.
6. Define two team roles:
   - `user`: may read, query, visualize, export, and nominate reusable examples.
   - `maintainer`: may additionally change sources, semantics, knowledge, and shared examples.
7. All authoritative mutations follow propose → diff → explicit approval.
8. Default LLM disclosure is metadata only.
9. Use semantic plans first and constrained agent-generated SQL only as a fallback.
10. Use private GitLab for the team catalog, supporting both direct-push and merge-request publication modes.
11. Local catalogs need no Git, but may optionally use local Git, GitHub, or GitLab.
12. Successful question/query pairs become shared memory only after user nomination and maintainer approval.
13. Reuse `fsspeckit` where useful for filesystems and storage configuration.
14. Do not depend on `flowerpower-io`; only borrow selected design patterns.
15. Use trusted reverse-proxy identity headers for the first team-server authentication adapter.

## 4. Workspace and dependency design

```text
selayer/
├── pyproject.toml                 # uv workspace, Python 3.14
├── uv.lock
├── packages/
│   ├── selayer/                   # semantic + OKF domain
│   ├── selayer-runtime/           # connectors, DuckDB, SQL policy
│   └── selayer-chat/              # optional agents and orchestration
├── services/
│   └── selayer-server/            # HTTP, OpenAI-compatible, MCP, team composition
├── apps/
│   └── selayer-web/               # Stario workbench
├── skills/                        # Claude Code/OpenCode Agent Skills
└── deploy/
    ├── local/                     # Docker Compose, no auth/Git required
    └── team/                      # trusted proxy, PostgreSQL, shared storage
```

### 4.1 `selayer`

`selayer` is independently installable and has no LLM, DuckDB, HTTP, MCP, or UI dependency.

Responsibilities:

- Typed semantic domain model.
- Stable IDs for sources, tables, fields, facts, dimensions, measures, metrics, hierarchies, and relationships.
- YAML parsing, validation, serialization, migration, and unknown-field preservation where appropriate.
- OKF v0.1 bundle parsing, generation, linting, indexing, linking, and extension preservation.
- YAML-to-OKF projection.
- Catalog revisions, proposals, semantic diffs, and validation reports.

### 4.2 `selayer-runtime`

Responsibilities:

- Source connector interface and connector capability descriptions.
- Bounded source profiling.
- DuckDB query sessions and approved source mounting.
- Typed analysis plans and deterministic semantic compilation.
- SQL parsing, binding, policy enforcement, EXPLAIN, cancellation, and resource limits.
- Query and chart artifact creation.

It depends on `selayer`. Lake connectors may depend on `fsspeckit` and format-specific libraries through extras.

### 4.3 `selayer-chat`

Responsibilities:

- Main analyst orchestration for clients that do not provide their own agent.
- Typed analysis planner.
- Constrained SQL fallback planner.
- BI/chart planner.
- Source onboarding proposal agent.
- Context and approved-memory retrieval.
- Streaming domain events.
- PydanticAI model/provider adapters.

It depends on `selayer-runtime` and `selayer`. Deterministic policy is never implemented in prompts.

### 4.4 Composition roots

`selayer-server` selects identity, operational-store, catalog-repository, artifact-store, secret-provider, model-provider, and transport adapters for the team deployment.

`selayer-web` is a Stario client of the native server interface. Stario does not host the platform's authentication, OpenAI-compatible API, or MCP server. This matters because Stario is not ASGI and intentionally does not supply the broader server infrastructure ecosystem.

Modules are promoted to separately versioned packages only when independent installation or release cadence becomes necessary. There will not initially be one package per agent, connector, or protocol.

## 5. Semantic YAML and OKF

Google's Open Knowledge Format is currently version 0.1 Draft. It is a permissive directory of Markdown concepts with YAML frontmatter. It intentionally does not prescribe domain taxonomies, execution semantics, storage, or query infrastructure.

Therefore YAML and OKF are related but not interchangeable:

- **selayer YAML** defines executable tables, fields, joins, metrics, dimensions, filters, and constraints.
- **OKF** defines narrative business meaning, provenance, examples, playbooks, citations, and links.

The YAML model is authoritative when both formats describe the same asset.

Generated OKF concepts use stable selayer IDs and may include producer-defined frontmatter such as the selayer ID, semantic kind, source revision, and generation fingerprint. Consumers must tolerate unknown OKF fields, and round-tripping must preserve them.

Generated content must not silently erase curated prose. The implementation will either preserve explicitly generated sections or keep generated concepts separate from curated concepts and link them. The exact file convention will be fixed in the implementation plan and covered by golden round-trip tests.

Useful approved question/query examples may become OKF concepts of a producer-defined type such as `Query Example`, linked to the metrics, dimensions, tables, and business concepts they exercise.

References:

- [Open Knowledge Format v0.1 Draft](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
- [Google Cloud OKF announcement](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
- [Andrej Karpathy's LLM Wiki proposal](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

## 6. Query architecture

### 6.1 Typed plan first

The planner produces an `AnalysisPlan` containing stable semantic IDs and typed fields such as:

- metrics
- dimensions
- filters
- time grain
- ordering
- limit
- desired result shape
- visualization intent

`selayer-runtime` resolves IDs, validates relationships, chooses declared join paths, compiles metric expressions, binds parameters, and emits SQL plus provenance.

### 6.2 SQL fallback

When a request cannot be represented by the semantic plan model, policy may permit a constrained SQL planner. The fallback receives only approved catalog metadata and must cite the relations and columns it uses.

Fallback SQL is optional per deployment, role, source, and request. It may be disabled for semantic-only environments.

### 6.3 SQL policy and execution

Regex checks are insufficient. The runtime must:

- Parse an AST with a DuckDB-aware SQL parser such as SQLGlot.
- Accept one read-only `SELECT`/`WITH` statement.
- Reject DDL, DML, extension installation/loading, external file/network access, and unapproved functions.
- Bind every relation and field against the mounted catalog.
- Use parameters rather than string interpolation for filter values.
- Apply result-row, memory, wall-clock, and concurrency limits.
- Run `EXPLAIN` before execution where useful.
- Use actual DuckDB interruption or process/session isolation for cancellation.
- Use request- or session-scoped connections on the team server rather than one shared connection.
- Record plan, SQL, catalog revision, actor, timings, policy decision, and artifact hashes.

## 7. BI and chart architecture

The BI planner reasons about analytical framing and produces a typed, declarative `ChartSpec`. A deterministic renderer validates and renders the specification to interactive HTML and/or static PNG/SVG. The agent never emits arbitrary executable plotting code.

The Stario workbench renders interactive charts and tables. Generic chat clients receive the best artifact representation they support: Markdown, a bounded preview, static image, downloadable data, or a link to an HTML artifact.

## 8. Source connector design

The initial deep interface has two operations:

```python
probe(source_ref, disclosure_policy) -> SourceProfile
mount(query_session, source_definition) -> MountedSource
```

`SourceProfile` describes schema, partitions, snapshot/version identity, bounded statistics, diagnostics, and connector capabilities. `MountedSource` describes the exact read-only relations exposed to DuckDB.

Optional behavior is represented as capabilities rather than a growing method surface. Example capabilities include schema discovery, partition discovery, statistics, snapshot identity, sampling, DuckDB mounting, and remote pushdown.

Initial connector family:

- Local and remote CSV files/datasets.
- Local and remote JSON files/datasets.
- Local and remote Parquet files, globs, and partitioned datasets.
- Delta Lake tables through a dedicated adapter.
- Apache Iceberg tables through a catalog-aware adapter.
- Local, S3, GCS, and Azure-style storage through supported `fsspeckit` filesystem/storage configuration.

Delta and Iceberg support must publish an explicit capability matrix. The system will not claim universal support for every catalog, transport, authentication mode, or extension merely because a source is called Delta or Iceberg.

The query platform is read-only with respect to underlying business data. Source onboarding changes catalog definitions, not source data.

## 9. Source onboarding and mutation workflow

1. A maintainer supplies a `SourceRef` containing a URI, connector kind/format hint, and credential reference.
2. A deterministic connector probes metadata under the active disclosure policy.
3. The onboarding agent proposes:
   - source and field definitions
   - facts/dimensions/measures/metrics where evidence permits
   - relationship candidates with confidence and open questions
   - generated or linked OKF concepts
4. Deterministic validation checks syntax, stable IDs, references, name collisions, query compilation, safe mounting, OKF conformance, and accidental secrets.
5. The maintainer reviews a structured semantic diff and the exact file diff.
6. Approval is valid only against the proposal's base catalog content hash or commit SHA.
7. The selected repository adapter publishes and activates the validated revision.
8. Activation invalidates stale sessions and query examples according to semantic and source fingerprints.

Agents never directly mutate the active catalog.

## 10. Catalog repositories and GitLab

A proposal is a domain object based on a base content hash. Git is one repository adapter, not a requirement of the proposal model.

### 10.1 Local repository

The local edition uses a plain YAML + OKF directory and atomic file replacement. It requires no Git or hosted service. Optional adapters may add:

- local Git history
- GitHub publication
- GitLab publication

### 10.2 Team GitLab repository

The team edition uses a private GitLab repository. Credentials are stored in the deployment secret provider; catalog configuration contains only a credential reference.

Both publication modes operate on the same proposal model:

- **Selayer approval mode:** Stario approval creates a bot-authored commit and direct protected-branch push.
- **GitLab merge-request mode:** selayer pushes a proposal branch and creates an MR; merge activates the validated merged commit through webhook or polling.

Use protected branches, restricted deploy keys or project access tokens, pull-before-propose, base-SHA conflict checks, and auditable commits.

## 11. Memory model

Memory is separated by purpose and trust.

### 11.1 Operational memory

Stored in SQLite locally and PostgreSQL on the server:

- conversations and messages
- tool calls and progress events
- generated plans and SQL
- chart specifications and artifact references
- session catalog revision and temporary assumptions
- proposal and nomination workflow state

### 11.2 Curated knowledge memory

A user may nominate a successful question/query pair. A maintainer must approve it before shared retrieval. The reusable exemplar contains:

- original or normalized question
- typed analysis plan
- SQL dialect and validated SQL where useful
- expected result shape
- chart suggestion
- business interpretation and provenance
- semantic-catalog revision/hash
- source schema or snapshot fingerprint

Execution success alone is not proof of semantic correctness. Stale exemplars are excluded when their semantic or source fingerprints no longer match.

Approved exemplars may be represented as OKF concepts so they remain portable and linkable.

### 11.3 Audit history

Audit evidence is not agent memory. Append-only events record actors, commands, approvals/rejections, policy decisions, catalog diffs, revisions, timings, and artifact hashes.

Agents remain stateless. The platform retrieves and injects a bounded context; model-provider conversation state is not the system of record. Private chain-of-thought is neither requested nor stored.

## 12. Client and protocol design

### 12.1 Native HTTP

The native interface exposes catalog, conversation, query, proposal, approval, memory, artifact, and audit resources. Server-sent events carry progress, SQL, rows/artifacts, citations, errors, and completion.

### 12.2 OpenAI-compatible HTTP

The server implements the common denominator required by Open WebUI and LibreChat, including `/v1/models`, `/v1/chat/completions`, and streaming.

This provides reliable chat, SQL, citations, and artifact interoperability. It cannot guarantee a native schema editor, semantic diff, approval workflow, or interactive BI panel in every generic client.

### 12.3 MCP

MCP is available over stdio locally and Streamable HTTP remotely. A small tool surface includes operations equivalent to:

- `catalog_search`
- `plan_query`
- `execute_plan`
- `create_chart`
- `profile_source`
- `propose_catalog_change`
- `get_proposal_diff`
- `nominate_query_example`

Mutation tools return proposals; they do not bypass approval.

### 12.4 Agent Skills

Claude Code and OpenCode Skills describe workflows such as querying data, onboarding a source, editing knowledge, and nominating a query example. Skills invoke the CLI or MCP tools. They do not duplicate connector logic, validation, SQL policy, or catalog mutation rules.

When used from Claude Code or OpenCode, the harness may replace the built-in main analyst as orchestrator. The deterministic platform capabilities remain unchanged.

References:

- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- [Open WebUI OpenAI-compatible connections](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)
- [Open WebUI MCP](https://docs.openwebui.com/features/extensibility/mcp/)
- [LibreChat MCP](https://www.librechat.ai/docs/features/mcp)
- [Claude Code Agent Skills](https://code.claude.com/docs/en/agent-sdk/skills)
- [OpenCode Skills](https://opencode.ai/docs/skills/)

## 13. Deployment profiles

### 13.1 Local single-user edition

The local edition runs natively or with Docker Compose.

- No authentication or reverse proxy.
- One explicit local principal with maintainer capability.
- Plain mounted YAML + OKF catalog directory.
- No Git requirement; optional local Git/GitHub/GitLab integration.
- Propose → diff → local confirmation → atomic content-hash-based replacement.
- SQLite operational state.
- Local artifact directory.
- Environment, `.env`, or mounted secret files.
- API/runtime and Stario UI started by Compose; MCP stdio may run directly.

### 13.2 Trusted-team single-tenant edition

- Trusted reverse proxy supplies identity headers.
- Direct public access that could spoof identity headers is rejected.
- Explicit `Principal` is passed to commands; domain modules do not inspect global request state.
- `user` and `maintainer` authorization is enforced server-side.
- Private GitLab catalog.
- PostgreSQL operational store.
- Local or fsspec-backed artifact store.
- Deployment secret provider.
- Independent API/runtime and Stario processes behind the proxy.

Authentication, GitLab, PostgreSQL, and shared storage are selected at the team composition root. They must not appear as conditionals throughout domain modules.

## 14. LLM disclosure policy

Metadata only is the default:

- schemas and types
- semantic YAML
- OKF concepts
- aggregate statistics
- redacted or synthetic examples
- policy-approved provenance

Raw source rows, sampled values, and query result rows require an explicit policy grant. Context retrieval is bounded and revision-aware.

## 15. Error handling and observability

Use typed failures:

- planning uncertainty asks a clarification rather than guessing
- policy rejection explains the forbidden construct and safe alternatives
- connector and query errors return sanitized diagnostics and correlation IDs
- stale proposals return a catalog-conflict error and require regeneration
- provider failure leaves deterministic SDK/CLI/MCP functions available
- artifact failures do not retroactively hide successful query execution

Only bounded, safe repair attempts are automatic.

Trace one request across gateway, context retrieval, agent, compiler, SQL policy, DuckDB, and chart rendering. Redact secrets and respect retention settings.

## 16. Testing strategy

### 16.1 `selayer`

- YAML and OKF round trips.
- Unknown-key preservation.
- Stable IDs and deterministic projections.
- Broken links and permissive OKF consumption.
- Golden catalog revisions, semantic diffs, migrations, and conflict scenarios.

### 16.2 Runtime and connectors

- Shared connector contract suite.
- Local and object-storage fixtures for CSV, JSON, Parquet, Delta, and supported Iceberg configurations.
- Schema/snapshot fingerprint tests.
- SQL AST attack corpus.
- Relation/function allowlists and parameter binding.
- Real cancellation, memory, row, and concurrency limits.

### 16.3 Agents

- Fake-model tests for typed plans, chart specs, proposals, abstention, and bounded repair.
- Curated evaluation set for semantic correctness, provenance, and clarification behavior.
- Tests proving agents cannot bypass deterministic mutation or execution policy.

### 16.4 Protocols, roles, and UI

- OpenAI-compatible schema and streaming conformance.
- MCP schemas and permissions.
- Full `user`/`maintainer` command matrix.
- Trusted-proxy spoofing boundary tests.
- Stario browser end-to-end tests.
- Reference Open WebUI and LibreChat configuration smoke tests.
- Local Docker Compose acceptance test with no auth or Git.
- Team acceptance test with GitLab workflow adapters and PostgreSQL.

## 17. Delivery slices

This design should be implemented as vertical slices, not as a large rewrite.

1. **Characterize and separate:** add tests for valuable MVP behavior, create the uv workspace, and extract the three packages without adding new product behavior.
2. **Catalog foundation:** typed semantic IDs, YAML validation/migration, OKF bundle support, proposals, semantic diffs, local catalog repository.
3. **Trusted local query path:** typed `AnalysisPlan`, deterministic compiler, DuckDB sessions, SQL policy, cancellation, Parquet connector, CLI.
4. **Agent and protocol path:** built-in analyst, bounded context, MCP, native HTTP, OpenAI-compatible HTTP, and streaming artifacts.
5. **Local product:** Stario workbench and no-auth Docker Compose profile.
6. **Onboarding and memory:** metadata profiling, local-file proposal flow, nomination/approval of query examples.
7. **Team profile:** reverse-proxy identity, role matrix, PostgreSQL, GitLab direct-push and MR publication.
8. **Lake expansion:** remote storage plus CSV/JSON/Parquet datasets, Delta, and explicitly scoped Iceberg adapters.
9. **Compatibility hardening:** Open WebUI, LibreChat, Claude Code, and OpenCode reference integrations and end-to-end verification.

Each slice must leave a usable, tested path and preserve the deterministic-core rule.

## 18. Explicit feasibility limits

The following are possible and in scope:

- Independently installable semantic, runtime, and agent packages in one uv workspace.
- One owned BI workbench plus standard chat and harness integrations.
- YAML-authoritative semantics enriched by OKF.
- Local and trusted-team profiles over the same core.
- Agent-assisted source onboarding with safe approval.
- Curated, revision-aware query memory.
- Local and object-store lake data through capability-declared connectors.

The following claims are intentionally not made:

- Lossless YAML ↔ OKF conversion.
- Pixel-identical BI workflows in Open WebUI and LibreChat.
- Skills as a substitute for deterministic Python implementation.
- Safe arbitrary SQL based on regex checks.
- Automatic semantic correctness because a query executed.
- Universal Delta/Iceberg compatibility without a tested capability matrix.
- Hard multi-tenant SaaS isolation in the trusted-team MVP.
- Automatic agent mutation of active semantics, knowledge, or source registrations.
