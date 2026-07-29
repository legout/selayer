# OKF Consumer Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `selayer.okf` a tolerant OKF v0.2 consumer without weakening its default validation guarantees, harden optional-metadata derivation/retrieval so malformed data cannot crash later reads, and add the two v0.1 read fallbacks permitted by OKF v0.2 §13.1 (legacy `timestamp` and `# Citations`) through non-mutating effective-value helpers.

**Architecture:** `validate_concept()` and `OkfBundle.load()` gain a keyword-only `strict: bool = True` option; `strict=False` downgrades only recognized optional-family issues from `error` to `warning` while required structure, selayer semantic bindings, malformed YAML, duplicate bindings, and reserved files stay fatal in every mode. A focused `selayer.okf.compatibility` module exposes deterministic, non-mutating `effective_generated_at()` and `effective_sources()` helpers with explicit v0.2-over-v0.1 precedence and an exact `# Citations` parser. Retrieval consumes `effective_sources()` so legacy citations become visible to existing context consumers without changing `ContextItem`'s shape, and `freshness()` is hardened so malformed optional metadata cannot raise during retrieval. No CLI flag, no new runtime dependency.

**Tech Stack:** Python 3.13, frozen slotted dataclasses, `collections.abc.Mapping`, `types.MappingProxyType`, `datetime.date`/`datetime`, stdlib `re` for citation parsing, PyYAML (usage unchanged), pytest, Ruff, Pyright.

## Global Constraints

- `OkfBundle.load(path: str | Path, *, layer: SemanticLayer | None = None, strict: bool = True) -> OkfBundle` is the public loading seam.
- `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None, *, strict: bool = True) -> tuple[OkfIssue, ...]` selects `optional_severity: Severity = "error" if strict else "warning"` exactly once inside its body and passes it to every optional-family validator.
- `strict=True` preserves current behavior. `strict=False` changes only the severity of recognized optional-family validation issues from `error` to `warning`.
- Soft optional families downgraded under `strict=False`: `status`, `stale_after`, `generated`, `verified`, `sources`, and the optional Attested Computation members `parameters`, `computation`, `executor`, and `attester` (the mutual-exclusivity issue at `computation` is downgraded with it). `usage_window` is reserved for the retrieval-credibility plan and is not introduced here.
- The following remain fatal in both modes: unreadable files, malformed YAML, invalid frontmatter shape, missing or empty `type`, missing or invalid Attested Computation `runtime`, invalid `selayer_id` shape or catalog binding, duplicate semantic identifiers, unsafe/symlinked bundle paths, and malformed reserved `index.md`/`log.md` structure.
- Unknown types and unknown extension fields remain accepted in both modes.
- All returned issues remain deterministically sorted by `(path, message)`.
- Warning paths and messages are identical to strict-mode errors; only severity changes. No warning is emitted merely because a valid v0.1 fallback is used.
- `effective_generated_at(concept: OkfConcept) -> object | None` and `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` do not mutate `OkfConcept.frontmatter` or `OkfConcept.sections`.
- Legacy `timestamp` and `# Citations` remain unchanged in the document model and survive load → write → load.
- `ContextItem`'s public shape is unchanged in this plan; retrieval gains no new fields.
- Add no runtime dependency (`re`, `MappingProxyType`, `datetime` are stdlib; PyYAML usage is unchanged).
- No CLI flag: `src/selayer/okf/cli.py` is unchanged and keeps calling `OkfBundle.load(bundle, layer=layer)` (default `strict=True`).

## Downstream consumers

This plan is a prerequisite for the retrieval-credibility and generation-interoperability plans. It produces these interfaces exactly:

- `OkfBundle.load(path, *, layer=None, strict=True) -> OkfBundle`
- `validate_concept(concept, layer=None, *, strict=True) -> tuple[OkfIssue, ...]` with one internal `optional_severity: Severity`
- `_optional_issue(concept: OkfConcept, field: str, message: str, severity: Severity) -> OkfIssue` in `validation.py` — the shared severity helper neighboring plans reuse
- `_validate_sources(concept: OkfConcept, value: object, *, severity: Severity) -> list[OkfIssue]` — already severity-aware so the retrieval-credibility plan only extends its body
- `effective_generated_at(concept: OkfConcept) -> object | None` from `selayer.okf.compatibility`
- `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` from `selayer.okf.compatibility`, which already omits malformed source mappings/resources so lenient retrieval cannot crash

## File responsibility map

- `src/selayer/okf/validation.py` — add the keyword-only `strict` option to `validate_concept()`, the single `optional_severity` selector, the `_optional_issue` helper, and convert every optional-family validator to severity-aware.
- `src/selayer/okf/bundle.py` — add the keyword-only `strict` option to `OkfBundle.load()` and thread it to `validate_concept()`; harden `freshness()` against malformed `stale_after`; route `_sources()`/`_context_item()` through `effective_sources()`.
- `src/selayer/okf/compatibility.py` — new module exposing non-mutating `effective_generated_at()` (v0.2 `generated.at` over legacy `timestamp`) and `effective_sources()` (frontmatter `sources` over `# Citations` fallback) with an exact Markdown-list citation parser.
- `src/selayer/okf/cli.py` — no production change; its `OkfBundle.load(bundle, layer=layer)` call is exercised by strict-default regression.
- `tests/okf/test_validation.py` — strict-default regression, direct strict/lenient `validate_concept()` coverage for every optional family, hard-families-remain-fatal coverage, and lenient bundle-loading behavior.
- `tests/okf/test_compatibility.py` — new module covering precedence, malformed-metadata omission, citation parsing rules, non-mutation, and load → write → load round trips.
- `tests/okf/test_retrieval.py` — defensive `freshness()` behavior and effective-source retrieval integration without a `ContextItem` shape change.

---

### Task 1: Propagate strict/lenient severity through concept validation

**Files:**

