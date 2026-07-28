# OKF Retrieval Credibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the resource-only retrieval API while adding validated, immutable source credibility metadata, deterministic Markdown-footnote attribution, and conservative character-budget accounting for direct and linked OKF context.

**Architecture:** A focused `selayer.okf.sources` module derives typed source details and claim source IDs from the consumer-compatibility layer's effective source mappings without mutating authored documents. Validation remains strict by default and downgrades only recognized optional credibility-family issues under `strict=False`; `OkfBundle` attaches the derived values to `ContextItem` and `_item_chars()` bounds every returned structured value. Existing rendered content, resource-only `ContextItem.sources`, breadth-first traversal, planner behavior, and compiler behavior remain unchanged.

**Tech Stack:** Python 3.13, frozen slotted dataclasses, `datetime.date`, `collections.abc.Mapping`, PyYAML, Markdown footnote-reference regular expressions, pytest, Ruff, Pyright.

## Global Constraints

- Preserve `ContextItem.sources: tuple[str, ...]` for compatibility.
- Add immutable typed source metadata.
- Validate all recognized credibility fields deterministically.
- Resolve shared and per-source usage windows.
- Surface source IDs used by Markdown footnote claims.
- Keep retrieval deterministic, attributed, and explicitly bounded.
- Preserve unknown source-entry extensions during round-tripping.
- The following specification non-goals remain prohibited:
- Calculating or storing a credibility score.
- Ranking, filtering, or recursively traversing sources by credibility.
- Parsing natural-language claims.
- Fetching source resources.
- Replacing existing trust tiers.
- Changing planner or compiler behavior.
- Existing `ContextItem.sources` and rendered `## Sources` remain resource-only and preserve their current output.
- Structured metadata is additive through `source_details`; no extra prose is injected into `content`.
- Unknown source-entry keys remain accepted and preserved.
- Strict malformed credibility metadata prevents bundle loading.
- Lenient malformed credibility metadata produces warnings and safe typed defaults/omissions.
- Unknown claim labels are not validation errors because broken/partial attribution is soft consumer guidance; they are simply not resolved.
- Duplicate IDs are errors in strict mode and warnings in lenient mode because attribution would otherwise be ambiguous.
- No derived score or implicit freshness/trust change is introduced.
- Add no runtime dependency.

---

## Prerequisite: OKF Consumer Compatibility

Complete `docs/superpowers/plans/2026-07-28-okf-consumer-compatibility.md` before starting Task 1. This plan consumes these prerequisite interfaces exactly:

- `OkfBundle.load(path: str | Path, *, layer: SemanticLayer | None = None, strict: bool = True) -> OkfBundle`
- `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None, *, strict: bool = True) -> tuple[OkfIssue, ...]`
- `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` from `selayer.okf.compatibility`
- `optional_severity: Severity = "error" if strict else "warning"` selected once inside `validate_concept()` and passed to optional-family validators

The prerequisite must already make malformed source containers/resources warning-only and safely omittable under `strict=False`, while retaining valid effective source mappings in authored order. Do not duplicate its v0.1 `# Citations` parser or its strict/lenient bundle-loading logic in this plan.

## File responsibility map

- `src/selayer/okf/model.py` — define `OkfUsageWindow` and `ContextSource`; append additive defaults to `ContextItem`.
- `src/selayer/okf/__init__.py` — export the two new public model types.
- `src/selayer/okf/validation.py` — validate shared and per-source credibility metadata with exact paths and prerequisite strict/lenient severity.
- `src/selayer/okf/sources.py` — derive normalized source metadata from `effective_sources()` and extract unambiguous Markdown footnote source IDs.
- `src/selayer/okf/bundle.py` — preserve resource-only rendering, populate additive structured fields, and count all structured values in `max_chars`.
- `src/selayer/okf/document.py` — no production change; its frozen frontmatter and parse/render behavior are exercised by source-extension round-trip tests.
- `tests/okf/test_public_api.py` — public exports, slots, frozen behavior, and additive `ContextItem` defaults.
- `tests/okf/test_validation.py` — strict/lenient credibility validation, exact paths, duplicate IDs, boolean/negative counts, date errors, and reversed windows.
- `tests/okf/test_sources.py` — source derivation, shared/override windows, date normalization, authored ordering, defensive lenient defaults, footnote attribution, and source-extension round trips.
- `tests/okf/test_retrieval.py` — compatibility-preserving retrieval, effective legacy sources, lenient safety, and exact direct/linked budget behavior.

---

### Task 1: Add the public credibility model without breaking ContextItem construction

**Files:**

- Modify: `src/selayer/okf/model.py`
- Modify: `src/selayer/okf/__init__.py`
- Test: `tests/okf/test_public_api.py`

**Interfaces:**

- Consumes: existing `AttestedComputation` and `ContextItem(concept_id: str, kind: str, content: str, provider: str, semantic_refs: tuple[str, ...], trust: TrustTier, freshness: Freshness, sources: tuple[str, ...], attested_computation: AttestedComputation | None = None)`.
- Produces: `OkfUsageWindow(start: date, end: date)`.
- Produces: `ContextSource(id: str | None, resource: str, title: str | None, author: str | None, usage_count: int | None, last_modified: date | None, usage_window: OkfUsageWindow | None)`.
- Produces: `ContextItem.source_details: tuple[ContextSource, ...] = ()` and `ContextItem.claim_source_ids: tuple[str, ...] = ()`, appended after `attested_computation`.
- Produces: public imports `from selayer.okf import ContextSource, OkfUsageWindow`.

- [ ] **Step 1: Write the failing public-model test**

Replace `tests/okf/test_public_api.py` with:

