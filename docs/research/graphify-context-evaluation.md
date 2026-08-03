# Graphify as context for selayer data sources

## Recommendation

Use Graphify as an optional developer exploration tool. Do not make it a selayer dependency, do not treat its graph as catalog authority, and do not feed its edges into query planning.

For agent context about a declared data source, the semantic catalog plus OKF already provide the stronger contract. They use stable semantic IDs, validated schemas and grains, bounded retrieval, citations, trust, freshness, and a clear authority order. Graphify adds something different: a map across implementation code, tests, design notes, and external documents.

A small experiment is worthwhile, but it should test codebase navigation first. A production context-provider integration is premature.

## The important distinction

"Context for a data source" can mean three different things.

1. **Executable source semantics.** Connector, declared schema, grain, relationships, facts, measures, and metrics. Selayer's catalog owns this.
2. **Advisory domain knowledge.** Usage guidance, caveats, examples, provenance, and linked concepts. OKF owns this.
3. **Implementation and corpus relationships.** Which adapter prepares a source, which tests cover reload behavior, which design note explains a constraint, and which documents mention the same concept. Graphify can help here.

Graphify is useful for the third category. It is weaker for the first two.

## What Graphify provides

Graphify builds a persistent NetworkX knowledge graph from code, documents, PDFs, images, and video. Its default outputs are `graph.json`, `GRAPH_REPORT.md`, and an interactive HTML graph. Code extraction is deterministic and AST-based. Documents and media use semantic extraction through Gemini when configured or through the host agent. Edges carry `EXTRACTED`, `INFERRED`, or `AMBIGUOUS` confidence labels. Incremental updates use file-level caches and a manifest.

The current default branch also exposes the graph through an MCP server. Its query implementation is more capable than a plain substring search, but it is still lexical. It combines exact, prefix, substring, source-file, IDF, and coverage scores, then walks the graph with BFS or DFS. It does not use embeddings or BM25 for `query_graph`. Returned edges are not ranked by authority, trust, freshness, or confidence. The token budget is an approximate character budget, and node lines are emitted before edge lines.

Primary sources:

