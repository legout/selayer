# OKF Consumer Compatibility Design

## Purpose

Make `selayer.okf` a tolerant OKF v0.2 consumer without weakening its default validation guarantees, and add the two v0.1 read fallbacks permitted by OKF v0.2 §13.1.

This design covers two related consumer concerns:

1. opt-in lenient handling of malformed optional families; and
2. effective v0.2 views of legacy `timestamp` and `# Citations` metadata.

## Goals

- Preserve strict loading as the default.
- Let callers opt into best-effort consumption with `OkfBundle.load(..., strict=False)`.
- Keep required structure and selayer semantic bindings fatal in every mode.
- Ensure malformed optional metadata cannot crash later retrieval.
- Prefer explicit v0.2 metadata over legacy fallback metadata.
- Preserve authored legacy fields and body sections during round-tripping.
- Add no runtime dependency.

## Non-goals

- A CLI `--lenient` flag.
- Repairing or rewriting malformed authored metadata.
- Silently dropping diagnostics.
- Treating malformed YAML, unsafe paths, duplicate semantic bindings, or invalid required fields as recoverable.
- Removing legacy `timestamp` or `# Citations` content when a bundle is written.

## Public API

`OkfBundle.load()` gains one keyword-only option:

```python
@classmethod
def load(
    cls,
    path: str | Path,
    *,
    layer: SemanticLayer | None = None,
    strict: bool = True,
) -> OkfBundle:
    ...
```

`strict=True` preserves current behavior. `strict=False` changes only the severity of recognized optional-family validation issues from `error` to `warning`.

The compatibility module exposes deterministic effective-value helpers for internal consumers:

```python
def effective_generated_at(concept: OkfConcept) -> object | None:
    ...


def effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]:
    ...
```

These helpers do not mutate `OkfConcept.frontmatter` or `OkfConcept.sections`.

## Strict and lenient validation

`validate_concept()` gains the same keyword-only `strict: bool = True` option. Optional-family validators receive an issue severity selected once by `validate_concept()`:

```python
optional_severity: Severity = "error" if strict else "warning"
```

The following recognized optional families are soft in lenient mode:

- `status`
- `stale_after`
- `generated`
- `verified`
- `sources`
- `usage_window` once credibility validation exists
- optional Attested Computation members: `parameters`, `computation`, `executor`, and `attester`

The following remain fatal in both modes:

- unreadable files, malformed YAML, or invalid frontmatter shape
- missing or empty `type`
- missing or invalid Attested Computation `runtime`
- invalid `selayer_id` shape or catalog binding
- duplicate semantic identifiers
- unsafe/symlinked bundle paths
- malformed reserved `index.md` or `log.md` structure that is currently fatal

Unknown types and unknown extension fields remain accepted in both modes.

All returned issues remain deterministically sorted by `(path, message)`.

## Defensive consumption

Lenient loading must not defer crashes into retrieval. Existing derivation helpers become defensive:

- malformed `sources` entries are omitted from effective sources while their load warning remains available;
- malformed `stale_after` yields freshness `unspecified` rather than raising;
- malformed `verified` and `generated` mappings do not raise during trust or provenance derivation;
- malformed optional Attested Computation members derive empty/default values while their warnings remain available.

Required semantic identifiers and required Attested Computation runtime values are never defaulted in lenient mode.

## v0.1 effective values

### Legacy timestamp

`effective_generated_at()` applies this precedence:

1. `generated.at`, when `generated` is a mapping and `at` is present;
2. top-level `timestamp` when `generated` is absent;
3. `None` otherwise.

If `generated` is present but malformed, the legacy timestamp does not override it: explicit v0.2 metadata always wins, and lenient mode reports the malformed v0.2 field as a warning.

The helper returns the frozen frontmatter value unchanged. This plan imposes no new validation on the legacy `timestamp`; consumers that require a parsed datetime must validate the effective value at their boundary.

### Legacy citations

`effective_sources()` applies this precedence:

1. valid entries from frontmatter `sources` when the key is present;
2. entries parsed from an exact `# Citations` section when `sources` is absent;
3. an empty tuple otherwise.

A legacy Citations section is a Markdown unordered list. Each non-empty `-` or `*` item becomes one source mapping:

- `[Title](resource)` becomes `{"title": "Title", "resource": "resource"}`;
- a plain item becomes `{"resource": "item text"}`.

Nested list content, malformed Markdown links, and non-list prose are ignored. Source order follows body order. Duplicate resources are preserved because OKF does not require source uniqueness.

The fallback is an effective read view only: `timestamp` and `# Citations` remain unchanged in the document model and survive load → write → load.

## Retrieval integration

`ContextItem.sources`, rendered `## Sources`, and later credibility derivation consume `effective_sources()` rather than raw frontmatter. This makes v0.1 citations visible to existing context consumers without changing the public `ContextItem` shape in this plan.

`effective_generated_at()` is the single compatibility seam for provenance consumers. The retrieval-credibility plan may use it but does not duplicate fallback logic.

## Error handling

- Strict mode raises `OkfValidationError` when any error issue exists.
- Lenient mode raises only for hard errors and returns downgraded warnings in `OkfBundle.diagnostics`.
- Warning paths and messages are identical to strict-mode errors; only severity changes.
- No warning is emitted merely because a valid v0.1 fallback is used.

## Testing strategy

- Strict-default regression tests for all existing validation behavior.
- Parameterized strict-versus-lenient tests for every optional family.
- Tests proving hard failures remain fatal with `strict=False`.
- Retrieval tests proving malformed optional metadata does not raise.
- Precedence tests for v0.2 metadata over v0.1 fallbacks.
- Citation parsing tests for links, plain resources, ignored prose, and ordering.
- Load → write → load tests proving legacy fields and sections are preserved.
- Full OKF and project suite verification with Ruff and Pyright.

## Delivery order

1. Add strict/lenient severity propagation.
2. Harden optional metadata derivation and retrieval.
3. Add compatibility helpers and v0.1 fallback tests.
4. Wire effective sources into retrieval.
5. Run integration and regression verification.