- Modify: `src/selayer/okf/validation.py:19-20` (model import), `src/selayer/okf/validation.py:35-37` (after `_issue`), `src/selayer/okf/validation.py:70-89` (`_validate_event`), `src/selayer/okf/validation.py:91-107` (`_validate_generated`), `src/selayer/okf/validation.py:109-136` (`_validate_verified`), `src/selayer/okf/validation.py:138-156` (`_validate_sources`), `src/selayer/okf/validation.py:201-259` (`validate_concept`), `src/selayer/okf/validation.py:261-293` (`_validate_parameters`, `_validate_computation`), `src/selayer/okf/validation.py:295-336` (`_validate_executor`, `_validate_attester`)
- Test: `tests/okf/test_validation.py`

**Interfaces:**

- Consumes: existing `_issue(concept: OkfConcept, field: str, message: str) -> OkfIssue`, `_is_nonempty_string(value: object) -> bool`, `_is_iso_date(value: object) -> bool`, `_is_iso_datetime(value: object) -> bool`, and every existing `_validate_*` optional-family helper.
- Produces: `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None, *, strict: bool = True) -> tuple[OkfIssue, ...]`.
- Produces: `optional_severity: Severity = "error" if strict else "warning"` selected exactly once inside `validate_concept()`.
- Produces: `_optional_issue(concept: OkfConcept, field: str, message: str, severity: Severity) -> OkfIssue`.
- Produces: severity-aware signatures `_validate_event(..., *, require_at: bool, severity: Severity)`, `_validate_generated(concept, value, *, severity: Severity)`, `_validate_verified(concept, value, *, severity: Severity)`, `_validate_sources(concept, value, *, severity: Severity)`, `_validate_parameters(concept, value, *, severity: Severity)`, `_validate_computation(concept, value, *, severity: Severity)`, `_validate_executor(concept, value, *, severity: Severity)`, `_validate_attester(concept, value, *, severity: Severity)`.
- Keeps fatal (uses `_issue`, severity stays `"error"`): `type`, Attested Computation `runtime`, `selayer_id` shape/catalog binding.

- [ ] **Step 1: Write the failing strict/lenient validation tests**

Replace the import block at the top of `tests/okf/test_validation.py`:

```python
from pathlib import Path, PurePosixPath

import pytest

from selayer import SemanticLayer
from selayer.okf import OkfBundle, OkfConcept, OkfValidationError
from selayer.okf.validation import validate_concept
```

Append the in-memory helper and these tests after the existing `_write_concept` helper:

```python
def _make_concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
    )


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": "Metric", "status": "bogus"},
        {"type": "Metric", "stale_after": "not-a-date"},
        {"type": "Metric", "generated": []},
        {
            "type": "Metric",
            "generated": {"by": "process:build", "fingerprint": "short"},
        },
        {"type": "Metric", "verified": "nope"},
        {"type": "Metric", "sources": "nope"},
        {"type": "Metric", "sources": [{"id": "policy"}]},
        {"type": "Attested Computation", "runtime": "python", "parameters": "nope"},
        {"type": "Attested Computation", "runtime": "python", "computation": ""},
        {"type": "Attested Computation", "runtime": "python", "executor": "nope"},
        {"type": "Attested Computation", "runtime": "python", "attester": "nope"},
    ],
)
def test_validate_concept_lenient_downgrades_optional_families_to_warnings(
    frontmatter: dict[str, object],
) -> None:
    issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert issues
    assert all(issue.severity == "warning" for issue in issues)
    assert list(issues) == sorted(
        issues, key=lambda issue: (issue.path, issue.message)
    )


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": ""},
        {"type": "Attested Computation"},
        {"type": "Selayer Metric", "selayer_id": "metric."},
        {"type": "Selayer Metric", "selayer_id": "bogus"},
    ],
)
def test_validate_concept_keeps_hard_families_fatal_in_lenient_mode(
    frontmatter: dict[str, object],
) -> None:
    issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert issues
    assert all(issue.severity == "error" for issue in issues)


def test_strict_default_keeps_optional_families_as_errors() -> None:
    issues = validate_concept(
        _make_concept({"type": "Metric", "status": "bogus"}), None
    )

    assert issues
    assert all(issue.severity == "error" for issue in issues)


def test_lenient_issues_share_paths_and_messages_with_strict_errors() -> None:
    frontmatter = {"type": "Metric", "status": "bogus", "sources": "nope"}

    strict_issues = validate_concept(_make_concept(frontmatter), None)
    lenient_issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert [(i.path, i.message) for i in strict_issues] == [
        (i.path, i.message) for i in lenient_issues
    ]
    assert all(i.severity == "error" for i in strict_issues)
    assert all(i.severity == "warning" for i in lenient_issues)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/okf/test_validation.py -k "validate_concept_lenient or hard_families_fatal or strict_default_keeps or lenient_issues_share"`

Expected: FAIL — `validate_concept()` raises `TypeError: ... got an unexpected keyword argument 'strict'` because the keyword option does not exist yet.

- [ ] **Step 3: Add the `Severity` import and the `_optional_issue` helper**

In `src/selayer/okf/validation.py`, change the model import to:

```python
from .model import OkfConcept, OkfIssue, Severity
```

Add this helper immediately after the existing `_issue` function (before `_is_nonempty_string`):

```python
def _optional_issue(
    concept: OkfConcept,
    field: str,
    message: str,
    severity: Severity,
) -> OkfIssue:
    issue = _issue(concept, field, message)
    return OkfIssue(path=issue.path, message=issue.message, severity=severity)
```

- [ ] **Step 4: Convert the optional-family validators to severity-aware**

Replace the `_validate_event` function with:

```python
def _validate_event(
    concept: OkfConcept,
    event: object,
    field: str,
    *,
    require_at: bool,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(event, Mapping):
        return [
            _optional_issue(
                concept,
                field,
                f"{field.rsplit('.', 1)[-1]} must be a mapping",
                severity,
            )
        ]
    issues: list[OkfIssue] = []
    actor = event.get("by")
    if not _is_nonempty_string(actor):
        issues.append(
            _optional_issue(
                concept, f"{field}.by", "by must be a non-empty string", severity
            )
        )
    if "at" not in event:
        if require_at:
            issues.append(
                _optional_issue(concept, f"{field}.at", "at is required", severity)
            )
    elif not _is_iso_datetime(event["at"]):
        issues.append(
            _optional_issue(
                concept,
                f"{field}.at",
                "at must be an ISO 8601 datetime",
                severity,
            )
        )
    return issues
```