- [Graphify v8 README](https://github.com/Graphify-Labs/graphify/blob/v8/README.md)
- [Graphify v8 skill workflow](https://github.com/Graphify-Labs/graphify/blob/v8/graphify/skill.md)
- [Graphify v8 architecture](https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md)
- [Graphify v8 MCP query implementation](https://github.com/Graphify-Labs/graphify/blob/v8/graphify/serve.py)
- [Graphify v8 package metadata](https://github.com/Graphify-Labs/graphify/blob/v8/pyproject.toml)

## What selayer already provides

### Semantic catalog

`src/selayer/catalog.py` validates source declarations before constructing an immutable `SemanticLayer`.

- `_validate_data_sources()` delegates connector and schema validation, parses declarations, and verifies that grain columns exist.
- `_validate_relationships()` and `_validate_fact_expression_reachability()` enforce safe semantic paths.
- `_validate_metric_grains()` rejects invalid grain combinations.
- `_build_layer()` creates typed sources, dimensions, facts, measures, metrics, and relationships.

`SemanticLayer.semantic_objects()` in `src/selayer/model.py` exposes every object under a stable typed ID such as `source.orders` or `metric.gross_margin`.

### Runtime source state

`src/selayer/sources/registry.py` owns prepared resources, observed schema checks, atomic reloads, generations, snapshots, and safe status output.

- `SourceRegistry.reload_source()` prepares a candidate, compares observed and declared schemas, publishes atomically, and keeps the previous registration on failure.
- `SourceRegistry.status()` reports sanitized source state.
- The private `SourceAdapter` protocol in `src/selayer/sources/base.py` defines preparation, schema inspection, registration, query binding, and cleanup.

Graphify cannot inspect this live state. A graph built from repository files knows the implementation, not the current registered schema, generation, snapshot, health, or runtime profile.

### OKF context

`OkfBundle.context_for()` in `src/selayer/okf/bundle.py` retrieves required concepts by exact semantic ID, includes linked concepts to a bounded depth, enforces a character budget, and returns diagnostics when linked material is omitted. Each `ContextItem` carries provider, semantic references, trust, freshness, sources, and optional attested computation.

`catalog_definition()` and `concepts_from_layer()` in `src/selayer/okf/generation.py` generate deterministic catalog summaries without reading source data. For a data source, the generated definition includes connector kind, schema fingerprint, grain, and a bounded field summary.

The intended boundary is explicit in `docs/superpowers/specs/2026-07-27-okf-context-provider-design.md`:

- the catalog is authoritative for executable semantics;
- OKF is advisory;
- retrieval indexes are not knowledge;
- a future orchestration package, not selayer, may combine `SelayerContextProvider`, wiki, and RAG providers;
- the broker ranks by eligibility, authority, trust, freshness, relevance, and diversity.

That design already has the right place for a Graphify-like system. If Graphify is ever integrated, it should sit outside selayer as a supplemental retrieval provider. The broker must normalize its results and keep catalog authority intact.

## Evidence from the existing selayer graph

The checked-in local artifact at `graphify-out/GRAPH_REPORT.md` was built from commit `4ca48940`. It contains:

- 219 files and about 206,151 words;
- 3,637 nodes and 10,695 edges;
- 249 communities;
- 54% extracted and 46% inferred edges;
- inferred-edge confidence averaging 0.54;
- 925 isolated nodes.

The graph does find the structural centers of the source subsystem. `TableSchema`, `RuntimeProfileResolver`, `ArrowProviderResolver`, `ParsedSource`, `SourceHandle`, `SourceScanRequirement`, and `SemanticLayer` appear as highly connected nodes.

The same output also shows why it should not drive semantic decisions. `TableSchema` has 249 inferred `uses` edges, `ParsedSource` has 180, and `SourceRegistry` has 89. Many are broad type co-occurrences with confidence 0.5, not domain relationships. These edges help with exploration, but they are too noisy for query planning.

A live query during this research asked whether Graphify adds useful data-source context beyond the catalog and OKF. It chose `selayer`, `Context`, and another `Context` node as seeds, then returned 67 nodes dominated by verification and agent-discovery documents. That is a poor answer to the actual question. The current Graphify v8 scorer has improved since the installed local CLI, but its retrieval remains lexical and graph-topological rather than authority-aware.

There is also a version caveat. The installed CLI is `graphify 0.8.36`; the repository's current v8 package metadata reports `0.9.32`. Results from the existing graph should not be treated as a benchmark of the latest release without rebuilding it.

## Where Graphify adds value

- Trace implementation relationships across adapters, registry code, tests, examples, and design notes.
- Find file-and-line evidence for onboarding and maintenance questions.
- Surface cross-file communities and bridge nodes that OKF does not model.
- Connect external manuals, architecture notes, or data dictionaries to implementation concepts when those materials are not represented in OKF.
- Support exploratory questions such as "which tests exercise Iceberg reload?" or "what connects runtime profiles to adapter preparation?"

## Where it is redundant or weaker

- It cannot replace catalog validation, grain checks, relationship safety, or metric validation.
- It cannot replace runtime schema inspection, source generation, snapshot, or health state.
- It duplicates catalog-derived definitions that OKF already generates deterministically.
- Its edge confidence is not the same as selayer's claim authority or OKF trust state.
- Its query path does not rank by authority, trust, freshness, or edge confidence.
- It has no exact equivalent of required-first OKF retrieval by stable semantic ID.
- Mixed code and document graphs can produce many low-confidence edges and isolated nodes.
- A semantic extraction run may send document content to a configured model or host agent. Source profiles, credentials, and raw samples must stay out of the corpus even though Graphify skips files it detects as sensitive.

## Proposed experiment

Use the existing graph first. Do not rebuild or add dependencies yet.

Ask five focused developer questions:

1. How does a Parquet source get prepared and registered?
2. What path leads from `reload_source()` to a schema mismatch error?
3. Which tests cover Iceberg query-scoped readers?
4. Where are runtime profile values prevented from leaking into diagnostics?
5. What calls `requirements_for_plan()` and how does its result reach an adapter?

For each question, compare Graphify's answer with normal symbol/LSP navigation.

Success means:

- at least four answers identify the correct implementation file and line range;
- no answer states an inferred relationship as fact without checking the cited source;
- the returned context includes the decisive relationship, not only nearby high-degree nodes;
- Graphify reduces navigation time or files opened for at least three questions;
- no credential, DSN, runtime profile value, or source sample enters the graph.

Stop if fewer than four answers are correct, more than one answer starts from irrelevant seed nodes, or relevant paths are dominated by confidence-0.5 inferred edges.

Only after that passes should we rebuild with the current release and repeat the same questions. Keep the experiment local. Do not add `graphifyy` to selayer's dependencies or CI, and do not commit Graphify output as product state.

## Bottom line

The semantic catalog and OKF are enough for authoritative data-source context. Graphify does not close a missing runtime capability there.

Graphify may still earn a place as a developer-facing map of code and documents. If selayer later gains the planned external context broker, Graphify could be evaluated as one supplemental provider for discovery and architecture questions. It should remain below the catalog and verified OKF in the authority order.