```python
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

import selayer
import selayer.okf


def test_okf_public_api_is_the_approved_boundary() -> None:
    assert set(selayer.okf.__all__) == {
        "AttestedComputation",
        "ContextBudgetError",
        "ContextItem",
        "ContextLookupError",
        "ContextResult",
        "ContextSource",
        "OkfBundle",
        "OkfConcept",
        "OkfIssue",
        "OkfParameter",
        "OkfUsageWindow",
        "OkfValidationError",
        "SyncReport",
    }


def test_okf_exports_attested_computation_types() -> None:
    from selayer.okf import AttestedComputation, ContextItem, OkfParameter

    assert AttestedComputation.__slots__ == (
        "runtime",
        "parameters",
        "computation_path",
        "computation_body",
        "executor_resource",
        "executor_receipt",
        "attester_resource",
    )
    assert OkfParameter.__slots__ == ("name", "type", "required")
    item = ContextItem(
        concept_id="c",
        kind="Attested Computation",
        content="",
        provider="selayer",
        semantic_refs=(),
        trust="unverified",
        freshness="unspecified",
        sources=(),
    )
    assert item.attested_computation is None
    assert item.source_details == ()
    assert item.claim_source_ids == ()


def test_okf_exports_frozen_slotted_source_types() -> None:
    from selayer.okf import ContextSource, OkfUsageWindow

    window = OkfUsageWindow(start=date(2026, 1, 1), end=date(2026, 12, 31))
    source = ContextSource(
        id="policy",
        resource="https://example.com/policy",
        title="Margin Policy",
        author="Finance",
        usage_count=12,
        last_modified=date(2026, 7, 20),
        usage_window=window,
    )

    assert OkfUsageWindow.__slots__ == ("start", "end")
    assert ContextSource.__slots__ == (
        "id",
        "resource",
        "title",
        "author",
        "usage_count",
        "last_modified",
        "usage_window",
    )
    with pytest.raises(FrozenInstanceError):
        window.start = date(2026, 2, 1)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        source.title = "Changed"  # type: ignore[misc]


def test_package_root_exposes_only_okf_bundle_from_okf_api() -> None:
    assert selayer.OkfBundle is selayer.okf.OkfBundle
    assert "OkfBundle" in selayer.__all__
    assert not (
        {
            "ContextBudgetError",
            "ContextItem",
            "ContextLookupError",
            "ContextResult",
            "ContextSource",
            "OkfConcept",
            "OkfIssue",
            "OkfUsageWindow",
            "OkfValidationError",
            "SyncReport",
        }
        & set(selayer.__all__)
    )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest -q tests/okf/test_public_api.py`

Expected: FAIL during import/assertion because `ContextSource` and `OkfUsageWindow` are not exported and `ContextItem` has no `source_details` or `claim_source_ids` fields.

- [ ] **Step 3: Add the immutable models and append ContextItem defaults**

In `src/selayer/okf/model.py`, add this import immediately after the dataclasses import:

```python
from datetime import date
```

Add these dataclasses immediately before `ContextItem`:

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

Replace the `ContextItem` declaration with:

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
    source_details: tuple[ContextSource, ...] = ()
    claim_source_ids: tuple[str, ...] = ()
```

Replace `model.py`'s `__all__` with:

```python
__all__ = [
    "AttestedComputation",
    "ContextBudgetError",
    "ContextItem",
    "ContextLookupError",
    "ContextResult",
    "ContextSource",
    "Freshness",
    "OkfConcept",
    "OkfIssue",
    "OkfMetadataError",
    "OkfParameter",
    "OkfSection",
    "OkfUsageWindow",
    "OkfValidationError",
    "Severity",
    "SyncReport",
    "TrustTier",
]
```

- [ ] **Step 4: Export the model types from selayer.okf**

Replace `src/selayer/okf/__init__.py` with:

```python
"""Public interface for advisory Open Knowledge Format context."""

from .bundle import OkfBundle
from .model import (
    AttestedComputation,
    ContextBudgetError,
    ContextItem,
    ContextLookupError,
    ContextResult,
    ContextSource,
    OkfConcept,
    OkfIssue,
    OkfParameter,
    OkfUsageWindow,
    OkfValidationError,
    SyncReport,
)

__all__ = [
    "AttestedComputation",
    "ContextBudgetError",
    "ContextItem",
    "ContextLookupError",
    "ContextResult",
    "ContextSource",
    "OkfBundle",
    "OkfConcept",
    "OkfIssue",
    "OkfParameter",
    "OkfUsageWindow",
    "OkfValidationError",
    "SyncReport",
]
```

- [ ] **Step 5: Run the focused test and verify GREEN**

Run: `uv run pytest -q tests/okf/test_public_api.py && uv run ruff check src/selayer/okf/model.py src/selayer/okf/__init__.py tests/okf/test_public_api.py && uv run pyright src/selayer/okf/model.py src/selayer/okf/__init__.py tests/okf/test_public_api.py`

Expected: PASS with zero test failures, lint findings, or type errors.

- [ ] **Step 6: Commit the public-model slice**

```bash
git add src/selayer/okf/model.py src/selayer/okf/__init__.py tests/okf/test_public_api.py
git commit -m "feat(okf): model source credibility metadata"
```

---

### Task 2: Validate credibility fields under strict and lenient policy

**Files:**

- Modify: `src/selayer/okf/validation.py`
- Test: `tests/okf/test_validation.py`

**Interfaces:**

- Consumes: prerequisite `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None, *, strict: bool = True) -> tuple[OkfIssue, ...]` and its local `optional_severity: Severity`.
- Consumes: existing `_is_nonempty_string(value: object) -> bool` and `_is_iso_date(value: object) -> bool`.
- Produces: `_validate_usage_window(concept: OkfConcept, value: object, field: str, *, severity: Severity) -> list[OkfIssue]`.
- Produces: `_validate_sources(concept: OkfConcept, value: object, *, severity: Severity) -> list[OkfIssue]` covering resources plus all credibility members.
- Produces: exact strict errors and lenient warnings at `usage_window`, `usage_window.from`, `usage_window.to`, `sources[n].id`, `sources[n].title`, `sources[n].author`, `sources[n].usage_count`, `sources[n].last_modified`, and `sources[n].usage_window` descendants.

- [ ] **Step 1: Add failing field, duplicate-ID, and strict/lenient tests**

Append to `tests/okf/test_validation.py`:

```python
@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, id: ''}",
            "sources[0].id",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, title: ''}",
            "sources[0].title",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, author: ''}",
            "sources[0].author",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, usage_count: true}",
            "sources[0].usage_count",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, usage_count: -1}",
            "sources[0].usage_count",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, usage_count: 1.5}",
            "sources[0].usage_count",
        ),
        (
            "type: Metric\nsources:\n"
            "  - {resource: https://example.com/policy, last_modified: yesterday}",
            "sources[0].last_modified",
        ),
        ("type: Metric\nusage_window: nope", "usage_window"),
        (
            "type: Metric\nusage_window: {to: 2026-12-31}",
            "usage_window.from",
        ),
        (
            "type: Metric\nusage_window: {from: 2026-01-01}",
            "usage_window.to",
        ),
        (
            "type: Metric\nusage_window: {from: yesterday, to: 2026-12-31}",
            "usage_window.from",
        ),
        (
            "type: Metric\nusage_window: {from: 2026-01-01, to: tomorrow}",
            "usage_window.to",
        ),
        (
            "type: Metric\nusage_window: {from: 2026-12-31, to: 2026-01-01}",
            "usage_window",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: nope",
            "sources[0].usage_window",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: {to: 2026-12-31}",
            "sources[0].usage_window.from",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: {from: 2026-01-01}",
            "sources[0].usage_window.to",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: {from: invalid, to: 2026-12-31}",
            "sources[0].usage_window.from",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: {from: 2026-01-01, to: invalid}",
            "sources[0].usage_window.to",
        ),
        (
            "type: Metric\nsources:\n"
            "  - resource: https://example.com/policy\n"
            "    usage_window: {from: 2026-12-31, to: 2026-01-01}",
            "sources[0].usage_window",
        ),
    ],
)
def test_source_credibility_fields_are_strict_errors(
    tmp_path: Path,
    frontmatter: str,
    issue_path: str,
) -> None:
    _write_concept(tmp_path, frontmatter)

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert f"concept.md.frontmatter.{issue_path}" in {
        issue.path for issue in caught.value.issues
    }
    assert all(issue.severity == "error" for issue in caught.value.issues)