Replace the `_validate_generated` function with:

```python
def _validate_generated(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    issues = _validate_event(
        concept, value, "generated", require_at=False, severity=severity
    )
    if isinstance(value, Mapping) and "fingerprint" in value:
        fingerprint = value["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or _SHA256_HEX.fullmatch(fingerprint) is None
        ):
            issues.append(
                _optional_issue(
                    concept,
                    "generated.fingerprint",
                    "fingerprint must be a 64-character SHA-256 hex digest",
                    severity,
                )
            )
    return issues
```

Replace the `_validate_verified` function with:

```python
def _validate_verified(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if isinstance(value, Mapping):
        events: tuple[object, ...] = (value,)
        is_sequence = False
    elif isinstance(value, (list, tuple)):
        if not value:
            return [
                _optional_issue(
                    concept,
                    "verified",
                    "verified must contain at least one event",
                    severity,
                )
            ]
        events = tuple(value)
        is_sequence = True
    else:
        return [
            _optional_issue(
                concept, "verified", "verified must be a mapping or list", severity
            )
        ]
    return [
        issue
        for index, event in enumerate(events)
        for issue in _validate_event(
            concept,
            event,
            f"verified[{index}]" if is_sequence else "verified",
            require_at=True,
            severity=severity,
        )
    ]
```

Replace the `_validate_sources` function with:

```python
def _validate_sources(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [
            _optional_issue(concept, "sources", "sources must be a list", severity)
        ]
    issues: list[OkfIssue] = []
    for index, source in enumerate(value):
        field = f"sources[{index}]"
        if not isinstance(source, Mapping):
            issues.append(
                _optional_issue(concept, field, "source must be a mapping", severity)
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
    return issues
```

Replace the `_validate_parameters` function with:

```python
def _validate_parameters(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [
            _optional_issue(
                concept, "parameters", "parameters must be a list", severity
            )
        ]
    issues: list[OkfIssue] = []
    for index, param in enumerate(value):
        field = f"parameters[{index}]"
        if not isinstance(param, Mapping):
            issues.append(
                _optional_issue(concept, field, "parameter must be a mapping", severity)
            )
            continue
        if not _is_nonempty_string(param.get("name")):
            issues.append(
                _optional_issue(
                    concept, f"{field}.name", "name must be a non-empty string", severity
                )
            )
        if not _is_nonempty_string(param.get("type")):
            issues.append(
                _optional_issue(
                    concept, f"{field}.type", "type must be a non-empty string", severity
                )
            )
        if "required" in param and not isinstance(param.get("required"), bool):
            issues.append(
                _optional_issue(
                    concept, f"{field}.required", "required must be a boolean", severity
                )
            )
    return issues
```

Replace the `_validate_computation` function with:

```python
def _validate_computation(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not _is_nonempty_string(value):
        return [
            _optional_issue(
                concept,
                "computation",
                "computation must be a non-empty string path",
                severity,
            )
        ]
    return []
```

Replace the `_validate_executor` function with:

```python
def _validate_executor(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [_optional_issue(concept, "executor", "executor must be a mapping", severity)]
    issues: list[OkfIssue] = []
    if not _is_nonempty_string(value.get("resource")):
        issues.append(
            _optional_issue(
                concept,
                "executor.resource",
                "resource must be a non-empty string",
                severity,
            )
        )
    receipt = value.get("receipt")
    if "receipt" in value:
        if not isinstance(receipt, (list, tuple)):
            issues.append(
                _optional_issue(
                    concept, "executor.receipt", "receipt must be a list", severity
                )
            )
        elif not receipt:
            issues.append(
                _optional_issue(
                    concept,
                    "executor.receipt",
                    "receipt must contain at least one field",
                    severity,
                )
            )
        else:
            for index, name in enumerate(receipt):
                if not _is_nonempty_string(name):
                    issues.append(
                        _optional_issue(
                            concept,
                            f"executor.receipt[{index}]",
                            "receipt field must be a non-empty string",
                            severity,
                        )
                    )
    return issues
```

Replace the `_validate_attester` function with:

```python
def _validate_attester(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [_optional_issue(concept, "attester", "attester must be a mapping", severity)]
    if not _is_nonempty_string(value.get("resource")):
        return [
            _optional_issue(
                concept,
                "attester.resource",
                "resource must be a non-empty string",
                severity,
            )
        ]
    return []
```

- [ ] **Step 5: Thread the single optional severity through validate_concept**

Replace the `validate_concept` function with:

```python
def validate_concept(
    concept: OkfConcept,
    layer: SemanticLayer | None = None,
    *,
    strict: bool = True,
) -> tuple[OkfIssue, ...]:
    """Validate required and recognized OKF v0.2 frontmatter fields."""
    frontmatter = concept.frontmatter
    optional_severity: Severity = "error" if strict else "warning"
    issues: list[OkfIssue] = []
    concept_type = frontmatter.get("type")
    if not _is_nonempty_string(concept_type):
        issues.append(_issue(concept, "type", "type must be a non-empty string"))
    status = frontmatter.get("status")
    if "status" in frontmatter and (
        not isinstance(status, str) or status not in _STATUS
    ):
        issues.append(
            _optional_issue(
                concept,
                "status",
                "status must be one of: deprecated, draft, stable",
                optional_severity,
            )
        )
    if "stale_after" in frontmatter and not _is_iso_date(frontmatter["stale_after"]):
        issues.append(
            _optional_issue(
                concept,
                "stale_after",
                "stale_after must be an ISO 8601 date",
                optional_severity,
            )
        )
    if "generated" in frontmatter:
        issues.extend(
            _validate_generated(
                concept, frontmatter["generated"], severity=optional_severity
            )
        )
    if "verified" in frontmatter:
        issues.extend(
            _validate_verified(
                concept, frontmatter["verified"], severity=optional_severity
            )
        )
    if "sources" in frontmatter:
        issues.extend(
            _validate_sources(
                concept, frontmatter["sources"], severity=optional_severity
            )
        )
    if concept_type == "Attested Computation":
        if not _is_nonempty_string(frontmatter.get("runtime")):
            issues.append(
                _issue(concept, "runtime", "runtime must be a non-empty string")
            )
        if "parameters" in frontmatter:
            issues.extend(
                _validate_parameters(
                    concept, frontmatter["parameters"], severity=optional_severity
                )
            )
        if "computation" in frontmatter:
            issues.extend(
                _validate_computation(
                    concept, frontmatter["computation"], severity=optional_severity
                )
            )
        if _is_nonempty_string(frontmatter.get("computation")) and any(
            section.title == _COMPUTATION_SECTION for section in concept.sections
        ):
            issues.append(
                _optional_issue(
                    concept,
                    "computation",
                    "computation path and inline Computation section are "
                    "mutually exclusive; provide exactly one computation source",
                    optional_severity,
                )
            )
        if "executor" in frontmatter:
            issues.extend(
                _validate_executor(
                    concept, frontmatter["executor"], severity=optional_severity
                )
            )
        if "attester" in frontmatter:
            issues.extend(
                _validate_attester(
                    concept, frontmatter["attester"], severity=optional_severity
                )
            )
    if "selayer_id" in frontmatter:
        issues.extend(_validate_selayer_id(concept, frontmatter["selayer_id"], layer))
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
```

