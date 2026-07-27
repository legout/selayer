# OKF Context Provider Design

## Purpose

Add Open Knowledge Format (OKF) support to selayer as an optional advisory knowledge layer for analytical agents. OKF enriches validated semantic objects with explanations, examples, caveats, provenance, and linked domain knowledge without becoming an executable source of query semantics.

Agent consumption is the primary use case. Human-readable documentation is a useful consequence, not the main design driver.

This design targets OKF v0.2 as specified by the [Open Knowledge Format specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md).

## Goals

1. Generate an OKF bundle from a validated `SemanticLayer`.
2. Bind OKF concepts to stable typed selayer identifiers such as `metric.gross_margin`.
3. Preserve curated knowledge while synchronizing catalog-derived content.
4. Load and validate OKF bundles without changing query behavior.
5. Retrieve bounded, attributed context suitable for analytical agents.
6. Represent provenance, verification, freshness, and lifecycle explicitly.
7. Support links from columns and dimensions to deeper domain knowledge, reference documents, and approved decoders.
8. Keep selayer independent of agent frameworks, wikis, vector databases, and multi-provider orchestration.

## Non-goals

- Defining executable facts, dimensions, metrics, joins, or expressions in OKF.
- Allowing OKF prose to modify query plans.
- Implementing a multi-provider context broker in selayer.
- Adding embeddings or a vector database dependency.
- Integrating directly with Obsidian, an LLM Wiki, or a specific RAG system.
- Implementing an interview user interface or agent orchestration runtime.
- Automatically promoting inferred knowledge to verified knowledge.
- Exporting raw data samples by default.
- Creating one OKF document per distinct column value.

## Authority model

The validated selayer catalog remains the sole authority for executable analytical semantics:

- source grains;
- relationships and join cardinality;
- facts and expressions;
- measures and aggregations;
- metrics and formulas;
- dimensions available for grouping and filtering.

OKF is advisory. It may help an agent discover, interpret, select, and explain semantic objects, but the planner must never read OKF to determine whether a query is valid.

```text
Semantic catalog ── generates/references ──▶ OKF context
Query planner ─────────────────────────────▶ Semantic catalog only
Agent ── retrieves ──▶ OKF + catalog ── requests query ──▶ Query planner
```

If an OKF statement conflicts with the catalog, selayer uses the catalog for execution and exposes the conflict for review.

## Stable semantic references

Catalog mapping keys are stable local identifiers. Their canonical typed forms are:

```text
source.order_items
dimension.product_category
fact.item_revenue
measure.total_item_revenue
metric.gross_margin
relationship.product_order_items
```

Catalog-backed OKF concepts carry the producer extension `selayer_id`:

```yaml
---
type: Selayer Metric
title: Gross margin
description: Gross profit as a proportion of item revenue.
selayer_id: metric.gross_margin
tags: [finance, profitability]
generated:
  by: process:selayer-okf
  at: 2026-07-27T14:00:00Z
status: stable
---
```

Labels, descriptions, and filenames may change without changing `selayer_id`. Concepts that describe broader business knowledge need not carry a `selayer_id`.

## Bundle structure

A generated bundle follows semantic kinds while allowing additional domain concepts, references, computations, and playbooks:

```text
knowledge/
├── index.md
├── sources/
│   └── order_items.md
├── dimensions/
│   └── product_category.md
├── facts/
│   └── item_revenue.md
├── measures/
│   └── total_item_revenue.md
├── metrics/
│   └── gross_margin.md
├── relationships/
│   └── product_order_items.md
├── concepts/
│   └── recognized_revenue.md
├── computations/
│   └── mlfb_decoder.md
├── playbooks/
│   └── investigate_margin_drop.md
├── references/
│   └── mlfb_coding_guide.md
└── log.md
```

`index.md` supports progressive disclosure. `log.md` records bundle updates. Links use standard OKF markdown links.

## Document ownership and synchronization

Catalog-backed concepts distinguish catalog-derived and curated sections:

```markdown
# Catalog Definition

Generated from the validated semantic catalog.

# Usage Guidance

Agent- or human-authored guidance.

# Examples

Representative questions and interpretations.

# Caveats

Known limitations and common mistakes.

# Related Concepts

Links to relevant OKF concepts and external knowledge.
```

Synchronization owns only the catalog-derived section and the corresponding generation metadata. It preserves curated sections and unknown frontmatter extension fields.

If synchronization cannot prove that it can update a generated region without losing curated content, it reports a conflict and leaves the file unchanged. A meaningful content change invalidates verification events that applied to the previous content.

Identical catalog input and preserved curated content must produce deterministic bundle output.

## Column-linked domain knowledge

A field may encode domain knowledge beyond its physical type. For example, an MLFB product number may encode product type, color, and features.

The catalog-backed dimension links to an identifier scheme, source material, and an approved decoder:

```text
dimension.mlfb
  ├── describes ──▶ MLFB identifier scheme
  ├── sourced from ──▶ MLFB coding manual
  ├── decoded by ──▶ approved MLFB decoder
  └── yields ──▶ product type, color, features
```