def test_duplicate_source_ids_report_every_ambiguous_entry(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric\n"
        "sources:\n"
        "  - {id: policy, resource: https://example.com/first}\n"
        "  - {id: policy, resource: https://example.com/second}",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert [issue.path for issue in caught.value.issues] == [
        "concept.md.frontmatter.sources[0].id",
        "concept.md.frontmatter.sources[1].id",
    ]
    assert all(
        issue.message == "id must be unique within the concept"
        for issue in caught.value.issues
    )


def test_credibility_issues_are_warnings_in_lenient_mode(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric\n"
        "usage_window: {from: 2026-12-31, to: 2026-01-01}\n"
        "sources:\n"
        "  - {id: policy, resource: https://example.com/first, usage_count: true}\n"
        "  - {id: policy, resource: https://example.com/second}",
    )

    bundle = OkfBundle.load(tmp_path, strict=False)

    assert [(issue.path, issue.severity) for issue in bundle.diagnostics] == [
        ("concept.md.frontmatter.sources[0].id", "warning"),
        ("concept.md.frontmatter.sources[0].usage_count", "warning"),
        ("concept.md.frontmatter.sources[1].id", "warning"),
        ("concept.md.frontmatter.usage_window", "warning"),
    ]


def test_valid_credibility_metadata_is_accepted_in_both_modes(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Metric\n"
        "usage_window: {from: 2026-01-01, to: 2026-12-31}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n"
        "    title: Margin Policy\n"
        "    author: Finance\n"
        "    usage_count: 0\n"
        "    last_modified: 2026-07-20\n"
        "  - id: warehouse\n"
        "    resource: urn:warehouse:margin\n"
        "    usage_window: {from: 2026-02-01, to: 2026-06-30}\n"
        "    custom_extension: retained",
    )

    assert OkfBundle.load(tmp_path).diagnostics == ()
    assert OkfBundle.load(tmp_path, strict=False).diagnostics == ()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/okf/test_validation.py -k "source_credibility or duplicate_source_ids or credibility_issues or valid_credibility"`

Expected: FAIL — malformed credibility fields are accepted, duplicate IDs produce no issues, and lenient diagnostics omit the new warning paths.

- [ ] **Step 3: Implement severity-aware window and source validation**

In `src/selayer/okf/validation.py`, extend the model import to:

```python
from .model import OkfConcept, OkfIssue, Severity
```

Add these helpers immediately before `_validate_sources`, then replace `_validate_sources` with the complete implementation below:

```python
def _optional_issue(
    concept: OkfConcept,
    field: str,
    message: str,
    severity: Severity,
) -> OkfIssue:
    issue = _issue(concept, field, message)
    return OkfIssue(path=issue.path, message=issue.message, severity=severity)


def _date_value(value: object) -> date | None:
    if not _is_iso_date(value):
        return None
    if isinstance(value, date):
        return value
    assert isinstance(value, str)
    return date.fromisoformat(value)


def _validate_usage_window(
    concept: OkfConcept,
    value: object,
    field: str,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [
            _optional_issue(
                concept,
                field,
                "usage_window must be a mapping",
                severity,
            )
        ]

    issues: list[OkfIssue] = []
    start = _date_value(value.get("from"))
    end = _date_value(value.get("to"))
    if start is None:
        issues.append(
            _optional_issue(
                concept,
                f"{field}.from",
                "from must be an ISO 8601 date",
                severity,
            )
        )
    if end is None:
        issues.append(
            _optional_issue(
                concept,
                f"{field}.to",
                "to must be an ISO 8601 date",
                severity,
            )
        )
    if start is not None and end is not None and start > end:
        issues.append(
            _optional_issue(
                concept,
                field,
                "usage_window from must be on or before to",
                severity,
            )
        )
    return issues


def _validate_sources(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [
            _optional_issue(
                concept,
                "sources",
                "sources must be a list",
                severity,
            )
        ]

    issues: list[OkfIssue] = []
    valid_ids: list[tuple[int, str]] = []
    for index, source in enumerate(value):
        field = f"sources[{index}]"
        if not isinstance(source, Mapping):
            issues.append(
                _optional_issue(
                    concept,
                    field,
                    "source must be a mapping",
                    severity,
                )
            )
            continue
        if not _is_nonempty_string(source.get("resource")):
            issues.append(
                _optional_issue(
                    concept,
                    f"{field}.resource",
                    "resource must be a non-empty string",
                    severity,
                )
            )
        if "id" in source:
            source_id = source.get("id")
            if not _is_nonempty_string(source_id):
                issues.append(
                    _optional_issue(
                        concept,
                        f"{field}.id",
                        "id must be a non-empty string",
                        severity,
                    )
                )
            else:
                assert isinstance(source_id, str)
                valid_ids.append((index, source_id))
        for member in ("title", "author"):
            if member in source and not _is_nonempty_string(source.get(member)):
                issues.append(
                    _optional_issue(
                        concept,
                        f"{field}.{member}",
                        f"{member} must be a non-empty string",
                        severity,
                    )
                )
        if "usage_count" in source:
            usage_count = source.get("usage_count")
            if (
                not isinstance(usage_count, int)
                or isinstance(usage_count, bool)
                or usage_count < 0
            ):
                issues.append(
                    _optional_issue(
                        concept,
                        f"{field}.usage_count",
                        "usage_count must be an integer greater than or equal to 0",
                        severity,
                    )
                )
        if "last_modified" in source and not _is_iso_date(
            source.get("last_modified")
        ):
            issues.append(
                _optional_issue(
                    concept,
                    f"{field}.last_modified",
                    "last_modified must be an ISO 8601 date",
                    severity,
                )
            )
        if "usage_window" in source:
            issues.extend(
                _validate_usage_window(
                    concept,
                    source.get("usage_window"),
                    f"{field}.usage_window",
                    severity=severity,
                )
            )

    id_counts = Counter(source_id for _, source_id in valid_ids)
    duplicates = {
        source_id for source_id, count in id_counts.items() if count > 1
    }
    for index, source_id in valid_ids:
        if source_id in duplicates:
            issues.append(
                _optional_issue(
                    concept,
                    f"sources[{index}].id",
                    "id must be unique within the concept",
                    severity,
                )
            )
    return issues
```

- [ ] **Step 4: Wire both optional families to the prerequisite severity**

Inside `validate_concept()`, replace the existing sources call with this block directly after verified validation:

```python
    if "sources" in frontmatter:
        issues.extend(
            _validate_sources(
                concept,
                frontmatter["sources"],
                severity=optional_severity,
            )
        )
    if "usage_window" in frontmatter:
        issues.extend(
            _validate_usage_window(
                concept,
                frontmatter["usage_window"],
                "usage_window",
                severity=optional_severity,
            )
        )
```

Do not create another strictness switch; use the prerequisite's one `optional_severity` value.

- [ ] **Step 5: Run validation tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_validation.py && uv run ruff check src/selayer/okf/validation.py tests/okf/test_validation.py && uv run pyright src/selayer/okf/validation.py tests/okf/test_validation.py`

Expected: PASS with strict errors, lenient warnings, deterministic `(path, message)` ordering, and no lint/type errors.

- [ ] **Step 6: Commit the validation slice**

```bash
git add src/selayer/okf/validation.py tests/okf/test_validation.py
git commit -m "feat(okf): validate source credibility metadata"
```

---

### Task 3: Derive normalized source details and usage windows

**Files:**

- Create: `src/selayer/okf/sources.py`
- Create: `tests/okf/test_sources.py`
- Verify unchanged behavior: `src/selayer/okf/document.py`

**Interfaces:**

- Consumes: prerequisite `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` from `selayer.okf.compatibility`.
- Consumes: `ContextSource` and `OkfUsageWindow` from `selayer.okf.model`.
- Produces: `context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]`.
- Produces: shared concept `usage_window` only for sources with a valid `usage_count`; an authored per-source `usage_window` takes precedence and may be exposed without `usage_count`.
- Produces: defensive `None`/empty typed values for malformed optional members that reached retrieval through `strict=False`.

- [ ] **Step 1: Write failing derivation, lenient-safety, and round-trip tests**

Create `tests/okf/test_sources.py` with:

```python
from datetime import date
from pathlib import Path, PurePosixPath

from selayer.okf import ContextSource, OkfBundle, OkfUsageWindow
from selayer.okf.model import OkfConcept
from selayer.okf.sources import context_sources


def _concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="reference",
        relative_path=PurePosixPath("reference.md"),
        frontmatter=frontmatter,
    )


def test_context_sources_preserve_order_and_normalize_dates() -> None:
    concept = _concept(
        {
            "type": "Reference",
            "usage_window": {"from": "2026-01-01", "to": date(2026, 12, 31)},
            "sources": [
                {
                    "id": "policy",
                    "resource": "https://example.com/policy",
                    "title": "Margin Policy",
                    "author": "Finance",
                    "usage_count": 7,
                    "last_modified": "2026-07-20",
                },
                {
                    "id": "warehouse",
                    "resource": "urn:warehouse:margin",
                    "usage_window": {
                        "from": date(2026, 2, 1),
                        "to": "2026-06-30",
                    },
                },
                {
                    "id": "snapshot",
                    "resource": "urn:snapshot:margin",
                    "usage_count": 0,
                    "usage_window": {
                        "from": "2026-03-01",
                        "to": "2026-03-31",
                    },
                },
                {
                    "id": "notes",
                    "resource": "urn:notes:margin",
                },
            ],
        }
    )

    assert context_sources(concept) == (
        ContextSource(
            id="policy",
            resource="https://example.com/policy",
            title="Margin Policy",
            author="Finance",
            usage_count=7,
            last_modified=date(2026, 7, 20),
            usage_window=OkfUsageWindow(
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
            ),
        ),
        ContextSource(
            id="warehouse",
            resource="urn:warehouse:margin",
            title=None,
            author=None,
            usage_count=None,
            last_modified=None,
            usage_window=OkfUsageWindow(
                start=date(2026, 2, 1),
                end=date(2026, 6, 30),
            ),
        ),
        ContextSource(
            id="snapshot",
            resource="urn:snapshot:margin",
            title=None,
            author=None,
            usage_count=0,
            last_modified=None,
            usage_window=OkfUsageWindow(
                start=date(2026, 3, 1),
                end=date(2026, 3, 31),
            ),
        ),
        ContextSource(
            id="notes",
            resource="urn:notes:margin",
            title=None,
            author=None,
            usage_count=None,
            last_modified=None,
            usage_window=None,
        ),
    )


def test_lenient_source_derivation_uses_safe_defaults(tmp_path: Path) -> None:
    (tmp_path / "reference.md").write_text(
        "---\n"
        "type: Reference\n"
        "usage_window: {from: 2026-01-01, to: 2026-12-31}\n"
        "sources:\n"
        "  - resource: https://example.com/policy\n"
        "    id: ''\n"
        "    title: []\n"
        "    author: '   '\n"
        "    usage_count: 4\n"
        "    last_modified: yesterday\n"
        "    usage_window: {from: 2026-12-31, to: 2026-01-01}\n"
        "---\n",
        encoding="utf-8",
    )

    bundle = OkfBundle.load(tmp_path, strict=False)
    source = context_sources(bundle.concepts["reference"])[0]

    assert source == ContextSource(
        id=None,
        resource="https://example.com/policy",
        title=None,
        author=None,
        usage_count=4,
        last_modified=None,
        usage_window=None,
    )
    assert {
        "reference.md.frontmatter.sources[0].author",
        "reference.md.frontmatter.sources[0].id",
        "reference.md.frontmatter.sources[0].last_modified",
        "reference.md.frontmatter.sources[0].title",
        "reference.md.frontmatter.sources[0].usage_window",
    } <= {issue.path for issue in bundle.diagnostics}
    assert all(issue.severity == "warning" for issue in bundle.diagnostics)


def test_unknown_source_extensions_survive_load_write_load(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "reference.md").write_text(
        "---\n"
        "type: Reference\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n"
        "    title: Margin Policy\n"
        "    custom_credibility:\n"
        "      reviewed_by: [finance, legal]\n"
        "---\n",
        encoding="utf-8",
    )

    loaded = OkfBundle.load(source_root)
    output_root = tmp_path / "output"
    loaded.write(output_root)
    reloaded = OkfBundle.load(output_root)

    assert reloaded.concepts["reference"].frontmatter["sources"][0][
        "custom_credibility"
    ] == {"reviewed_by": ("finance", "legal")}
```

- [ ] **Step 2: Run the new module tests and verify RED**

Run: `uv run pytest -q tests/okf/test_sources.py`

Expected: FAIL during collection because `selayer.okf.sources` does not exist.

- [ ] **Step 3: Implement the focused derivation module**

Create `src/selayer/okf/sources.py` with:

```python
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime

from .compatibility import effective_sources
from .model import ContextSource, OkfConcept, OkfUsageWindow

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) else None


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _usage_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_window(value: object) -> OkfUsageWindow | None:
    if not isinstance(value, Mapping):
        return None
    start = _date_value(value.get("from"))
    end = _date_value(value.get("to"))
    if start is None or end is None or start > end:
        return None
    return OkfUsageWindow(start=start, end=end)


def context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]:
    """Derive typed credibility details from effective source mappings."""
    shared_window = _usage_window(concept.frontmatter.get("usage_window"))
    derived: list[ContextSource] = []
    for source in effective_sources(concept):
        resource = _nonempty_string(source.get("resource"))
        if resource is None:
            continue
        usage_count = _usage_count(source.get("usage_count"))
        if "usage_window" in source:
            usage_window = _usage_window(source.get("usage_window"))
        elif usage_count is not None:
            usage_window = shared_window
        else:
            usage_window = None
        derived.append(
            ContextSource(
                id=_nonempty_string(source.get("id")),
                resource=resource,
                title=_nonempty_string(source.get("title")),
                author=_nonempty_string(source.get("author")),
                usage_count=usage_count,
                last_modified=_date_value(source.get("last_modified")),
                usage_window=usage_window,
            )
        )
    return tuple(derived)


__all__ = ["context_sources"]
```

- [ ] **Step 4: Run derivation and document round-trip tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_sources.py tests/okf/test_document.py && uv run ruff check src/selayer/okf/sources.py tests/okf/test_sources.py && uv run pyright src/selayer/okf/sources.py tests/okf/test_sources.py`

Expected: PASS; source order and normalized dates are exact, malformed lenient details default safely, unknown nested source keys survive, and there are zero lint/type errors.

- [ ] **Step 5: Commit the derivation slice**

```bash
git add src/selayer/okf/sources.py tests/okf/test_sources.py
git commit -m "feat(okf): derive typed source details"
```

---

### Task 4: Extract deterministic unambiguous claim source IDs

**Files:**

- Modify: `src/selayer/okf/sources.py`
- Modify: `tests/okf/test_sources.py`

**Interfaces:**

- Consumes: `context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]` from Task 3.
- Produces: `claim_source_ids(concept: OkfConcept) -> tuple[str, ...]`.
- Produces: full-body source-order scanning for references matching `[^label]`, excluding definition lines, unknown IDs, and every duplicated/ambiguous source ID.
- Produces: first-reference ordering and label deduplication without parsing claim prose.

- [ ] **Step 1: Add failing footnote-attribution tests**

Append to `tests/okf/test_sources.py` and extend its model import to include `OkfSection`:

```python
from selayer.okf.model import OkfConcept, OkfSection
```

```python
def test_claim_source_ids_follow_first_reference_order() -> None:
    concept = OkfConcept.create(
        concept_id="metric",
        relative_path=PurePosixPath("metric.md"),
        frontmatter={
            "type": "Metric",
            "sources": [
                {"id": "policy", "resource": "https://example.com/policy"},
                {"id": "dataset", "resource": "urn:dataset:margin"},
            ],
        },
        preamble="Dataset claim [^dataset] and unknown claim [^missing].",
        sections=(
            OkfSection(
                "Definition",
                "Policy claim [^policy]. Repeated dataset claim [^dataset].",
            ),
            OkfSection(
                "Footnotes",
                "[^policy]: Finance policy.\n[^dataset]: Warehouse dataset.",
            ),
        ),
    )

    assert claim_source_ids(concept) == ("dataset", "policy")


def test_footnote_definitions_alone_do_not_count_as_claims() -> None:
    concept = OkfConcept.create(
        concept_id="reference",
        relative_path=PurePosixPath("reference.md"),
        frontmatter={
            "type": "Reference",
            "sources": [
                {"id": "policy", "resource": "https://example.com/policy"},
                {"id": "dataset", "resource": "urn:dataset:margin"},
            ],
        },
        sections=(
            OkfSection(
                "Footnotes",
                "[^policy]: Policy mentions [^dataset].\n"
                "[^dataset]: Dataset description.",
            ),
        ),
    )

    assert claim_source_ids(concept) == ()


def test_duplicate_source_ids_never_create_ambiguous_attribution() -> None:
    concept = OkfConcept.create(
        concept_id="reference",
        relative_path=PurePosixPath("reference.md"),
        frontmatter={
            "type": "Reference",
            "sources": [
                {"id": "duplicate", "resource": "urn:first"},
                {"id": "duplicate", "resource": "urn:second"},
                {"id": "unique", "resource": "urn:unique"},
            ],
        },
        preamble="Ambiguous [^duplicate], resolved [^unique].",
    )

    assert claim_source_ids(concept) == ("unique",)
```

Also change the sources import at the top of the test file to:

```python
from selayer.okf.sources import claim_source_ids, context_sources
```

- [ ] **Step 2: Run footnote tests and verify RED**

Run: `uv run pytest -q tests/okf/test_sources.py -k "claim_source_ids or footnote_definitions or duplicate_source_ids"`

Expected: FAIL during collection because `claim_source_ids` is not importable from `selayer.okf.sources`.

- [ ] **Step 3: Replace sources.py with the complete derivation and attribution module**

Replace `src/selayer/okf/sources.py` with:

```python
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime

from .compatibility import effective_sources
from .model import ContextSource, OkfConcept, OkfUsageWindow

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FOOTNOTE_REFERENCE = re.compile(r"\[\^([^\]\r\n]+)\]")
_FOOTNOTE_DEFINITION = re.compile(r"^[ \t]{0,3}\[\^[^\]\r\n]+\]:")


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) else None


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _usage_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_window(value: object) -> OkfUsageWindow | None:
    if not isinstance(value, Mapping):
        return None
    start = _date_value(value.get("from"))
    end = _date_value(value.get("to"))
    if start is None or end is None or start > end:
        return None
    return OkfUsageWindow(start=start, end=end)


def context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]:
    """Derive typed credibility details from effective source mappings."""
    shared_window = _usage_window(concept.frontmatter.get("usage_window"))
    derived: list[ContextSource] = []
    for source in effective_sources(concept):
        resource = _nonempty_string(source.get("resource"))
        if resource is None:
            continue
        usage_count = _usage_count(source.get("usage_count"))
        if "usage_window" in source:
            usage_window = _usage_window(source.get("usage_window"))
        elif usage_count is not None:
            usage_window = shared_window
        else:
            usage_window = None
        derived.append(
            ContextSource(
                id=_nonempty_string(source.get("id")),
                resource=resource,
                title=_nonempty_string(source.get("title")),
                author=_nonempty_string(source.get("author")),
                usage_count=usage_count,
                last_modified=_date_value(source.get("last_modified")),
                usage_window=usage_window,
            )
        )
    return tuple(derived)