- [ ] **Step 6: Run validation tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_validation.py && uv run ruff check src/selayer/okf/validation.py tests/okf/test_validation.py && uv run pyright src/selayer/okf/validation.py tests/okf/test_validation.py`

Expected: PASS — lenient issues are warnings, hard families stay errors in both modes, strict default keeps current behavior, paths/messages are identical across modes, issues stay sorted, and there are zero lint/type errors.

- [ ] **Step 7: Commit the validation severity slice**

```bash
git add src/selayer/okf/validation.py tests/okf/test_validation.py
git commit -m "feat(okf): propagate strict/lenient severity in validation"
```

---

### Task 2: Add strict loading to OkfBundle

**Files:**

- Modify: `src/selayer/okf/bundle.py:553-557` (`load` signature), `src/selayer/okf/bundle.py:580` (`validate_concept` call)
- Test: `tests/okf/test_validation.py`

**Interfaces:**

- Consumes: Task 1's `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None, *, strict: bool = True) -> tuple[OkfIssue, ...]`.
- Produces: `OkfBundle.load(path: str | Path, *, layer: SemanticLayer | None = None, strict: bool = True) -> OkfBundle`.
- Preserves: fatality stays "any `error`-severity issue raises `OkfValidationError`", so under `strict=False` only hard errors raise and optional issues become loadable `OkfBundle.diagnostics` warnings.

- [ ] **Step 1: Write the failing lenient-loading tests**

Append to `tests/okf/test_validation.py`:

```python
def test_lenient_load_downgrades_optional_errors_to_warnings(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Metric\nstatus: unknown")

    bundle = OkfBundle.load(tmp_path, strict=False)

    assert [(issue.path, issue.severity) for issue in bundle.diagnostics] == [
        ("concept.md.frontmatter.status", "warning")
    ]
    assert bundle.concepts["concept"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "type: ''",
        "type: Attested Computation",
        "type: Selayer Metric\nselayer_id: metric.",
    ],
)
def test_hard_failures_remain_fatal_in_lenient_mode(
    tmp_path: Path, frontmatter: str
) -> None:
    _write_concept(tmp_path, frontmatter)

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path, strict=False)

    assert caught.value.issues
    assert all(issue.severity == "error" for issue in caught.value.issues)


def test_strict_load_default_remains_fatal_for_optional_errors(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Metric\nstatus: unknown")

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert caught.value.issues[0].severity == "error"


def test_malformed_yaml_remains_fatal_in_lenient_mode(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        "---\ntype: [unterminated\n---\n", encoding="utf-8"
    )

    with pytest.raises(OkfValidationError):
        OkfBundle.load(tmp_path, strict=False)


def test_duplicate_bindings_remain_fatal_in_lenient_mode(
    tmp_path: Path, valid_catalog_path: Path
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(
            "---\ntype: Selayer Metric\nselayer_id: metric.gross_margin\n---\n",
            encoding="utf-8",
        )

    with pytest.raises(OkfValidationError):
        OkfBundle.load(tmp_path, layer=layer, strict=False)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/okf/test_validation.py -k "lenient_load_downgrades or hard_failures_remain_fatal or strict_load_default_remains or malformed_yaml_remains or duplicate_bindings_remain"`

Expected: FAIL — `OkfBundle.load()` raises `TypeError: ... got an unexpected keyword argument 'strict'` because the keyword option does not exist yet.

- [ ] **Step 3: Add the strict option to OkfBundle.load**

In `src/selayer/okf/bundle.py`, replace the `load` signature block:

```python
    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        layer: SemanticLayer | None = None,
    ) -> OkfBundle:
```

with:

```python
    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        layer: SemanticLayer | None = None,
        strict: bool = True,
    ) -> OkfBundle:
```

Replace the single concept-validation call inside `load`:

```python
            issues.extend(validate_concept(concept, layer))
```

with:

```python
            issues.extend(validate_concept(concept, layer, strict=strict))
```

Do not change the fatality check — it already raises only on `error`-severity issues, which keeps hard errors fatal and lets downgraded optional warnings through.

- [ ] **Step 4: Run loading tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_validation.py && uv run ruff check src/selayer/okf/bundle.py tests/okf/test_validation.py && uv run pyright src/selayer/okf/bundle.py tests/okf/test_validation.py`

Expected: PASS — lenient load downgrades optional errors to loadable warnings, every hard failure (missing type/runtime, malformed `selayer_id`, malformed YAML, duplicate bindings) stays fatal under `strict=False`, strict default is unchanged, and there are zero lint/type errors.

