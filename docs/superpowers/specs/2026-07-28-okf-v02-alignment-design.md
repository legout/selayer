# OKF v0.2 Alignment Design

## Purpose

Align selayer's `selayer.okf` module with the **released** OKF v0.2
specification ([Open Knowledge Format
specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)).

selayer already targeted v0.2 and is broadly compliant (correct `type`-required
rule, `status`/`stale_after`, `generated { by, at }`, `verified` mapping-or-list,
trust tiers keyed off the `human:` prefix, `sources[].resource`, reserved
`index.md`/`log.md`, `okf_version: "0.2"`, and permissive round-tripping of
unknown types and extension fields). This design closes the remaining gaps
identified by an audit of `src/selayer/okf/` against the final spec.

## Scope

The **critical** gap is the under-modeled **Attested Computation** contract
(§10), v0.2's flagship feature. This design specifies how selayer models,
validates, and surfaces authored Attested Computation concepts. selayer does
**not** generate attested computations from the catalog — they are authored
knowledge, not executable catalog objects — so this work is about parsing,
validation, typed derivation, and bounded retrieval.

Secondary alignment items (lower priority) are listed in §6 and receive their
own tasks later.

## Non-goals

- Executing, attesting, or caching attestation runs. The full runtime protocol
  (receipt/verdict wire formats, attester ABI, sandboxing) is explicitly
  deferred by the spec (§12 "Considered and deferred") and out of scope.
- Generating Attested Computation concepts from the semantic catalog.
- Changing the selayer catalog's authority over executable semantics.
- A multi-provider context broker (belongs to the separate SemaLoom package).

## Attested Computation contract model

An Attested Computation concept (`type: Attested Computation`) carries a
sanctioned way to compute a value so a consumer can confirm a number was
produced by running it. Per §10.2, in addition to the provenance, trust, and
lifecycle families (§5), the concept carries:

- `runtime`: **REQUIRED for this type.** How the computation runs and how the
  executor/attester and `parameters` are interpreted. Example values:
  `bigquery`, `postgres`, `dbt`, `python`, `Looker`.
- `parameters`: optional list of typed, named holes the agent may fill. Each
  entry: `{ name, type, required }`. Binding semantics follow `runtime`.
- `computation`: optional path (§6.2) to a file holding the computation, used
  instead of an inline body fence. Absent ⇒ the body `# Computation` fence is
  the computation.
- `executor`: how the computation runs. `resource` names run instructions or
  code; `receipt` declares the fields a run must return.
- `attester`: deterministic (no-LLM) code that inspects a receipt and returns a
  verdict; `resource` names that code.

The computation is provided **either** inline (a fenced block under the body
heading `# Computation`, §4.2/§10.3) **or** via the `computation` path.

### Typed derived model

selayer keeps all frontmatter as frozen mappings (the existing convention).
The Attested Computation contract is **derived** into typed frozen dataclasses
at retrieval time, mirroring how `trust_tier()` and `freshness()` derive values
from frontmatter.

```python
@dataclass(frozen=True, slots=True)
class OkfParameter:
    name: str
    type: str
    required: bool

@dataclass(frozen=True, slots=True)
class AttestedComputation:
    runtime: str
    parameters: tuple[OkfParameter, ...]
    computation_path: str | None    # the `computation` frontmatter field, or None
    computation_body: str           # the `# Computation` body section, or ""
    executor_resource: str | None
    executor_receipt: tuple[str, ...]
    attester_resource: str | None
```

`attested_computation(concept) -> AttestedComputation | None` returns the typed
contract for an `Attested Computation` concept, or `None` for any other type.
It reads `parameters`, `computation`, `executor`, `attester` from frontmatter
and the `# Computation` section from the parsed body.

### Validation rules

All contract fields except `runtime` are **optional**. A minimal
`type: Attested Computation` with only `runtime` remains valid (this keeps the
existing MLFB fixture valid). When present, structural validation runs as
errors, consistent with how `generated`/`verified`/`sources` are validated:

- `runtime`: already validated as a required non-empty string for this type.
- `parameters`: if present, a list; each entry a mapping with a non-empty
  string `name`, a non-empty string `type`, and (if present) a boolean
  `required`.
- `computation`: if present, a non-empty string (a path per §6.2).
- `executor`: if present, a mapping with a non-empty string `resource` and,
  if present, a `receipt` list of non-empty strings.
- `attester`: if present, a mapping with a non-empty string `resource`.

Malformed members are reported as sorted `OkfIssue`s with severity `error`,
following the existing pattern.

## Surface in bounded retrieval

`OkfBundle.context_for()` already follows links (`include_linked=True`,
`max_depth=1`). A metric that links to an Attested Computation already pulls
that concept in as a `ContextItem`. The retrieval enhancement is therefore
small: populate a new optional field on `ContextItem` when the retrieved
concept is an Attested Computation.

```python
@dataclass(frozen=True, slots=True)
class ContextItem:
    concept_id: str
    kind: str
    content: str
    provider: str
    semantic_refs: tuple[str, ...]
    trust: TrustTier
    freshness: Freshness
    sources: tuple[str, ...]
    attested_computation: AttestedComputation | None = None
```

`_context_item()` populates `attested_computation` via the derivation
function. Agents consuming context for `plan_query` or `explain_result` then
receive the structured contract (runtime, parameters, computation,
executor, attester) in addition to the rendered prose. Retrieval never
executes anything and never modifies a query plan.

## Privacy and safety

Attested Computation concepts describe sanctioned computations; they never
contain query results or data values. Surfacing their contract does not change
the existing rule that catalog export contains no data values and that
retrieval is advisory only.

## Testing strategy

- **Unit tests** for each validator (`parameters`, `executor`, `attester`,
  `computation`) — valid and malformed cases, deterministic issue ordering.
- **Derivation tests** for `attested_computation()`: inline-body vs file-path
  computation, missing optional fields, non-Attested types return `None`.
- **Round-trip tests**: an authored Attested Computation survives load →
  write → load preserving all contract fields and the `# Computation` body.
- **Retrieval test**: retrieving a metric that links to an Attested
  Computation surfaces the typed contract on the linked `ContextItem`.
- **Scenario test**: the MLFB fixture gains a complete attested computation
  contract; `dimension.mlfb` retrieval surfaces it without adding queryable
  dimensions.

## Secondary alignment items

The deferred audit items are now grouped into three approved follow-up designs,
implemented in this dependency order:

1. [OKF Consumer Compatibility](2026-07-28-okf-consumer-compatibility-design.md)
   — lenient optional-family validation and v0.1 `timestamp`/`# Citations`
   fallbacks.
2. [OKF Generation Interoperability](2026-07-28-okf-generation-interoperability-design.md)
   — generic generated types, index descriptions, absolute links, fingerprint
   relocation, and catalog-backed resources.
3. [OKF Retrieval Credibility](2026-07-28-okf-retrieval-credibility-design.md)
   — typed credibility signals, usage windows, and per-claim source IDs.

The generation policy adopts generic type names while accepting legacy
`Selayer …` names. Fingerprints use dual-read/new-write auto-upgrade semantics.
Dimension resources use `<source-path>#column=<URL-encoded-column>`.