def _markdown_body(concept: OkfConcept) -> str:
    parts: list[str] = []
    if concept.preamble:
        parts.append(concept.preamble)
    parts.extend(
        f"# {section.title}\n\n{section.content}".rstrip()
        for section in concept.sections
    )
    return "\n\n".join(parts)


def claim_source_ids(concept: OkfConcept) -> tuple[str, ...]:
    """Resolve first-use Markdown footnote labels to unique source IDs."""
    ids = tuple(
        source.id for source in context_sources(concept) if source.id is not None
    )
    valid_ids = {
        source_id
        for source_id, count in Counter(ids).items()
        if count == 1
    }
    claimed: list[str] = []
    seen: set[str] = set()
    for line in _markdown_body(concept).splitlines():
        if _FOOTNOTE_DEFINITION.match(line) is not None:
            continue
        for label in _FOOTNOTE_REFERENCE.findall(line):
            if label in valid_ids and label not in seen:
                seen.add(label)
                claimed.append(label)
    return tuple(claimed)


__all__ = ["claim_source_ids", "context_sources"]
```

- [ ] **Step 4: Run all source tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_sources.py && uv run ruff check src/selayer/okf/sources.py tests/okf/test_sources.py && uv run pyright src/selayer/okf/sources.py tests/okf/test_sources.py`