- [ ] **Step 5: Commit the loading slice**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_validation.py
git commit -m "feat(okf): add strict loading to OkfBundle"
```

---

### Task 3: Harden freshness derivation against malformed stale_after

**Files:**

- Modify: `src/selayer/okf/bundle.py:123-128` (`freshness`)
- Test: `tests/okf/test_retrieval.py`

**Interfaces:**

- Consumes: Task 2's lenient loading (for the integrated lenient-retrieval-safety test) and the existing `context_for(...)` public seam.
- Produces: `freshness(frontmatter: Mapping[str, Any], today: date) -> Freshness` that never raises for malformed `stale_after` and returns `"unspecified"` for non-date, non-ISO-string, and `datetime` values (a `datetime` is rejected because OKF requires a date here).
- Preserves: `"current"`/`"stale"` results for valid `date` and ISO-date-string values, so existing retrieval assertions are unchanged.

- [ ] **Step 1: Write the failing freshness-hardening tests**

In `tests/okf/test_retrieval.py`, replace the datetime import:

```python
from datetime import date
```

with:

```python
from datetime import date, datetime, timedelta, timezone
```

Append these tests:

```python
def test_malformed_stale_after_yields_unspecified_freshness_without_raising() -> None:
    concept = OkfConcept.create(
        concept_id="metrics/margin",
        relative_path=PurePosixPath("metrics/margin.md"),
        frontmatter={
            "type": "Selayer Metric",
            "selayer_id": "metric.margin",
            "stale_after": "not-a-date",
        },
    )
    bundle = OkfBundle(root=None, concepts={concept.concept_id: concept})

    result = bundle.context_for(["metric.margin"], include_linked=False)

    assert result.items[0].freshness == "unspecified"


def test_datetime_stale_after_is_treated_as_unspecified() -> None:
    concept = OkfConcept.create(
        concept_id="metrics/margin",
        relative_path=PurePosixPath("metrics/margin.md"),
        frontmatter={
            "type": "Selayer Metric",
            "selayer_id": "metric.margin",
            "stale_after": datetime(
                2026, 7, 27, 10, 0, 0, tzinfo=timezone(timedelta(0))
            ),
        },
    )
    bundle = OkfBundle(root=None, concepts={concept.concept_id: concept})

    result = bundle.context_for(["metric.margin"], include_linked=False)

    assert result.items[0].freshness == "unspecified"


def test_lenient_retrieval_does_not_crash_on_malformed_stale_after(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\nselayer_id: metric.margin\nstale_after: not-a-date",
    )

    bundle = OkfBundle.load(tmp_path, strict=False)
    result = bundle.context_for(["metric.margin"], include_linked=False)

    assert result.items[0].freshness == "unspecified"
    assert any(
        issue.path == "metrics/margin.md.frontmatter.stale_after"
        and issue.severity == "warning"
        for issue in result.diagnostics
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/okf/test_retrieval.py -k "malformed_stale_after or datetime_stale_after or lenient_retrieval_does_not_crash"`

Expected: FAIL — `freshness()` raises `ValueError` (for the ISO-string case via `date.fromisoformat("not-a-date")`) or `TypeError` (for the `datetime` case, because comparing `date >= datetime` is not allowed), so `context_for(...)` crashes instead of returning `"unspecified"`.

- [ ] **Step 3: Harden freshness against every malformed shape**

In `src/selayer/okf/bundle.py`, replace the `freshness` function with:

```python
def freshness(frontmatter: Mapping[str, Any], today: date) -> Freshness:
    value = frontmatter.get("stale_after")
    if isinstance(value, datetime):
        return "unspecified"
    if isinstance(value, date):
        stale_after = value
    elif isinstance(value, str):
        try:
            stale_after = date.fromisoformat(value)
        except ValueError:
            return "unspecified"
    else:
        return "unspecified"
    return "stale" if today >= stale_after else "current"
```

The `datetime` branch is checked before the `date` branch because `datetime` subclasses `date`; OKF requires a bare date for `stale_after`, so a `datetime` is treated as malformed.

- [ ] **Step 4: Run retrieval tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_retrieval.py && uv run ruff check src/selayer/okf/bundle.py tests/okf/test_retrieval.py && uv run pyright src/selayer/okf/bundle.py tests/okf/test_retrieval.py`

Expected: PASS — malformed and `datetime` `stale_after` yield `"unspecified"` without raising, the lenient path surfaces its warning diagnostic, valid values still resolve to `"current"`/`"stale"`, and there are zero lint/type errors.

- [ ] **Step 5: Commit the freshness-hardening slice**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_retrieval.py
git commit -m "fix(okf): harden freshness against malformed stale_after"
```

---

### Task 4: Add the effective_generated_at compatibility helper

**Files:**

- Create: `src/selayer/okf/compatibility.py`
- Create: `tests/okf/test_compatibility.py`

**Interfaces:**

- Consumes: `OkfConcept.frontmatter: Mapping[str, Any]` and the stdlib `collections.abc.Mapping` ABC.
- Produces: `effective_generated_at(concept: OkfConcept) -> object | None` in `selayer.okf.compatibility` applying precedence `generated.at` (mapping with `at`) → top-level `timestamp` (when `generated` absent) → `None`, with a present-but-malformed `generated` field never falling back to `timestamp`.
- Produces: non-mutation of `OkfConcept.frontmatter` and `OkfConcept.sections`.
- Note: `effective_sources` is added to the same module in Task 5; this task exports only `effective_generated_at`.

- [ ] **Step 1: Write the failing precedence and non-mutation tests**

Create `tests/okf/test_compatibility.py` with:

```python
from datetime import datetime, timezone
from pathlib import PurePosixPath

from selayer.okf import OkfConcept
from selayer.okf.compatibility import effective_generated_at


def _concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
    )


def test_effective_generated_at_prefers_generated_at() -> None:
    stamped = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    concept = _concept(
        {
            "type": "Metric",
            "generated": {"by": "process:selayer-okf", "at": stamped},
            "timestamp": "2026-01-01",
        }
    )

    assert effective_generated_at(concept) is stamped


def test_effective_generated_at_falls_back_to_legacy_timestamp() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    assert effective_generated_at(concept) == "2026-01-01"


def test_effective_generated_at_returns_none_without_metadata() -> None:
    assert effective_generated_at(_concept({"type": "Metric"})) is None