Example concept:

```markdown
---
type: Selayer Dimension
title: MLFB number
selayer_id: dimension.mlfb
tags: [product, identifier]
sources:
  - id: mlfb-guide
    resource: ../references/mlfb_coding_guide.md
    title: MLFB coding guide
verified:
  by: human:product-data-owner
  at: 2026-07-27T10:00:00Z
---

# Meaning

The MLFB is a structured product identifier. Portions of the identifier
encode the product family, variant, color, and optional features.

# Interpretation

See the [MLFB identifier scheme](../concepts/mlfb_scheme.md).

# Decoding

Use the [approved MLFB decoder](../computations/mlfb_decoder.md).
Do not infer unknown positions from similar product numbers.
```

### Interpretation versus queryability

OKF may teach an agent how to explain an MLFB, locate its manual, or select an approved decoder. This does not make decoded attributes queryable.

Questions such as “show revenue for blue products by product type” require `product_color` and `product_type` to exist as real semantic dimensions. A typical implementation materializes or exposes a lookup relation:

```text
product_attributes
├── mlfb
├── product_type
├── color
└── feature_set
```

The catalog then models the attributes and a grain-preserving relationship keyed by MLFB. OKF explains the mapping, provenance, examples, and caveats.

Bundles should describe the identifier scheme and decoder rather than create one concept per MLFB value. Sample-based inferences remain drafts until supported by an authoritative source or a verified test set.

## Authorship model

OKF is agent-maintained with human governance.

### Deterministic selayer generation

Selayer generates:

- semantic identifiers;
- catalog definitions;
- source schemas and grains;
- relationships;
- types and expressions;
- deterministic indexes.

### Agent enrichment

Agents may propose:

- descriptions and summaries;
- synonyms and disambiguation guidance;
- representative questions and examples;
- caveats and unsupported use cases;
- relationships to domain concepts;
- content extracted from manuals;
- candidate rules inferred from approved data profiles;
- interview-derived context.

Raw data inspection is opt-in. Profiling should prefer summaries such as cardinality, null rates, ranges, distributions, and redacted representative values.

### Human governance

Humans verify consequential semantics:

- business definitions and exclusions;
- encoded identifier rules;
- financial and operational metrics;
- privacy, legal, and access guidance;
- exceptions not inferable from samples;
- ownership and freshness commitments.

Humans do not need to hand-author every Markdown file.

### Lifecycle

```text
Catalog generation
      ↓
Agent enrichment from sources, profiles, and interviews
      ↓
Structural validation
      ↓
Machine verification where deterministic checks exist
      ↓
Human review for consequential semantics
      ↓
Continuous drift and freshness checks
```

Standard OKF v0.2 fields express generation, verification, status, sources, and staleness. Agent-generated content is normal but must expose its trust state. Sample-derived rules remain `draft` until verified.

## Native selayer interface

The optional `selayer.okf` module provides a small interface:

```python
from selayer.okf import OkfBundle

bundle = OkfBundle.from_layer(layer)
bundle.write("knowledge")

bundle = OkfBundle.load("knowledge", layer=layer)
context = bundle.context_for(
    ["metric.gross_margin", "dimension.product_category"],
    include_linked=True,
    max_chars=12_000,
)
```

`context_for()` returns structured context with content, semantic references, provenance, trust, lifecycle, freshness, sources, and diagnostics. It never returns alternative executable definitions and never modifies a query plan.

Retrieval is deterministic and uses indexes, frontmatter, identifiers, and links. Embedding search is not required.

## Multi-provider architecture

Selayer is one context domain beside broader knowledge systems such as an LLM Wiki. The systems remain separate and linked rather than merged.

### Knowledge ownership

| Domain | Owns |
| --- | --- |
| Selayer YAML | Executable grain, joins, expressions, metrics, and dimensions |
| Selayer OKF | Usage guidance tied to semantic identifiers |
| LLM Wiki | Broader domain knowledge, manuals, decisions, and organizational knowledge |
| Semantic or vector index | Retrieval acceleration only; not a source of truth |

A broad wiki page may be the canonical home of the MLFB identifier system. The OKF concept for `dimension.mlfb` links to it and records only selayer-specific implications. Knowledge is linked instead of copied.

### Context-provider seam

Selayer owns native context production through `OkfBundle.context_for()`. A separate orchestration package owns the generic provider protocol, provider adapters, and context broker:

```text
selayer
└── OkfBundle.context_for(...)

orchestration package
├── ContextProvider protocol
├── SelayerContextProvider adapter
├── WikiContextProvider adapter
├── RagContextProvider adapter
└── ContextBroker
```

This keeps selayer independent of Obsidian, vector databases, and agent frameworks. The orchestration package may be part of the broader SemaLoom platform, but it is not implemented in this repository.

### Canonical terminology