Expected: PASS; definitions and unknown/duplicate labels contribute no claims, first references are stable, and there are zero lint/type errors.

- [ ] **Step 5: Commit the attribution slice**

```bash
git add src/selayer/okf/sources.py tests/okf/test_sources.py
git commit -m "feat(okf): resolve claim source footnotes"
```

---

### Task 5: Populate structured credibility during compatible retrieval

**Files:**

- Modify: `src/selayer/okf/bundle.py`
- Modify: `tests/okf/test_retrieval.py`

**Interfaces:**

- Consumes: `context_sources(concept: OkfConcept) -> tuple[ContextSource, ...]` and `claim_source_ids(concept: OkfConcept) -> tuple[str, ...]` from Task 4.
- Consumes: prerequisite effective-source behavior, including v0.1 exact `# Citations` fallback and lenient omission of malformed source mappings/resources.
- Produces: `_context_item(concept: OkfConcept, today: date) -> ContextItem` with resource-only `sources`, typed `source_details`, and resolved `claim_source_ids` from one effective source view.
- Preserves: `_render_context(concept: OkfConcept, sources: tuple[str, ...]) -> str` and its resource-only `## Sources` output.

- [ ] **Step 1: Add failing retrieval compatibility and effective-source tests**

Extend the `selayer.okf` import in `tests/okf/test_retrieval.py` to include `ContextSource` and `OkfUsageWindow`:

