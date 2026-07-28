# OKF Retrieval Credibility Design

## Purpose

Surface OKF v0.2 source credibility signals and per-claim attribution through bounded advisory context, without replacing the existing resource-only `ContextItem.sources` API or inventing a credibility score.

## Goals

- Preserve `ContextItem.sources: tuple[str, ...]` for compatibility.
- Add immutable typed source metadata.
- Validate all recognized credibility fields deterministically.
- Resolve shared and per-source usage windows.
- Surface source IDs used by Markdown footnote claims.
- Keep retrieval deterministic, attributed, and explicitly bounded.
- Preserve unknown source-entry extensions during round-tripping.

## Non-goals

- Calculating or storing a credibility score.
- Ranking, filtering, or recursively traversing sources by credibility.
- Parsing natural-language claims.
- Fetching source resources.
- Replacing existing trust tiers.
- Changing planner or compiler behavior.

## Public model

Add frozen, slotted dataclasses:

```python
@dataclass(frozen=True, slots=True)
class OkfUsageWindow:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class ContextSource:
    id: str | None
    resource: str
    title: str | None
    author: str | None
    usage_count: int | None
    last_modified: date | None
    usage_window: OkfUsageWindow | None
```

Extend `ContextItem` by appending fields so existing positional construction remains valid:

```python
@dataclass(frozen=True, slots=True)
class ContextItem:
    # existing fields unchanged
    attested_computation: AttestedComputation | None = None
    source_details: tuple[ContextSource, ...] = ()
    claim_source_ids: tuple[str, ...] = ()
```

Export `ContextSource` and `OkfUsageWindow` from `selayer.okf`.

`source_details` preserves authored source order. `claim_source_ids` contains valid footnote labels in first-reference order, deduplicated by label. A label is included only when it matches a `source_details.id`.

## Source derivation

A focused module derives source details from the effective source mappings supplied by the consumer-compatibility layer:

```python
def context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]:
    ...


def claim_source_ids(concept: OkfConcept) -> tuple[str, ...]:
    ...
```

The concept-level `usage_window` applies to every source carrying a `usage_count`. A source entry's own `usage_window` overrides the shared value. Sources without `usage_count` may still carry and expose a usage window; selayer preserves what was authored rather than inferring intent.

Date values accepted from PyYAML may be `datetime.date` or ISO `YYYY-MM-DD` strings. Derived typed values always use `datetime.date`.

Malformed optional source details are omitted/defaulted defensively only when a bundle was loaded leniently; their warnings remain in `OkfBundle.diagnostics`.

## Validation

Extend source validation with exact paths:

- `sources[n].id`, if present: non-empty string;
- source IDs: unique within one concept;
- `sources[n].title`, if present: non-empty string;
- `sources[n].author`, if present: non-empty string;
- `sources[n].usage_count`, if present: integer `>= 0`; booleans are rejected;
- `sources[n].last_modified`, if present: ISO `YYYY-MM-DD` date;
- shared `usage_window`, if present: mapping with valid `from` and `to` dates;
- `sources[n].usage_window`, if present: same shape;
- every usage window must satisfy `from <= to`.

All these fields are optional-family constraints: strict loading reports errors; `strict=False` reports warnings.

Unknown source-entry keys remain accepted and preserved.

## Per-claim attribution

OKF v0.2 attributes claims through Markdown footnotes whose labels match `sources[].id`.

The extractor scans the full Markdown body in source order for footnote references matching:

```text
[^source-id]
```

It ignores footnote definition lines when determining first claim reference. It does not parse the footnote prose or attempt to isolate sentence text.

Rules:

- include a referenced label only when it matches a unique source ID;
- preserve first-reference order;
- deduplicate repeated labels;
- unknown labels remain in rendered content but are not added to `claim_source_ids`;
- duplicate source IDs are validation issues and never produce ambiguous attribution;
- footnote definitions alone do not count as claims.

This gives consumers the stable join keys while `ContextItem.content` retains the exact claim and footnote context.

## Rendering and compatibility

Existing `ContextItem.sources` and rendered `## Sources` remain resource-only and preserve their current output. Structured metadata is additive through `source_details`; no extra prose is injected into `content`.

This avoids duplicate presentation and preserves consumers that compare rendered context exactly.

## Bounded retrieval

`_item_chars()` includes every newly returned structured value:

- source ID, resource, title, and author strings;
- decimal character length of `usage_count`;
- ISO text of `last_modified`;
- ISO text of usage-window start and end;
- each `claim_source_ids` label.

The existing resource string is counted once in rendered content and again in structured `source_details`, matching the conservative policy already used for Attested Computation bodies. Required items raise `ContextBudgetError`; linked items are omitted breadth-first with the existing warning.

## Error handling

- Strict malformed credibility metadata prevents bundle loading.
- Lenient malformed credibility metadata produces warnings and safe typed defaults/omissions.
- Unknown claim labels are not validation errors because broken/partial attribution is soft consumer guidance; they are simply not resolved.
- Duplicate IDs are errors in strict mode and warnings in lenient mode because attribution would otherwise be ambiguous.
- No derived score or implicit freshness/trust change is introduced.

## Testing strategy

- Public API tests for frozen slots and additive `ContextItem` defaults.
- Parameterized validation tests for every field, duplicate IDs, booleans, negative counts, malformed dates, reversed windows, and source overrides.
- Derivation tests for shared windows, overrides, date normalization, and source ordering.
- Footnote tests for first-reference ordering, deduplication, definition-only labels, unknown labels, and duplicates.
- Retrieval tests preserving existing `sources` and content while exposing typed details.
- Exact required and linked budget tests for all structured values.
- Lenient-mode safety tests coordinated with the consumer-compatibility plan.
- Parse/render round-trip tests for unknown source extensions.
- Full OKF and project suite verification with Ruff and Pyright.

## Delivery order

1. Add public typed models with compatibility defaults.
2. Extend strict/lenient source validation.
3. Derive normalized source details and usage windows.
4. Extract claim source IDs.
5. Populate retrieval and extend budget accounting.
6. Run integration and regression verification.