- **Knowledge source:** Original authority, such as a catalog, manual, or wiki page.
- **Retrieval index:** Search implementation such as BM25 or embeddings; not knowledge.
- **Context provider:** Retrieves from one coherent knowledge domain.
- **Context item:** One attributed piece of retrieved knowledge.
- **Context broker:** Selects providers and assembles context.
- **Context bundle:** Bounded, structured context supplied to an agent.

### Normalized provider output

The orchestration package should normalize provider output into attributed items containing:

```text
id
kind
content
provider
semantic references
authority domain
trust state
freshness
sources and citations
```

Requests carry intent, optional semantic identifiers, scope, and a token or character budget. Expected intents include `plan_query`, `explain_result`, `discover_data`, and `answer_domain_question`.

### Intent-aware retrieval

- `plan_query`: require the selayer catalog and linked OKF; consult the wiki only for unresolved terminology.
- `explain_result`: use catalog definitions, OKF guidance, and then relevant wiki knowledge.
- `discover_data`: start from selayer indexes and summaries, then expand selected concepts.
- `answer_domain_question`: use the wiki first and selayer only when analytical objects are relevant.

Providers use progressive disclosure: index, summary, relevant section, full concept, then linked concepts. Retrieval stops when the context budget is met.

### Authority and ranking

Context assembly ranks in this order:

1. eligibility for the request;
2. authority for the claim type;
3. verification and trust;
4. freshness;
5. relevance;
6. information diversity.

Embedding similarity is a relevance signal and cannot outrank authority.

Authority is claim-specific:

- executable formula or join: selayer catalog;
- guidance for using a semantic object: verified selayer OKF;
- broad domain explanation: wiki or authoritative manual;
- search score: retrieval signal only.

### Conflicts

The context broker must not blend contradictory claims silently. It retains each attributed claim, identifies the conflict, states which source governs the current task when an authority rule applies, and asks for review when no authoritative winner exists.

A contradictory wiki formula cannot override an executable metric definition from the selayer catalog. The contradiction remains visible for correction.

### Failure behavior

- An unavailable optional provider produces a partial bundle with a warning.
- An unavailable required provider fails the context request explicitly.
- Missing semantic references and broken links produce deterministic diagnostics.
- Stale or unverified knowledge remains readable but visibly marked.
- Budget pressure removes lower-authority optional context before mandatory authoritative items.
- Duplicate content is collapsed while preserving all source provenance.
- An unavailable semantic index falls back to provider-native indexes and metadata.

Multi-provider assembly and these cross-provider policies are requirements for the separate orchestration package, not selayer implementation work.

## Validation

Bundle validation collects deterministic issues and reports them together in stable order. It checks:

- OKF frontmatter and required `type` fields;
- reserved `index.md` and `log.md` structure;
- duplicate `selayer_id` bindings;
- unknown or malformed semantic identifiers;
- kind mismatches between `type` and `selayer_id`;
- broken internal links as warnings;
- malformed trust, lifecycle, provenance, and attested-computation fields when present;
- synchronization conflicts;
- retrieval budget violations.

Unknown OKF types and extension fields remain valid and survive round-tripping, as required by OKF extensibility.

## Privacy and safety

- Catalog export does not include data values by default.
- Data profiling requires explicit activation and a caller-provided access mechanism.
- Profiles prefer aggregate statistics and redacted examples.
- Sensitive-value policies are enforced before content reaches an agent.
- Agent-inferred rules are marked as drafts and cannot impersonate human verification.
- Regeneration never silently overwrites curated content.

## Testing strategy

### Unit tests

Cover parsing, serialization, validation, identifier resolution, synchronization, trust derivation, link traversal, deterministic ordering, and budget enforcement.

### Golden bundle tests

Compare generated Markdown and indexes with reviewed fixtures to guarantee deterministic output.

### Round-trip tests

Verify that loading and writing preserves unknown frontmatter fields, curated sections, links, and provenance.

### Integration tests

Exercise catalog generation, bundle loading, validation, and `context_for()` retrieval together.

### Safety tests

Cover stale content, broken links, invalid and duplicate identifiers, overwrite conflicts, malformed trust metadata, and opt-in sample behavior.

### Scenario tests

1. A metric retrieves its definition, guidance, caveats, and linked dimensions.
2. `dimension.mlfb` retrieves its identifier scheme, manual, and decoder guidance.
3. MLFB-derived attributes remain non-queryable until modeled as catalog dimensions.
4. Regeneration updates catalog-derived content without losing curated guidance.
5. A stale or unverified concept returns visible diagnostics.
6. Identical input produces identical bundle structure and indexes.

The separate orchestration package requires conformance tests proving that contradictory wiki or RAG content cannot override the executable selayer catalog.

## Delivery boundary

The selayer OKF feature is complete when it can generate, synchronize, load, validate, and retrieve bounded advisory context from a conformant bundle while preserving catalog authority and curated content.

The following work belongs to a separate design and implementation plan:

- the generic `ContextProvider` protocol;
- the Selayer, Wiki, and RAG provider adapters;
- context brokering and intent-based routing;
- cross-provider conflict resolution;
- semantic-search integration;
- agent interview and enrichment workflows.