```python
from selayer.okf import (
    ContextBudgetError,
    ContextLookupError,
    ContextSource,
    OkfBundle,
    OkfConcept,
    OkfUsageWindow,
)
```

Append these tests:

```python
def test_retrieval_preserves_resource_content_and_adds_source_details(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\n"
        "title: Margin\n"
        "selayer_id: metric.margin\n"
        "usage_window: {from: 2026-01-01, to: 2026-12-31}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n"
        "    title: Margin Policy\n"
        "    author: Finance\n"
        "    usage_count: 12\n"
        "    last_modified: 2026-07-20",
        "\n# Definition\n\nMargin follows policy [^policy].\n\n"
        "[^policy]: Approved finance policy.\n",
    )

    item = OkfBundle.load(tmp_path).context_for(
        ["metric.margin"], include_linked=False
    ).items[0]

    assert item.sources == ("https://example.com/policy",)
    assert item.content == (
        "# Margin\n\n"
        "# Definition\n\n"
        "Margin follows policy [^policy].\n\n"
        "[^policy]: Approved finance policy.\n\n"
        "## Sources\n\n"
        "- https://example.com/policy"
    )
    assert "Margin Policy" not in item.content
    assert "Finance" not in item.content
    assert item.source_details == (
        ContextSource(
            id="policy",
            resource="https://example.com/policy",
            title="Margin Policy",
            author="Finance",
            usage_count=12,
            last_modified=date(2026, 7, 20),
            usage_window=OkfUsageWindow(
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
            ),
        ),
    )
    assert item.claim_source_ids == ("policy",)


def test_retrieval_source_details_use_legacy_citations_fallback(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\nselayer_id: metric.margin",
        "\n# Definition\n\nMargin guidance.\n\n"
        "# Citations\n\n"
        "- [Margin Policy](https://example.com/policy)\n"
        "* urn:warehouse:margin\n",
    )

    item = OkfBundle.load(tmp_path).context_for(
        ["metric.margin"], include_linked=False
    ).items[0]

    assert item.sources == (
        "https://example.com/policy",
        "urn:warehouse:margin",
    )
    assert item.source_details == (
        ContextSource(
            id=None,
            resource="https://example.com/policy",
            title="Margin Policy",
            author=None,
            usage_count=None,
            last_modified=None,
            usage_window=None,
        ),
        ContextSource(
            id=None,
            resource="urn:warehouse:margin",
            title=None,
            author=None,
            usage_count=None,
            last_modified=None,
            usage_window=None,
        ),
    )
    assert item.claim_source_ids == ()


def test_lenient_retrieval_keeps_valid_resources_and_safe_details(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\n"
        "selayer_id: metric.margin\n"
        "sources:\n"
        "  - resource: https://example.com/policy\n"
        "    id: ''\n"
        "    usage_count: true\n"
        "    last_modified: yesterday",
    )

    bundle = OkfBundle.load(tmp_path, strict=False)
    result = bundle.context_for(["metric.margin"], include_linked=False)
    item = result.items[0]

    assert item.sources == ("https://example.com/policy",)
    assert item.source_details == (
        ContextSource(
            id=None,
            resource="https://example.com/policy",
            title=None,
            author=None,
            usage_count=None,
            last_modified=None,
            usage_window=None,
        ),
    )
    assert item.claim_source_ids == ()
    assert {
        "metrics/margin.md.frontmatter.sources[0].id",
        "metrics/margin.md.frontmatter.sources[0].last_modified",
        "metrics/margin.md.frontmatter.sources[0].usage_count",
    } <= {issue.path for issue in result.diagnostics}
```

