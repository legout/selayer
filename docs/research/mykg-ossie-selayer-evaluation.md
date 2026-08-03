# myKG and Apache OSSIE for selayer

## Recommendation

Apache OSSIE is the stronger strategic fit. Selayer should track it and run an export-only compatibility experiment. Do not replace selayer's internal model and do not add the draft Python package as a core dependency yet.

myKG is useful for a different problem. It can turn manuals, data dictionaries, runbooks, and domain documents into a source-grounded knowledge graph. If selayer later participates in the planned external context broker, myKG could supply supplemental domain context through MCP. It should not run inside selayer or write executable catalog semantics.

The projects occupy different layers:

| Project | Best role around selayer | Authority |
| --- | --- | --- |
| selayer catalog | Executable analytical semantics and planning | Authoritative |
| selayer OKF | Curated guidance, provenance, trust, and freshness | Advisory, catalog-linked |
| Apache OSSIE | Exchange format for semantic models | Interchange only |
| myKG | Extracted domain knowledge from documents | Supplemental and unverified |
| Graphify | Developer navigation across code and documents | Supplemental and unverified |

## myKG

### What it is

[myKG](https://github.com/SenolIsci/mykg) is a document-to-knowledge-graph pipeline. Version `0.3.31` accepts Markdown, text, PDF, Office documents, HTML, and images. It can also crawl websites or fetch repositories. The package is MIT licensed, requires Python 3.11, and is currently maintained primarily by one contributor.

The extraction pipeline has several distinct stages:

1. An LLM induces an RDFS/OWL schema from document batches.
2. Deterministic merging collapses exact and normalized duplicates.
3. LLM passes harmonize and review the schema.
4. A deterministic validator checks Turtle syntax and a limited set of explicit domain, range, and parent declarations.
5. An LLM extracts typed nodes and relationships against the approved schema.
6. LLM passes normalize names and propose relationships for orphan nodes.
7. Deterministic assembly creates stable IDs, merges duplicates, records confidence, and exports the graph.

The pipeline is resumable and session-based. Append mode uses file hashes to reprocess changed documents. A locked base schema can be extended but not rewritten, and a human review gate can pause extraction until the induced schema is approved.

Primary sources:

- [myKG README](https://github.com/SenolIsci/mykg/blob/main/README.md)
- [myKG architecture](https://github.com/SenolIsci/mykg/blob/main/docs/architecture.md)
- [myKG package metadata](https://github.com/SenolIsci/mykg/blob/main/pyproject.toml)
- [myKG MCP server](https://github.com/SenolIsci/mykg/blob/main/src/mykg/mcp_server.py)
- [myKG query implementation](https://github.com/SenolIsci/mykg/blob/main/src/mykg/query.py)
- [myKG ID generation](https://github.com/SenolIsci/mykg/blob/main/src/mykg/ids.py)
- [myKG schema validator](https://github.com/SenolIsci/mykg/blob/main/src/mykg/schema_validator.py)

### What selayer can learn from it

The strongest ideas are operational, not model-level.

- **Locked schema plus discovery.** myKG separates an approved base ontology from LLM-proposed additions. This is a good model for agent-assisted semantic discovery: proposals may extend a reviewed vocabulary, but cannot silently rewrite it.
- **Human approval before extraction.** The schema gate makes uncertainty visible before thousands of instances depend on a bad ontology.
- **Evidence as a separate retrieval step.** `mykg_get_source` returns source chunks for a node or edge. This is better than treating graph proximity as sufficient evidence.
- **Resumable, inspectable runs.** Session manifests, per-file shards, schema history, merge logs, and failed-chunk records make an LLM-heavy pipeline auditable.
- **Attribute-level confidence.** Nodes, edges, and attributes retain confidence and source files. Selayer should not copy the scoring policy, but the granularity is useful for discovery artifacts.
- **Bounded schema growth.** New properties can trigger re-extraction of affected chunks instead of rebuilding the entire corpus.

These patterns fit a companion discovery or context system. They do not belong in selayer's query engine.

### What myKG cannot replace

myKG does not validate analytical semantics. It has no model for source connector contracts, observed schemas, source grain, safe relationship traversal, facts, measures, metric compatibility, or runtime source generations.

Its confidence values are extraction metadata, not calibrated probabilities and not an authority model. Assembly may choose the highest-confidence value, while conflicting values at confidence `1.0` are concatenated. That behavior is inappropriate for executable formulas or joins.

Its query path is lexical. It selects up to three nodes by exact, containment, or attribute matches, then traverses an undirected view of the graph. `mykg_query_graph` returns confidence but not source excerpts. An agent needs a second `mykg_get_source` call to inspect evidence.

Stable IDs normalize type and name into slugs. Distinct punctuation variants may collide, and the ID module does not resolve collisions. Selayer's typed semantic IDs are stronger.

### Sensible integration seam

Do not import myKG's Python package into selayer. Its core dependency set includes LLM clients, RDF tooling, NetworkX, MCP, document conversion, and orchestration that the query engine does not need.

If there is a real domain-document use case, run myKG as a separate process and adapt a small MCP subset in the future orchestration package:

- `mykg_search_nodes`
- `mykg_get_node`
- `mykg_get_neighbors`
- `mykg_get_source`

The adapter should normalize results into the context item shape already described in `docs/superpowers/specs/2026-07-27-okf-context-provider-design.md`: provider, semantic references, authority domain, trust, freshness, and citations.

myKG output may suggest new OKF concepts or links, but only as drafts. It must never modify the catalog or promote inferred knowledge to verified OKF automatically. Raw source data, credentials, DSNs, and runtime profiles must stay outside its corpus.

## Apache OSSIE

### What it is

[Apache OSSIE](https://github.com/apache/ossie), formerly Open Semantic Interchange, is a vendor-neutral YAML and JSON format for exchanging semantic models. It is an Apache Incubator project under Apache 2.0. The current core specification is draft `0.2.0.dev0`; the repository states that the schema may change before `0.2.0` is released.

The core model contains:

- semantic models;
- logical datasets with physical `source` strings;
- fields with scalar, dialect-specific expressions;
- primary and unique keys;
- fixed many-to-one relationships with simple or composite columns;
- model-level metrics with dialect-specific aggregate expressions;
- `ai_context` for instructions, synonyms, and examples;
- vendor `custom_extensions` containing JSON strings.

The repository includes a JSON Schema, Pydantic models, a Python validator, a TPC-DS example, and converters for several semantic-layer products. The validator checks JSON Schema conformance, local name uniqueness, relationship dataset references, and SQL syntax where `sqlglot` supports the dialect. It does not validate field references, key correctness, grain compatibility, or query safety.

Primary sources:

- [Apache OSSIE overview](https://github.com/apache/ossie/blob/main/README.md)
- [Core specification](https://github.com/apache/ossie/blob/main/core-spec/spec.md)
- [JSON Schema](https://github.com/apache/ossie/blob/main/core-spec/osi-schema.json)
- [Roadmap](https://github.com/apache/ossie/blob/main/ROADMAP.md)
- [Official TPC-DS example](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml)
- [Python models](https://github.com/apache/ossie/blob/main/python/src/ossie/models.py)
- [Validator](https://github.com/apache/ossie/blob/main/validation/validate.py)
- [Converter guidance](https://github.com/apache/ossie/blob/main/converters/README.md)
- [Incubation disclaimer](https://github.com/apache/ossie/blob/main/DISCLAIMER)

### Why it matters to selayer

OSSIE addresses a real missing capability: exchanging semantic models with other tools. Selayer currently has a good internal model, but no neutral import or export format.

The fit is not exact. Selayer deliberately owns semantics that OSSIE core does not yet represent:

- explicit source grain;
- separate facts, measures, and metrics;
- aggregation type;
- grain compatibility and row-expansion checks;
- typed semantic IDs;
- closed connector declarations and complete physical schemas;
- restricted engine-neutral expression ASTs;
- runtime schema fingerprints, source generations, snapshots, and health;
- OKF trust, freshness, citations, and attested computations.

OSSIE keys are useful, but a primary key is not a complete analytical grain model. OSSIE relationships always encode many-to-one direction and have no general cardinality field. OSSIE metrics are free-form dialect expressions with no declared measures, anchor source, additivity, valid dimensions, or output grain.

The official TPC-DS example exposes these gaps. A metric that divides sales by store employee count can fan out the employee count after joining to fact rows, but the interchange model has no rule to reject that plan. One composite primary-key column is not even declared as a field, and the official validator does not catch it.

### Mapping selayer to OSSIE

An exporter can map a useful subset:

| selayer | OSSIE core | Treatment |
| --- | --- | --- |
| `SemanticLayer` | `semantic_model` | Direct |
| `DataSource` | `dataset` | Direct for identity and source name |
| `DataSource.grain` | `primary_key` | Also preserve as a `SELAYER` extension |
| physical schema field | `field` | Direct with a column expression |
| `Dimension` | field metadata | Merge description and time role where possible |
| many-to-one relationship | relationship | Direct |
| one-to-many relationship | relationship | Reverse endpoints and columns |
| one-to-one relationship | relationship plus extension | Core loses exact cardinality |
| `Fact` and `Measure` | no direct equivalent | Preserve in a `SELAYER` extension |
| `Metric` | metric | Requires a generated dialect expression plus extension |
| selected OKF guidance | `ai_context` | Optional summary only; keep full OKF separate |

Connector configuration needs special handling. Database relations and portable file locations can populate `dataset.source`. Programmatic PyArrow handles and runtime profiles are not portable. Credentials, DSNs, profile values, and source samples must never appear in OSSIE output.

Metric export is the hardest part. Selayer would have to inline fact expressions and measure aggregations into an OSSIE metric expression. That can provide an ANSI SQL representation, but OSSIE cannot preserve the proof that the metric is grain-safe. The export must therefore carry the original typed IDs, grain, fact and measure decomposition, and expression form in a `SELAYER` extension.

### Why import should wait

A general OSSIE importer cannot construct a valid selayer catalog without extra input:

- `dataset.source` is an unstructured relation, path, or query, while selayer requires a closed connector declaration;
- OSSIE may omit field types and does not require complete physical schemas;
- primary keys may be absent and do not always express intended analytical grain;
- SQL expressions cannot always be converted to selayer's restricted expression AST;
- metrics do not declare the facts, measures, aggregation, or anchor source selayer requires;
- `ai_context` cannot replace an OKF bundle.

An importer would need a user-supplied binding file and a compatibility report. Building that before a real consuming use case would create a shallow adapter with a large interface and little leverage.

### Recommended seam

Start with one deep export module, not a generic interchange framework:

```text
export_ossie(layer, *, include_context=False) -> OssieExportResult
```

`OssieExportResult` should contain the document plus a deterministic compatibility report. Every selayer object must be classified as:

- represented in OSSIE core;
- preserved in the `SELAYER` extension;
- omitted by explicit policy;
- unsupported and therefore an export error.

The module should hide mapping, expression lowering, extension encoding, and validation behind this small interface. Add a generic interchange seam only when a second format or a real importer exists.

Do not make `apache-ossie==0.2.0.dev0` a core dependency. For an experiment, validate against a pinned schema snapshot or use the package only in a development extra. The current Pydantic models are frozen at the object level but contain mutable lists and perform no cross-reference or semantic validation.

## What to do first

### Experiment 1: OSSIE export

Use a small existing catalog with two sources, one relationship, dimensions, facts, measures, and two metrics.

1. Produce OSSIE YAML without changing selayer's internal model.
2. Add a versioned `SELAYER` extension for typed IDs, source grain, relationship cardinality, fact/measure decomposition, and the original expression form.
3. Run the official JSON Schema and Python validator.
4. Generate a compatibility report with no silent loss.
5. Run the export twice and require byte-identical output.

Success means every catalog object is accounted for, the document validates, no secret-bearing field is exported, and the report clearly distinguishes core representation from extension-preserved semantics.

Do not implement import, publish a converter, or add a runtime dependency during this experiment.

### Experiment 2: myKG domain context

Run this only if there is a concrete corpus of manuals or data dictionaries that OKF does not already cover.

1. Select 10 to 20 non-sensitive documents for one domain.
2. Use schema review before instance extraction.
3. Ask 10 domain questions and require supporting source chunks.
4. Measure entity duplication, orphan rate, unsupported claims, extraction cost, and repeat-run stability.
5. Convert only approved findings into draft OKF concepts or links.

Stop if fewer than eight answers cite the correct source passage, if identity collisions merge distinct entities, or if the review effort exceeds writing the missing OKF pages directly.

## Decision

1. Track and experiment with Apache OSSIE now. It addresses semantic-model portability, which selayer does not currently provide.
2. Keep the experiment export-only and deterministic. Preserve selayer's richer semantics in a documented extension and compatibility report.
3. Consider myKG only for document-derived domain context outside selayer. Access it through MCP or files, not as a library dependency.
4. Keep the authority order unchanged: catalog, verified OKF, supplemental providers. Neither OSSIE nor myKG should alter executable semantics at runtime.