def test_malformed_generated_does_not_fall_back_to_timestamp() -> None:
    concept = _concept(
        {"type": "Metric", "generated": "not-a-mapping", "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_generated_mapping_without_at_returns_none_without_fallback() -> None:
    concept = _concept(
        {"type": "Metric", "generated": {"by": "process:x"}, "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_effective_generated_at_does_not_mutate_frontmatter() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    effective_generated_at(concept)

    assert dict(concept.frontmatter) == {"type": "Metric", "timestamp": "2026-01-01"}
```

- [ ] **Step 2: Run the new module tests and verify RED**

Run: `uv run pytest -q tests/okf/test_compatibility.py`

Expected: FAIL during collection because `selayer.okf.compatibility` does not exist (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the compatibility module with effective_generated_at**

Create `src/selayer/okf/compatibility.py` with:

```python
"""Non-mutating effective-value helpers for OKF consumer compatibility."""

from __future__ import annotations

from collections.abc import Mapping

from .model import OkfConcept

__all__ = ["effective_generated_at"]


def effective_generated_at(concept: OkfConcept) -> object | None:
    """Return the effective generation timestamp without mutating the concept.

    Precedence:
      1. ``generated.at`` when ``generated`` is a mapping that carries ``at``;
      2. top-level ``timestamp`` when ``generated`` is absent;
      3. ``None`` otherwise.

    A present-but-malformed ``generated`` field never falls back to the legacy
    ``timestamp``: explicit v0.2 metadata always wins. The frozen frontmatter
    value is returned unchanged.
    """
    frontmatter = concept.frontmatter
    if "generated" in frontmatter:
        generated = frontmatter["generated"]
        if isinstance(generated, Mapping) and "at" in generated:
            return generated["at"]
        return None
    if "timestamp" in frontmatter:
        return frontmatter["timestamp"]
    return None
```

- [ ] **Step 4: Run the helper tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_compatibility.py && uv run ruff check src/selayer/okf/compatibility.py tests/okf/test_compatibility.py && uv run pyright src/selayer/okf/compatibility.py tests/okf/test_compatibility.py`

Expected: PASS — `generated.at` wins, legacy `timestamp` is used only when `generated` is absent, malformed `generated` returns `None` without fallback, the frozen frontmatter is unchanged, and there are zero lint/type errors.

- [ ] **Step 5: Commit the effective_generated_at slice**

```bash
git add src/selayer/okf/compatibility.py tests/okf/test_compatibility.py
git commit -m "feat(okf): add effective_generated_at compatibility helper"
```

---

### Task 5: Add effective_sources with the v0.1 Citations fallback

**Files:**

- Modify: `src/selayer/okf/compatibility.py` (module header, imports, `__all__`, add `effective_sources` and its citation parser)
- Test: `tests/okf/test_compatibility.py`

**Interfaces:**

- Consumes: `OkfConcept.frontmatter: Mapping[str, Any]`, `OkfConcept.sections: tuple[OkfSection, ...]`, and the stdlib `types.MappingProxyType` for the parsed fallback mappings.
- Produces: `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` applying precedence: valid frontmatter `sources` entries (when the key is present) → `# Citations` list items (when `sources` is absent) → empty tuple.
- Produces: frontmatter entries that are not mappings or lack a non-empty `resource` are omitted; a malformed `sources` container yields an empty tuple.
- Produces: citation parsing where each non-indented `-`/`*` item becomes `[Title](resource)` → `{"title": "Title", "resource": "resource"}` or a plain item → `{"resource": "item text"}`; nested/indented list content, non-list prose, empty items, and empty-resource links are ignored; source order and duplicate resources are preserved.
- Produces: non-mutation of `OkfConcept.frontmatter` and `OkfConcept.sections`, and preservation of legacy `timestamp`/`# Citations` through load → write → load.

- [ ] **Step 1: Write the failing precedence, parsing, non-mutation, and round-trip tests**

Append to `tests/okf/test_compatibility.py`. First extend its imports by replacing the existing import block with:

```python
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

from selayer.okf import OkfBundle, OkfConcept
from selayer.okf.compatibility import effective_generated_at, effective_sources
from selayer.okf.model import OkfSection
```

Append the section-concept helper and these tests:

```python
def _section_concept(
    frontmatter: dict[str, object], sections: tuple[OkfSection, ...]
) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
        sections=sections,
    )


def test_effective_sources_return_frontmatter_entries_when_present() -> None:
    concept = _concept(
        {
            "type": "Metric",
            "sources": [
                {"id": "policy", "resource": "https://example.com/policy"},
                {"resource": "urn:warehouse:margin"},
            ],
        }
    )

    assert effective_sources(concept) == (
        concept.frontmatter["sources"][0],
        concept.frontmatter["sources"][1],
    )


def test_effective_sources_omit_malformed_frontmatter_entries() -> None:
    raw = [
        {"id": "policy", "resource": "https://example.com/policy"},
        {"id": "broken"},
        "not-a-mapping",
        {"resource": "   "},
        {"resource": "urn:warehouse:margin"},
    ]
    concept = _concept({"type": "Metric", "sources": raw})

    assert [source["resource"] for source in effective_sources(concept)] == [
        "https://example.com/policy",
        "urn:warehouse:margin",
    ]


def test_effective_sources_returns_empty_for_malformed_container() -> None:
    concept = _concept({"type": "Metric", "sources": "not-a-list"})

    assert effective_sources(concept) == ()


def test_effective_sources_fall_back_to_citations_when_sources_absent() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "- [Margin Policy](https://example.com/policy)\n"
                "* urn:warehouse:margin\n",
            ),
        ),
    )

    assert [dict(source) for source in effective_sources(concept)] == [
        {"title": "Margin Policy", "resource": "https://example.com/policy"},
        {"resource": "urn:warehouse:margin"},
    ]


def test_citations_ignore_nested_prose_and_non_list_lines() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "Intro prose is ignored.\n"
                "- [Policy](https://example.com/policy)\n"
                "  - nested item ignored\n"
                "plain text ignored\n"
                "- urn:warehouse:margin\n",
            ),
        ),
    )

    assert [source["resource"] for source in effective_sources(concept)] == [
        "https://example.com/policy",
        "urn:warehouse:margin",
    ]


def test_citations_empty_resource_links_are_ignored() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (OkfSection("Citations", "- [Broken]()\n- urn:ok\n"),),
    )

    assert [source["resource"] for source in effective_sources(concept)] == ["urn:ok"]


def test_citations_preserve_order_and_duplicate_resources() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "- urn:first\n- [Second](urn:second)\n- urn:first\n",
            ),
        ),
    )

    assert [source["resource"] for source in effective_sources(concept)] == [
        "urn:first",
        "urn:second",
        "urn:first",
    ]


def test_effective_sources_returns_empty_without_sources_or_citations() -> None:
    concept = _concept({"type": "Metric"})

    assert effective_sources(concept) == ()


def test_effective_sources_does_not_mutate_frontmatter_or_sections() -> None:
    concept = _section_concept(
        {"type": "Metric", "sources": [{"resource": "urn:ok"}]},
        (OkfSection("Citations", "- urn:cite\n"),),
    )

    effective_sources(concept)

    assert [dict(source) for source in concept.frontmatter["sources"]] == [
        {"resource": "urn:ok"}
    ]
    assert [section.content for section in concept.sections] == ["- urn:cite\n"]


def test_legacy_timestamp_and_citations_survive_load_write_load(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "concept.md").write_text(
        "---\n"
        "type: Metric\n"
        "timestamp: 2026-01-01\n"
        "---\n"
        "# Meaning\n\nAuthored body.\n\n"
        "# Citations\n\n"
        "- [Policy](https://example.com/policy)\n",
        encoding="utf-8",
    )

    loaded = OkfBundle.load(source_root)
    output_root = tmp_path / "output"
    loaded.write(output_root)
    reloaded = OkfBundle.load(output_root)

    rewritten = (output_root / "concept.md").read_text(encoding="utf-8")
    assert "timestamp: 2026-01-01" in rewritten
    assert "# Citations" in rewritten
    assert "[Policy](https://example.com/policy)" in rewritten
    # PyYAML parses unquoted ISO dates as date objects; the helper returns
    # the stored value unchanged to preserve its non-mutating contract.
    assert effective_generated_at(reloaded.concepts["concept"]) == date(2026, 1, 1)
    assert [
        source["resource"]
        for source in effective_sources(reloaded.concepts["concept"])
    ] == ["https://example.com/policy"]
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `uv run pytest -q tests/okf/test_compatibility.py -k "effective_sources or citations or legacy_timestamp"`

Expected: FAIL during collection — `effective_sources` cannot be imported from `selayer.okf.compatibility` (`ImportError: cannot import name 'effective_sources'`).

- [ ] **Step 3: Add effective_sources and the Citations parser**

Replace the entire contents of `src/selayer/okf/compatibility.py` with:

```python
"""Non-mutating effective-value helpers for OKF consumer compatibility."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from .model import OkfConcept

_CITATIONS_SECTION = "Citations"
_LIST_ITEM = re.compile(r"^[-*][ \t]+(.+)$")
_MARKDOWN_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]*)\)$")

__all__ = ["effective_generated_at", "effective_sources"]


def effective_generated_at(concept: OkfConcept) -> object | None:
    """Return the effective generation timestamp without mutating the concept.

    Precedence:
      1. ``generated.at`` when ``generated`` is a mapping that carries ``at``;
      2. top-level ``timestamp`` when ``generated`` is absent;
      3. ``None`` otherwise.

    A present-but-malformed ``generated`` field never falls back to the legacy
    ``timestamp``: explicit v0.2 metadata always wins. The frozen frontmatter
    value is returned unchanged.
    """
    frontmatter = concept.frontmatter
    if "generated" in frontmatter:
        generated = frontmatter["generated"]
        if isinstance(generated, Mapping) and "at" in generated:
            return generated["at"]
        return None
    if "timestamp" in frontmatter:
        return frontmatter["timestamp"]
    return None


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_frontmatter_sources(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        source
        for source in value
        if isinstance(source, Mapping) and _is_nonempty_string(source.get("resource"))
    )


def _citations_section(concept: OkfConcept) -> str | None:
    for section in concept.sections:
        if section.title == _CITATIONS_SECTION:
            return section.content
    return None


def _parse_citations(content: str) -> tuple[Mapping[str, object], ...]:
    sources: list[Mapping[str, object]] = []
    for line in content.splitlines():
        match = _LIST_ITEM.match(line)
        if match is None:
            continue
        text = match.group(1).strip()
        if not text:
            continue
        link = _MARKDOWN_LINK.match(text)
        if link is not None:
            resource = link.group(2)
            if not resource:
                continue
            sources.append(
                MappingProxyType({"title": link.group(1), "resource": resource})
            )
        else:
            sources.append(MappingProxyType({"resource": text}))
    return tuple(sources)


def effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]:
    """Return effective source mappings without mutating the concept.

    Precedence:
      1. valid entries from frontmatter ``sources`` when the key is present;
      2. entries parsed from an exact ``# Citations`` section when ``sources``
         is absent;
      3. an empty tuple otherwise.

    Frontmatter entries that are not mappings or lack a non-empty ``resource``
    are omitted so malformed optional metadata cannot crash later retrieval.
    A legacy ``# Citations`` section is an unordered Markdown list whose
    ``-``/``*`` items become ``{"title", "resource"}`` (Markdown links) or
    ``{"resource"}`` (plain) mappings. Indented/nested items, non-list prose,
    empty items, and empty-resource links are ignored. Source order follows
    body order and duplicate resources are preserved.
    """
    frontmatter = concept.frontmatter
    if "sources" in frontmatter:
        return _valid_frontmatter_sources(frontmatter["sources"])
    citations = _citations_section(concept)
    if citations is None:
        return ()
    return _parse_citations(citations)
```

The citation parser matches only zero-indent `-`/`*` bullets, so indented nested list content and prose lines never become sources; a Markdown link with an empty resource (e.g. `[Broken]()`) is dropped while plain items and complete links become verbatim resources.

- [ ] **Step 4: Run the helper and round-trip tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_compatibility.py && uv run ruff check src/selayer/okf/compatibility.py tests/okf/test_compatibility.py && uv run pyright src/selayer/okf/compatibility.py tests/okf/test_compatibility.py`

Expected: PASS — frontmatter entries win and malformed ones are omitted, the malformed container yields `()`, the citation fallback parses links/plain items and ignores prose/nested/empty-resource items, order and duplicates are preserved, neither frontmatter nor sections is mutated, legacy `timestamp`/`# Citations` survive load → write → load, and there are zero lint/type errors.

- [ ] **Step 5: Commit the effective_sources slice**

```bash
git add src/selayer/okf/compatibility.py tests/okf/test_compatibility.py
git commit -m "feat(okf): add effective_sources with v0.1 citations fallback"
```

---

### Task 6: Surface effective sources in retrieval

**Files:**

- Modify: `src/selayer/okf/bundle.py:16` (add a `from .compatibility import effective_sources` import after the `computation` import), `src/selayer/okf/bundle.py:131-133` (`_sources`), `src/selayer/okf/bundle.py:155-157` (`_context_item` header and its `sources = _sources(...)` line)
- Test: `tests/okf/test_retrieval.py`

**Interfaces:**

- Consumes: Task 5's `effective_sources(concept: OkfConcept) -> tuple[Mapping[str, object], ...]` from `selayer.okf.compatibility`.
- Produces: `_sources(concept: OkfConcept) -> tuple[str, ...]` reading resources from `effective_sources(concept)`, and `_context_item(concept, today)` calling it.
- Preserves: `_render_context(concept: OkfConcept, sources: tuple[str, ...]) -> str` and its resource-only `## Sources` output, plus the public `ContextItem` shape (no new fields this plan).

- [ ] **Step 1: Write the failing retrieval-integration tests**

Append to `tests/okf/test_retrieval.py`:

```python
def test_retrieval_surfaces_legacy_citations_as_sources(tmp_path: Path) -> None:
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
    assert "## Sources" in item.content
    assert "- https://example.com/policy" in item.content
    assert "- urn:warehouse:margin" in item.content


def test_retrieval_omits_malformed_frontmatter_sources_in_lenient_mode(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\n"
        "selayer_id: metric.margin\n"
        "sources:\n"
        "  - resource: https://example.com/policy\n"
        "  - broken: entry\n"
        "  - not-a-mapping",
    )

    bundle = OkfBundle.load(tmp_path, strict=False)
    item = bundle.context_for(["metric.margin"], include_linked=False).items[0]

    assert item.sources == ("https://example.com/policy",)


def test_retrieval_without_sources_or_citations_has_no_sources_section(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "metrics/margin.md",
        "type: Selayer Metric\nselayer_id: metric.margin",
        "\n# Definition\n\nNo citations.\n",
    )

    item = OkfBundle.load(tmp_path).context_for(
        ["metric.margin"], include_linked=False
    ).items[0]

    assert item.sources == ()
    assert "## Sources" not in item.content
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/okf/test_retrieval.py -k "surfaces_legacy_citations or omits_malformed_frontmatter_sources or without_sources_or_citations"`

Expected: FAIL — `_sources()` still reads raw frontmatter, so the legacy-citations concept reports `item.sources == ()` (no `## Sources` section), the lenient malformed-entries case raises because `_sources()` indexes into a non-mapping entry, and the no-source case is unaffected.

- [ ] **Step 3: Route retrieval through effective_sources**

In `src/selayer/okf/bundle.py`, add this import alongside the existing relative imports (after the `from .computation import attested_computation` line):

```python
from .compatibility import effective_sources
```

Replace the `_sources` function with:

```python
def _sources(concept: OkfConcept) -> tuple[str, ...]:
    sources: list[str] = []
    for source in effective_sources(concept):
        resource = source.get("resource")
        if isinstance(resource, str) and resource:
            sources.append(resource)
    return tuple(sources)
```

Inside `_context_item`, change the single call from `_sources(frontmatter)` to `_sources(concept)` so the function body begins:

```python
def _context_item(concept: OkfConcept, today: date) -> ContextItem:
    frontmatter = concept.frontmatter
    sources = _sources(concept)
    semantic_id = frontmatter.get("selayer_id")
```

Do not change `_render_context()` or the `ContextItem(...)` keyword arguments; only the source of the `sources` tuple changes.

- [ ] **Step 4: Run retrieval tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_retrieval.py && uv run ruff check src/selayer/okf/bundle.py tests/okf/test_retrieval.py && uv run pyright src/selayer/okf/bundle.py tests/okf/test_retrieval.py`

Expected: PASS — legacy `# Citations` surface as `item.sources` and rendered `## Sources`, malformed frontmatter entries are omitted under `strict=False`, a concept without sources or citations has no sources section, existing resource-only rendering and `ContextItem` shape are unchanged, and there are zero lint/type errors.

- [ ] **Step 5: Commit the retrieval-integration slice**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_retrieval.py
git commit -m "feat(okf): surface effective sources in retrieval"
```

---

## Final full verification

- [ ] **Step 1: Run all OKF tests**

Run: `uv run pytest -q tests/okf`

Expected: PASS with zero failures, including strict/lenient loading, direct strict/lenient validation for every optional family, freshness hardening, compatibility precedence and citation parsing, load → write → load round trips, and effective-source retrieval.

- [ ] **Step 2: Run the full project test suite**

Run: `uv run pytest -q`

Expected: PASS with zero failures and no planner/compiler/catalog regressions.

- [ ] **Step 3: Run repository-wide lint and type checks**

Run: `uv run ruff check . && uv run pyright`

Expected: both commands exit 0 with no findings.

- [ ] **Step 4: Check patch hygiene and dependency stability**

Run: `git diff --check HEAD~6..HEAD && git diff --exit-code HEAD~6..HEAD -- pyproject.toml uv.lock`

Expected: both commands exit 0; there is no whitespace error and no dependency-file change (no runtime dependency was added).

- [ ] **Step 5: Confirm the six-task file boundary**

Run: `git diff --name-only HEAD~6..HEAD | sort`

Expected output:

```text
src/selayer/okf/bundle.py
src/selayer/okf/compatibility.py
src/selayer/okf/validation.py
tests/okf/test_compatibility.py
tests/okf/test_retrieval.py
tests/okf/test_validation.py
```

- [ ] **Step 6: Confirm the implementation branch is clean**

Run: `git status --short`

Expected: no output.