- [ ] **Step 2: Run the retrieval tests and verify RED**

Run: `uv run pytest -q tests/okf/test_retrieval.py -k "adds_source_details or legacy_citations_fallback or safe_details"`

Expected: FAIL — `source_details` and `claim_source_ids` remain empty, and retrieval does not yet populate credibility from effective sources.

- [ ] **Step 3: Wire source derivation into _context_item without changing prose**

In `src/selayer/okf/bundle.py`, add this import:

```python
from .sources import claim_source_ids, context_sources
```

Delete the obsolete `_sources(frontmatter)` helper. Replace `_context_item()` with:

```python
def _context_item(concept: OkfConcept, today: date) -> ContextItem:
    frontmatter = concept.frontmatter
    source_details = context_sources(concept)
    sources = tuple(source.resource for source in source_details)
    semantic_id = frontmatter.get("selayer_id")
    return ContextItem(
        concept_id=concept.concept_id,
        kind=frontmatter["type"],
        content=_render_context(concept, sources),
        provider="selayer",
        semantic_refs=(semantic_id,) if isinstance(semantic_id, str) else (),
        trust=trust_tier(frontmatter),
        freshness=freshness(frontmatter, today),
        sources=sources,
        attested_computation=attested_computation(concept),
        source_details=source_details,
        claim_source_ids=claim_source_ids(concept),
    )
```

Do not change `_render_context()` and do not append credibility prose.

- [ ] **Step 4: Run retrieval compatibility tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_retrieval.py -k "context_for_returns_attributed_direct_concept or adds_source_details or legacy_citations_fallback or safe_details" && uv run ruff check src/selayer/okf/bundle.py tests/okf/test_retrieval.py && uv run pyright src/selayer/okf/bundle.py tests/okf/test_retrieval.py`

Expected: PASS; existing exact rendered content remains resource-only, additive fields are populated, legacy citations flow through the prerequisite seam, lenient retrieval is safe, and there are zero lint/type errors.

- [ ] **Step 5: Commit the retrieval integration slice**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_retrieval.py
git commit -m "feat(okf): surface source credibility in context"
```

---

### Task 6: Bound every structured source value for direct and linked retrieval

**Files:**

- Modify: `src/selayer/okf/bundle.py`
- Modify: `tests/okf/test_retrieval.py`

**Interfaces:**

- Consumes: `ContextItem.source_details`, `ContextItem.claim_source_ids`, and the existing `ContextItem.attested_computation` contract.
- Produces: `_item_chars(item: ContextItem) -> int` counting rendered content; every source ID/resource/title/author; decimal `usage_count`; ISO `last_modified`; ISO window start/end; every claim label; and all existing Attested Computation values.
- Preserves: required-item overflow raises `ContextBudgetError(required_chars: int, max_chars: int)`.
- Preserves: linked-item overflow omits the item breadth-first and adds one warning with `path="context"`.

- [ ] **Step 1: Add exact direct and linked budget tests**

Append to `tests/okf/test_retrieval.py`:

```python
def _full_credibility_chars() -> int:
    return sum(
        len(value)
        for value in (
            "policy",
            "https://example.com/policy",
            "Margin Policy",
            "Finance",
            "12",
            "2026-07-20",
            "2026-01-01",
            "2026-12-31",
            "policy",
        )
    )


def test_direct_source_credibility_counts_every_structured_value(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\n"
        "selayer_id: metric.margin\n"
        "usage_window: {from: 2026-01-01, to: 2026-12-31}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n"
        "    title: Margin Policy\n"
        "    author: Finance\n"
        "    usage_count: 12\n"
        "    last_modified: 2026-07-20",
        "\n# Definition\n\nMargin follows policy [^policy].\n",
    )
    bundle = OkfBundle.load(tmp_path)

    ample = bundle.context_for(["metric.margin"], include_linked=False)
    item = ample.items[0]
    required = len(item.content) + _full_credibility_chars()

    assert ample.total_chars == required
    with pytest.raises(ContextBudgetError) as caught:
        bundle.context_for(
            ["metric.margin"],
            include_linked=False,
            max_chars=required - 1,
        )
    assert caught.value.required_chars == required
    assert caught.value.max_chars == required - 1


def test_linked_source_credibility_counts_every_structured_value(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\nselayer_id: metric.margin",
        "\n# Definition\n\nSee [policy](../references/policy.md).\n",
    )
    _write_concept(
        tmp_path,
        "references/policy.md",
        "type: Reference\n"
        "usage_window: {from: 2026-01-01, to: 2026-12-31}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n"
        "    title: Margin Policy\n"
        "    author: Finance\n"
        "    usage_count: 12\n"
        "    last_modified: 2026-07-20",
        "\n# Guidance\n\nApproved policy [^policy].\n",
    )
    bundle = OkfBundle.load(tmp_path)

    ample = bundle.context_for(["metric.margin"], max_depth=1)
    metric_item, policy_item = ample.items
    required = (
        len(metric_item.content)
        + len(policy_item.content)
        + _full_credibility_chars()
    )
    assert ample.total_chars == required

    constrained = bundle.context_for(
        ["metric.margin"],
        max_depth=1,
        max_chars=required - 1,
    )

    assert [item.concept_id for item in constrained.items] == ["metrics/margin"]
    assert constrained.total_chars == len(metric_item.content)
    assert [
        (issue.path, issue.severity)
        for issue in constrained.diagnostics
        if "omitted linked context" in issue.message
    ] == [("context", "warning")]
```

- [ ] **Step 2: Run exact budget tests and verify RED**

Run: `uv run pytest -q tests/okf/test_retrieval.py -k "source_credibility_counts_every"`

Expected: FAIL — `total_chars` omits the structured credibility values, the direct call does not raise at `required - 1`, and the linked item is not omitted at that boundary.

- [ ] **Step 3: Replace _item_chars with complete conservative accounting**

Replace `_item_chars()` in `src/selayer/okf/bundle.py` with:

```python
def _item_chars(item: ContextItem) -> int:
    """Return the conservative character size of every context value."""
    total = len(item.content)
    for source in item.source_details:
        if source.id is not None:
            total += len(source.id)
        total += len(source.resource)
        if source.title is not None:
            total += len(source.title)
        if source.author is not None:
            total += len(source.author)
        if source.usage_count is not None:
            total += len(str(source.usage_count))
        if source.last_modified is not None:
            total += len(source.last_modified.isoformat())
        if source.usage_window is not None:
            total += len(source.usage_window.start.isoformat())
            total += len(source.usage_window.end.isoformat())
    total += sum(len(source_id) for source_id in item.claim_source_ids)

    contract = item.attested_computation
    if contract is None:
        return total
    total += len(contract.runtime)
    for parameter in contract.parameters:
        total += len(parameter.name)
        total += len(parameter.type)
        total += len("true") if parameter.required else len("false")
    if contract.computation_path is not None:
        total += len(contract.computation_path)
    total += len(contract.computation_body)
    if contract.executor_resource is not None:
        total += len(contract.executor_resource)
    total += sum(len(receipt) for receipt in contract.executor_receipt)
    if contract.attester_resource is not None:
        total += len(contract.attester_resource)
    return total
```

The resource string is intentionally counted once in rendered content and once in `source_details`; a source ID used by a claim is intentionally counted once in `source_details` and once in `claim_source_ids`.

- [ ] **Step 4: Run all retrieval tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_retrieval.py && uv run ruff check src/selayer/okf/bundle.py tests/okf/test_retrieval.py && uv run pyright src/selayer/okf/bundle.py tests/okf/test_retrieval.py`

Expected: PASS; direct overflow reports the exact required count, linked overflow omits breadth-first with one warning, existing Attested Computation accounting remains exact, and there are zero lint/type errors.

- [ ] **Step 5: Commit the budget-accounting slice**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_retrieval.py
git commit -m "feat(okf): bound structured source context"
```

---

## Final full verification

- [ ] **Step 1: Run all OKF tests**

Run: `uv run pytest -q tests/okf`

Expected: PASS with zero failures, including strict/lenient loading, source derivation, footnote attribution, round trips, retrieval compatibility, and direct/linked budgets.

- [ ] **Step 2: Run the full project test suite**

Run: `uv run pytest -q`

Expected: PASS with zero failures and no planner/compiler regressions.

- [ ] **Step 3: Run repository-wide lint and type checks**

Run: `uv run ruff check . && uv run pyright`

Expected: both commands exit 0 with no findings.

- [ ] **Step 4: Check patch hygiene and dependency stability**

Run: `git diff --check HEAD~6..HEAD && git diff --exit-code HEAD~6..HEAD -- pyproject.toml uv.lock`

Expected: both commands exit 0; there is no whitespace error and no dependency-file change.

- [ ] **Step 5: Confirm the six-task file boundary**

Run: `git diff --name-only HEAD~6..HEAD | sort`

Expected output:

```text
src/selayer/okf/__init__.py
src/selayer/okf/bundle.py
src/selayer/okf/model.py
src/selayer/okf/sources.py
src/selayer/okf/validation.py
tests/okf/test_public_api.py
tests/okf/test_retrieval.py
tests/okf/test_sources.py
tests/okf/test_validation.py
```

- [ ] **Step 6: Confirm the implementation branch is clean**

Run: `git status --short`

Expected: no output.
