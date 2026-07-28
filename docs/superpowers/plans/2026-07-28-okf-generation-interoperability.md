# OKF Generation Interoperability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate generic, interoperable OKF v0.2 concepts while preserving validation, synchronization, and conflict-safety compatibility with existing selayer-generated bundles.

**Architecture:** Keep interoperability behavior inside the existing `selayer.okf` generation, document, validation, and bundle-sync seams. Generation writes canonical types, catalog-backed resources, bundle-absolute descriptive indexes, and a top-level fingerprint; document and sync helpers dual-read the new and legacy fingerprint locations so untouched legacy files migrate through the existing controlled merge while edited files remain conflicts.

**Tech Stack:** Python 3.13, standard-library `hashlib`, `hmac`, `json`, and `urllib.parse`, PyYAML, pytest, Ruff, Pyright, uv

## Global Constraints

- Preserve deterministic output and synchronization behavior.
- Generated output writes only canonical names; successful sync naturally upgrades legacy generated documents.
- Unknown producer-defined types remain accepted when they do not claim a `selayer_id` binding.
- Consumers continue accepting both absolute and relative authored links.
- Every generated link begins with `/` and is relative to the bundle root.
- Root and per-kind indexes both use the same absolute target path.
- Append `- {description}` only when frontmatter contains a non-empty string `description`; omit the separator when no description exists.
- Preserve existing deterministic kind and concept ordering.
- Do not change `include_descriptive=False`; indexes use descriptions only when generation already produced them.
- Fingerprint canonicalization excludes both top-level `selayer_fingerprint` and legacy `generated.fingerprint`.
- If both fingerprint fields exist, the top-level field is authoritative; a malformed authoritative new field is a conflict and must not fall back to the legacy field.
- `selayer_fingerprint`, when present, must be a lowercase 64-character hexadecimal SHA-256 string.
- Legacy `generated.fingerprint` remains accepted and validated during the compatibility window.
- Both fingerprint fields may coexist for reading, with the new field authoritative.
- A successful sync of an untouched legacy generated document removes `generated.fingerprint` and writes `selayer_fingerprint`.
- Use `urllib.parse.quote(column, safe="")`; add no dependency.
- Source paths are emitted verbatim, are not required to exist, and are not converted to absolute filesystem paths.
- Generate no `resource` for facts, measures, metrics, or relationships.
- Resource construction performs no I/O and cannot fail for a catalog that has already passed validation.
- Authored unknown fields remain preserved during parse/render and controlled synchronization.
- Legacy documents with curated edits remain conflicts and are not overwritten.
- Dry-run and real sync report the same create, update, and conflict decisions.
- Generated canonical type, resource, description, links, and new fingerprint participate in deterministic generated output.
- Invalid new or legacy fingerprints remain errors.
- Do not add a central type registry, rewrite arbitrary authored concept types, resolve physical resources, or change catalog authority, planning, or compilation behavior.
- Add no dependencies.

---

## File Responsibility Map

- `src/selayer/okf/generation.py` — own canonical generated type names, physical-resource construction, new fingerprint writes, and deterministic root/per-kind index rendering.
- `src/selayer/okf/document.py` — own generator-controlled frontmatter keys, canonical fingerprint input, stored-fingerprint precedence, and byte-preserving controlled merge behavior.
- `src/selayer/okf/validation.py` — own canonical/legacy `selayer_id` type compatibility and validation of both fingerprint locations.
- `src/selayer/okf/bundle.py` — own synchronization baseline selection, conflict classification, legacy auto-upgrade, and dry-run parity.
- `tests/okf/test_generation.py` — golden coverage for all six canonical types, resources, absolute/descriptive indexes, deterministic ordering, and no-data-access generation.
- `tests/okf/test_document.py` — unit coverage for fingerprint canonicalization, precedence, malformed authoritative values, coexistence, and extension-preserving round trips.
- `tests/okf/test_validation.py` — compatibility coverage for all six legacy types, canonical types, semantic mismatches, and both fingerprint schemas.
- `tests/okf/test_sync.py` — regression coverage for untouched legacy migration, curated legacy conflicts, malformed/mismatched new fingerprints, controlled resource insertion, and dry-run parity.
- `tests/okf/test_cli.py` — keep the CLI JSON contract stable while snapshotting the new generated document and index shape.
- `tests/okf/test_documentation.py` — executable assertions for the public interoperability promises added to the README.
- `README.md` — document generic generated types, physical resources, absolute indexes, the private top-level fingerprint, and automatic legacy migration.

---

### Task 1: Canonical Generated Types with Legacy Binding Recognition

**Files:**

- Modify: `src/selayer/okf/generation.py:14-31`
- Modify: `src/selayer/okf/validation.py:14-27,158-198`
- Modify: `tests/okf/test_generation.py:18-115`
- Modify: `tests/okf/test_validation.py:276-340`

**Interfaces:**

- Consumes: `concepts_from_layer(layer: SemanticLayer, *, generated_at: datetime | None = None, include_descriptive: bool = True) -> Mapping[str, OkfConcept]`
- Consumes: `validate_concept(concept: OkfConcept, layer: SemanticLayer | None = None) -> tuple[OkfIssue, ...]`
- Produces: `_KIND_TYPES: dict[str, str]` containing the six canonical generated type names.
- Produces: `_LEGACY_KIND_TYPES: dict[str, str]` containing the six accepted legacy type names in validation only.
- Produces: no new public API; `selayer_id` accepts exactly the canonical or matching legacy type for its semantic prefix.

- [ ] **Step 1: Write failing canonical-generation and dual-recognition tests**

In `tests/okf/test_generation.py`, replace the frontmatter assertion in `test_generate_metric_concept` with this exact expected mapping. The fingerprint remains in the legacy location in this task; Task 3 relocates it.

```python
    assert concept.frontmatter == {
        "type": "Metric",
        "title": "Gross margin",
        "description": "Gross margin ratio",
        "selayer_id": "metric.gross_margin",
        "generated": {
            "by": "process:selayer-okf",
            "at": "2026-07-27T12:00:00Z",
            "fingerprint": "381606f10532a7bc4e9e3511b7b3e57e5a3261bbb86a2e47a5c6de4119cf5344",
        },
        "status": "stable",
    }
```

Add this golden test after `test_generation_without_timestamp_is_deterministic`:

```python
def test_projection_uses_all_six_canonical_concept_types(
    ecommerce_layer: SemanticLayer,
) -> None:
    bundle = OkfBundle.from_layer(ecommerce_layer)

    assert {
        semantic_id: bundle.concepts[concept_id].frontmatter["type"]
        for semantic_id, concept_id in {
            "source.orders": "sources/orders",
            "dimension.order_date": "dimensions/order_date",
            "fact.item_cost": "facts/item_cost",
            "measure.total_item_cost": "measures/total_item_cost",
            "metric.gross_margin": "metrics/gross_margin",
            "relationship.product_order_items": (
                "relationships/product_order_items"
            ),
        }.items()
    } == {
        "source.orders": "Data Source",
        "dimension.order_date": "Dimension",
        "fact.item_cost": "Fact",
        "measure.total_item_cost": "Measure",
        "metric.gross_margin": "Metric",
        "relationship.product_order_items": "Relationship",
    }
```

Add these compatibility tests after `test_selayer_id_is_resolved_and_kind_checked` in `tests/okf/test_validation.py`:

```python
@pytest.mark.parametrize(
    ("semantic_id", "concept_type"),
    [
        ("source.orders", "Data Source"),
        ("source.orders", "Selayer Data Source"),
        ("dimension.order_date", "Dimension"),
        ("dimension.order_date", "Selayer Dimension"),
        ("fact.item_cost", "Fact"),
        ("fact.item_cost", "Selayer Fact"),
        ("measure.total_item_cost", "Measure"),
        ("measure.total_item_cost", "Selayer Measure"),
        ("metric.gross_margin", "Metric"),
        ("metric.gross_margin", "Selayer Metric"),
        ("relationship.product_order_items", "Relationship"),
        (
            "relationship.product_order_items",
            "Selayer Relationship",
        ),
    ],
)
def test_selayer_id_accepts_canonical_and_matching_legacy_types(
    tmp_path: Path,
    valid_catalog_path: Path,
    semantic_id: str,
    concept_type: str,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    _write_concept(
        knowledge,
        f"type: {concept_type}\nselayer_id: {semantic_id}",
    )

    bundle = OkfBundle.load(knowledge, layer=layer)

    assert bundle.concepts["concept"].frontmatter["type"] == concept_type


@pytest.mark.parametrize("concept_type", ["Dimension", "Selayer Dimension"])
def test_selayer_id_rejects_mismatched_canonical_and_legacy_types(
    tmp_path: Path,
    valid_catalog_path: Path,
    concept_type: str,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    _write_concept(
        knowledge,
        f"type: {concept_type}\nselayer_id: metric.gross_margin",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(knowledge, layer=layer)

    assert {issue.path for issue in caught.value.issues} == {
        "concept.md.frontmatter.type"
    }
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py::test_generate_metric_concept \
  tests/okf/test_generation.py::test_projection_uses_all_six_canonical_concept_types \
  tests/okf/test_validation.py::test_selayer_id_accepts_canonical_and_matching_legacy_types \
  tests/okf/test_validation.py::test_selayer_id_rejects_mismatched_canonical_and_legacy_types \
  -q
```

Expected: FAIL because generation still emits `Selayer …` names, canonical `selayer_id` bindings are rejected, and the canonical metric fingerprint differs from the legacy-type fingerprint.

- [ ] **Step 3: Switch generated names and accept matching legacy names**

Replace `_KIND_TYPES` in `src/selayer/okf/generation.py` with:

```python
_KIND_TYPES = {
    "source": "Data Source",
    "dimension": "Dimension",
    "fact": "Fact",
    "measure": "Measure",
    "metric": "Metric",
    "relationship": "Relationship",
}
```

Replace the type mapping in `src/selayer/okf/validation.py` with both exact mappings:

```python
_KIND_TYPES = {
    "source": "Data Source",
    "dimension": "Dimension",
    "fact": "Fact",
    "measure": "Measure",
    "metric": "Metric",
    "relationship": "Relationship",
}
_LEGACY_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}
```

Replace `_validate_selayer_id` with:

```python
def _validate_selayer_id(
    concept: OkfConcept,
    value: object,
    layer: SemanticLayer | None,
) -> list[OkfIssue]:
    if not _is_nonempty_string(value):
        return [_issue(concept, "selayer_id", "selayer_id must be a non-empty string")]
    semantic_id = value
    assert isinstance(semantic_id, str)
    if _SELAYER_ID.fullmatch(semantic_id) is None:
        return [
            _issue(
                concept,
                "selayer_id",
                "selayer_id must use a canonical semantic kind and a local name "
                "matching [a-z][a-z0-9_]*",
            )
        ]
    prefix = semantic_id.partition(".")[0]
    canonical_type = _KIND_TYPES[prefix]
    legacy_type = _LEGACY_KIND_TYPES[prefix]
    issues: list[OkfIssue] = []
    if concept.frontmatter.get("type") not in (canonical_type, legacy_type):
        issues.append(
            _issue(
                concept,
                "type",
                f"type must be '{canonical_type}' or '{legacy_type}' "
                f"for selayer_id '{semantic_id}'",
            )
        )
    if layer is not None:
        try:
            layer.resolve(semantic_id)
        except KeyError:
            issues.append(
                _issue(
                    concept,
                    "selayer_id",
                    f"unknown semantic identifier '{semantic_id}'",
                )
            )
    return issues
```

Do not add type validation for documents without `selayer_id`; their producer-defined `type` remains accepted.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again.

Expected: PASS for all canonical generation cases, all twelve canonical/legacy bindings, and both mismatch cases.

- [ ] **Step 5: Run the complete OKF type/validation regression set**

Run:

```bash
uv run pytest tests/okf/test_generation.py tests/okf/test_validation.py -q
```

Expected: PASS with unknown producer-defined types still accepted and every existing semantic binding check intact.

- [ ] **Step 6: Commit the independently reviewable type change**

```bash
git add \
  src/selayer/okf/generation.py \
  src/selayer/okf/validation.py \
  tests/okf/test_generation.py \
  tests/okf/test_validation.py
git commit -m "feat(okf): generate canonical concept types"
```

---

### Task 2: Dual-Location Fingerprint Primitives and Validation

**Files:**

- Modify: `src/selayer/okf/document.py:15-29,160-184,386-394`
- Modify: `src/selayer/okf/validation.py:25-27,91-106,201-258`
- Modify: `tests/okf/test_document.py:1-16,201-214`
- Modify: `tests/okf/test_validation.py:136-172`

**Interfaces:**

- Consumes: `generated_fingerprint(frontmatter: Mapping[str, Any], catalog_definition: str) -> str`
- Produces: `stored_generated_fingerprint(frontmatter: Mapping[str, Any]) -> str | None`; returns the authoritative digest normalized to lowercase, returns `None` when neither location exists, and raises `OkfDocumentError` for a malformed authoritative new value or malformed legacy value.
- Produces: `CONTROLLED_FRONTMATTER_KEYS` containing top-level `selayer_fingerprint` so controlled merge can insert or remove it.
- Produces: validation of top-level `selayer_fingerprint` as lowercase hexadecimal while preserving the compatibility validation of legacy `generated.fingerprint`.

- [ ] **Step 1: Write failing canonicalization, precedence, coexistence, and validation tests**

Replace the document import in `tests/okf/test_document.py` with:

```python
from selayer.okf.document import (
    OkfDocumentError,
    generated_fingerprint,
    parse_concept,
    render_concept,
    stored_generated_fingerprint,
)
```

Append these tests to `tests/okf/test_document.py`:

```python
def test_generated_fingerprint_excludes_new_and_legacy_digest_locations() -> None:
    definition = "Semantic ID: `metric.gross_margin`"
    base = {
        "type": "Metric",
        "title": "Gross margin",
        "selayer_id": "metric.gross_margin",
        "generated": {"by": "process:selayer-okf"},
    }
    legacy = {
        **base,
        "generated": {
            "by": "process:selayer-okf",
            "fingerprint": "a" * 64,
        },
    }
    current = {**base, "selayer_fingerprint": "b" * 64}
    coexist = {
        **current,
        "generated": {
            "by": "process:selayer-okf",
            "fingerprint": "c" * 64,
        },
    }

    expected = generated_fingerprint(base, definition)

    assert generated_fingerprint(legacy, definition) == expected
    assert generated_fingerprint(current, definition) == expected
    assert generated_fingerprint(coexist, definition) == expected


def test_stored_generated_fingerprint_prefers_new_field_when_both_exist() -> None:
    assert stored_generated_fingerprint(
        {
            "selayer_fingerprint": "a" * 64,
            "generated": {
                "by": "process:selayer-okf",
                "fingerprint": "b" * 64,
            },
        }
    ) == "a" * 64


def test_stored_generated_fingerprint_reads_and_normalizes_legacy_field() -> None:
    assert stored_generated_fingerprint(
        {
            "generated": {
                "by": "process:selayer-okf",
                "fingerprint": "A" * 64,
            }
        }
    ) == "a" * 64
    assert stored_generated_fingerprint({"type": "Metric"}) is None


def test_malformed_authoritative_new_fingerprint_does_not_fall_back() -> None:
    with pytest.raises(
        OkfDocumentError,
        match="selayer_fingerprint must be a lowercase",
    ):
        stored_generated_fingerprint(
            {
                "selayer_fingerprint": "not-a-digest",
                "generated": {
                    "by": "process:selayer-okf",
                    "fingerprint": "b" * 64,
                },
            }
        )
```

Add this top-level fingerprint validation test to `tests/okf/test_validation.py`:

```python
@pytest.mark.parametrize(
    "fingerprint",
    ["short", "g" * 64, "A" * 64, 123],
)
def test_selayer_fingerprint_must_be_lowercase_sha256_when_present(
    tmp_path: Path,
    fingerprint: object,
) -> None:
    _write_concept(
        tmp_path,
        f"type: Metric\nselayer_fingerprint: {fingerprint}",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert "concept.md.frontmatter.selayer_fingerprint" in {
        issue.path for issue in caught.value.issues
    }
```

Replace `test_valid_v02_optional_families_are_accepted` with the coexistence case below. This retains legacy validation while proving that both fields may be read together.

```python
def test_valid_v02_optional_families_are_accepted(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric\n"
        "status: stable\n"
        "stale_after: 2026-09-23\n"
        f"selayer_fingerprint: {'b' * 64}\n"
        "generated: {by: process:selayer-okf, at: 2026-07-27T14:00:00Z, "
        f"fingerprint: {'a' * 64}}}\n"
        "verified: {by: human:owner, at: 2026-07-27T15:00:00Z}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n",
    )

    assert OkfBundle.load(tmp_path).concepts["concept"]
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_document.py::test_generated_fingerprint_excludes_new_and_legacy_digest_locations \
  tests/okf/test_document.py::test_stored_generated_fingerprint_prefers_new_field_when_both_exist \
  tests/okf/test_document.py::test_stored_generated_fingerprint_reads_and_normalizes_legacy_field \
  tests/okf/test_document.py::test_malformed_authoritative_new_fingerprint_does_not_fall_back \
  tests/okf/test_validation.py::test_selayer_fingerprint_must_be_lowercase_sha256_when_present \
  tests/okf/test_validation.py::test_valid_v02_optional_families_are_accepted \
  -q
```

Expected: collection FAIL because `stored_generated_fingerprint` does not exist; after importing only the available symbols, the canonicalization and uppercase-new-field cases also FAIL because the top-level field is neither excluded nor validated.

- [ ] **Step 3: Implement canonicalization and authoritative dual-read semantics**

Replace `CONTROLLED_FRONTMATTER_KEYS` in `src/selayer/okf/document.py` with:

```python
CONTROLLED_FRONTMATTER_KEYS = (
    "type",
    "title",
    "description",
    "selayer_id",
    "generated",
    "selayer_fingerprint",
)
```

Add these regexes after `_YAML_SET_TAG`:

```python
_LOWER_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_LEGACY_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
```

Replace `generated_fingerprint` and add `stored_generated_fingerprint` immediately after it:

```python
def generated_fingerprint(
    frontmatter: Mapping[str, Any], catalog_definition: str
) -> str:
    """Hash the canonical generated projection, excluding digest metadata."""
    controlled = {
        key: _thaw(frontmatter[key])
        for key in CONTROLLED_FRONTMATTER_KEYS
        if key in frontmatter
    }
    controlled.pop("selayer_fingerprint", None)
    generated = controlled.get("generated")
    if isinstance(generated, dict):
        generated = dict(generated)
        generated.pop("fingerprint", None)
        controlled["generated"] = generated
    canonical = json.dumps(
        {
            "catalog_definition": catalog_definition,
            "frontmatter": controlled,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stored_generated_fingerprint(frontmatter: Mapping[str, Any]) -> str | None:
    """Read the authoritative selayer digest with legacy fallback."""
    if "selayer_fingerprint" in frontmatter:
        value = frontmatter["selayer_fingerprint"]
        if (
            not isinstance(value, str)
            or _LOWER_SHA256_HEX.fullmatch(value) is None
        ):
            raise OkfDocumentError(
                "selayer_fingerprint must be a lowercase 64-character "
                "SHA-256 hex digest"
            )
        return value

    generated = frontmatter.get("generated")
    if not isinstance(generated, Mapping) or "fingerprint" not in generated:
        return None
    legacy = generated["fingerprint"]
    if (
        not isinstance(legacy, str)
        or _LEGACY_SHA256_HEX.fullmatch(legacy) is None
    ):
        raise OkfDocumentError(
            "generated.fingerprint must be a 64-character SHA-256 hex digest"
        )
    return legacy.lower()
```

Add `stored_generated_fingerprint` to `document.py`'s `__all__`:

```python
__all__ = [
    "CONTROLLED_FRONTMATTER_KEYS",
    "OkfControlledMergeError",
    "OkfDocumentError",
    "generated_fingerprint",
    "merge_generated_concept_text",
    "parse_concept",
    "render_concept",
    "split_sections",
    "stored_generated_fingerprint",
]
```

In `src/selayer/okf/validation.py`, add a lowercase regex beside the existing legacy-compatible regex:

```python
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
_LOWER_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
```

Add this block in `validate_concept` immediately after generated-family validation:

```python
    if "generated" in frontmatter:
        issues.extend(_validate_generated(concept, frontmatter["generated"]))
    if "selayer_fingerprint" in frontmatter:
        fingerprint = frontmatter["selayer_fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or _LOWER_SHA256_HEX.fullmatch(fingerprint) is None
        ):
            issues.append(
                _issue(
                    concept,
                    "selayer_fingerprint",
                    "selayer_fingerprint must be a lowercase 64-character "
                    "SHA-256 hex digest",
                )
            )
```

Keep `_validate_generated` unchanged so legacy fingerprints retain their compatibility-window validation.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: PASS, including precedence of valid top-level metadata over a different valid legacy digest and rejection of malformed authoritative new metadata without fallback.

- [ ] **Step 5: Run document and validation regression tests**

Run:

```bash
uv run pytest tests/okf/test_document.py tests/okf/test_validation.py -q
```

Expected: PASS, including unknown type/extension preservation and parse-render-parse round trips.

- [ ] **Step 6: Commit the independently reviewable fingerprint primitives**

```bash
git add \
  src/selayer/okf/document.py \
  src/selayer/okf/validation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py
git commit -m "feat(okf): support dual fingerprint locations"
```

---

### Task 3: New-Write Fingerprints and Conflict-Safe Legacy Sync Migration

**Files:**

- Modify: `src/selayer/okf/generation.py:120-163`
- Modify: `src/selayer/okf/bundle.py:1-25,91-95,320-454`
- Modify: `tests/okf/test_generation.py:18-68`
- Modify: `tests/okf/test_sync.py:1-16,37-135,218-268`

**Interfaces:**

- Consumes: `generated_fingerprint(frontmatter: Mapping[str, Any], catalog_definition: str) -> str`
- Consumes: `stored_generated_fingerprint(frontmatter: Mapping[str, Any]) -> str | None`
- Consumes: `merge_generated_concept_text(existing_text: str, generated: OkfConcept, *, definition_changed: bool) -> str`
- Produces: generated frontmatter containing `generated: {by, at?}` and top-level `selayer_fingerprint`, never `generated.fingerprint`.
- Produces: `OkfBundle.sync(self, path: str | Path, *, dry_run: bool = False) -> SyncReport` with new-first/legacy-second baseline selection, untouched legacy auto-upgrade, byte-preserving conflicts, and dry-run/real decision parity.
- Produces for later test tasks: `_legacy_generated_text(concept: OkfConcept, *, legacy_type: str) -> str` in `tests/okf/test_sync.py`.

- [ ] **Step 1: Write failing new-write, migration, and conflict-preservation tests**

In `tests/okf/test_generation.py`, replace the expected metric frontmatter with:

```python
    assert concept.frontmatter == {
        "type": "Metric",
        "title": "Gross margin",
        "description": "Gross margin ratio",
        "selayer_id": "metric.gross_margin",
        "generated": {
            "by": "process:selayer-okf",
            "at": "2026-07-27T12:00:00Z",
        },
        "selayer_fingerprint": (
            "381606f10532a7bc4e9e3511b7b3e57e5a3261bbb86a2e47a5c6de4119cf5344"
        ),
        "status": "stable",
    }
```

Replace `test_generation_without_timestamp_is_deterministic` with:

```python
def test_generation_without_timestamp_is_deterministic(
    ecommerce_layer: SemanticLayer,
) -> None:
    first = OkfBundle.from_layer(ecommerce_layer)
    second = OkfBundle.from_layer(ecommerce_layer)

    assert first.concepts == second.concepts
    frontmatter = first.concepts["metrics/gross_margin"].frontmatter
    assert frontmatter["generated"] == {"by": "process:selayer-okf"}
    assert re.fullmatch(r"[0-9a-f]{64}", frontmatter["selayer_fingerprint"])
```

In `tests/okf/test_sync.py`, add these imports:

```python
from selayer.okf.document import generated_fingerprint, render_concept
from selayer.okf.model import OkfConcept
```

Add this helper after the fixtures:

```python
def _legacy_generated_text(
    concept: OkfConcept,
    *,
    legacy_type: str,
) -> str:
    frontmatter = dict(concept.frontmatter)
    generated = dict(frontmatter["generated"])
    generated.pop("fingerprint", None)
    frontmatter["generated"] = generated
    frontmatter.pop("selayer_fingerprint", None)
    frontmatter.pop("resource", None)
    frontmatter["type"] = legacy_type
    definition = next(
        section.content
        for section in concept.sections
        if section.title == "Catalog Definition"
    )
    generated["fingerprint"] = generated_fingerprint(frontmatter, definition)
    legacy = OkfConcept.create(
        concept_id=concept.concept_id,
        relative_path=concept.relative_path,
        frontmatter=frontmatter,
        preamble=concept.preamble,
        sections=concept.sections,
        links=concept.links,
    )
    return render_concept(legacy)
```

Add these tests after `test_sync_preserves_curated_sections_and_extensions`:

```python
def test_sync_auto_upgrades_untouched_legacy_document_with_dry_run_parity(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    bundle = OkfBundle.from_layer(ecommerce_layer)
    bundle.write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        _legacy_generated_text(
            bundle.concepts["metrics/gross_margin"],
            legacy_type="Selayer Metric",
        ),
        encoding="utf-8",
    )
    legacy_bytes = metric_path.read_bytes()

    dry_run = bundle.sync(destination, dry_run=True)

    assert dry_run.written == ("metrics/gross_margin.md",)
    assert dry_run.conflicts == ()
    assert metric_path.read_bytes() == legacy_bytes

    real = bundle.sync(destination)
    upgraded = OkfBundle.load(destination, layer=ecommerce_layer).concepts[
        "metrics/gross_margin"
    ]

    assert real == dry_run
    assert upgraded.frontmatter["type"] == "Metric"
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        upgraded.frontmatter["selayer_fingerprint"],
    )
    assert "fingerprint" not in upgraded.frontmatter["generated"]


def test_sync_preserves_curated_edit_in_legacy_generated_document(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    bundle = OkfBundle.from_layer(ecommerce_layer)
    bundle.write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    edited = _legacy_generated_text(
        bundle.concepts["metrics/gross_margin"],
        legacy_type="Selayer Metric",
    ).replace(
        "description: Gross margin ratio",
        "description: Finance-owned wording",
    )
    metric_path.write_text(edited, encoding="utf-8")
    edited_bytes = metric_path.read_bytes()

    dry_run = bundle.sync(destination, dry_run=True)
    real = bundle.sync(destination)

    assert dry_run == real
    assert real.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_bytes() == edited_bytes


def test_sync_rejects_malformed_authoritative_new_field_with_valid_legacy_fallback(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    bundle = OkfBundle.from_layer(ecommerce_layer)
    bundle.write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    coexist = _legacy_generated_text(
        bundle.concepts["metrics/gross_margin"],
        legacy_type="Selayer Metric",
    ).replace(
        "selayer_id: metric.gross_margin\n",
        "selayer_id: metric.gross_margin\n"
        "selayer_fingerprint: not-a-digest\n",
    )
    metric_path.write_text(coexist, encoding="utf-8")
    unsafe = metric_path.read_bytes()

    report = bundle.sync(destination)

    assert report.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_bytes() == unsafe
```

Replace `test_sync_conflicts_on_an_unprovable_generated_baseline` with the new-field version:

```python
@pytest.mark.parametrize(
    "fingerprint_replacement",
    [
        "",
        "selayer_fingerprint: not-a-digest\n",
        "selayer_fingerprint: " + "0" * 64 + "\n",
    ],
    ids=["absent", "invalid", "mismatched"],
)
def test_sync_conflicts_on_an_unprovable_generated_baseline(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    fingerprint_replacement: str,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    unsafe = re.sub(
        r"^selayer_fingerprint: [0-9a-f]{64}\n",
        fingerprint_replacement,
        metric_path.read_text(encoding="utf-8"),
        count=1,
        flags=re.MULTILINE,
    ).encode()
    metric_path.write_bytes(unsafe)

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert report.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_bytes() == unsafe
```

In `test_controlled_merge_preserves_all_curated_crlf_bytes`, replace the two fingerprint reads with:

```python
    original_fingerprint = original_bundle.concepts[
        "metrics/gross_margin"
    ].frontmatter["selayer_fingerprint"]
    changed_fingerprint = changed_bundle.concepts[
        "metrics/gross_margin"
    ].frontmatter["selayer_fingerprint"]
```

Replace the expected fingerprint text patch in the same test with:

```python
    expected = curated.replace(
        f"selayer_fingerprint: {original_fingerprint}",
        f"selayer_fingerprint: {changed_fingerprint}",
    ).replace(
        original_definition.replace("\n", "\r\n"),
        changed_definition.replace("\n", "\r\n"),
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py::test_generate_metric_concept \
  tests/okf/test_generation.py::test_generation_without_timestamp_is_deterministic \
  tests/okf/test_sync.py::test_sync_auto_upgrades_untouched_legacy_document_with_dry_run_parity \
  tests/okf/test_sync.py::test_sync_preserves_curated_edit_in_legacy_generated_document \
  tests/okf/test_sync.py::test_sync_rejects_malformed_authoritative_new_field_with_valid_legacy_fallback \
  tests/okf/test_sync.py::test_sync_conflicts_on_an_unprovable_generated_baseline \
  tests/okf/test_sync.py::test_controlled_merge_preserves_all_curated_crlf_bytes \
  -q
```

Expected: FAIL because generation still nests the fingerprint, sync still requires `generated.fingerprint`, and untouched legacy documents are not rewritten to the new metadata shape.

- [ ] **Step 3: Write only the new fingerprint location during generation**

In `src/selayer/okf/generation.py`, replace the generated-metadata portion of `concepts_from_layer` with:

```python
        definition = catalog_definition(semantic_id, value)
        generated = _generated_metadata(generated_at)
        frontmatter.update(
            {
                "selayer_id": semantic_id,
                "generated": generated,
            }
        )
        frontmatter["selayer_fingerprint"] = generated_fingerprint(
            frontmatter,
            definition,
        )
        frontmatter["status"] = "stable"
```

There must be no assignment to `generated["fingerprint"]` anywhere in generation.

- [ ] **Step 4: Make sync use authoritative new-first/legacy-second reads**

In `src/selayer/okf/bundle.py`, remove `import re`, remove `_SHA256_HEX`, and add `stored_generated_fingerprint` to the document imports:

```python
from .document import (
    OkfControlledMergeError,
    OkfDocumentError,
    generated_fingerprint,
    merge_generated_concept_text,
    parse_concept,
    render_concept,
    stored_generated_fingerprint,
)
```

Replace the existing fingerprint extraction/validation/baseline block inside `OkfBundle.sync` with:

```python
            try:
                fingerprint = stored_generated_fingerprint(existing.frontmatter)
            except OkfDocumentError:
                conflicts.append(relative)
                continue
            if fingerprint is None:
                conflicts.append(relative)
                continue
            try:
                baseline_fingerprint = generated_fingerprint(
                    existing.frontmatter,
                    existing_definition.content,
                )
            except (TypeError, ValueError):
                conflicts.append(relative)
                continue
            if not hmac.compare_digest(fingerprint, baseline_fingerprint):
                conflicts.append(relative)
                continue
```

The existing controlled merge then replaces the legacy `generated` mapping with the new mapping and inserts `selayer_fingerprint`. Because the baseline is checked before merge, curated legacy edits still classify as conflicts and retain their original bytes.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: PASS; dry-run and real sync return identical migration/conflict reports, untouched legacy metadata upgrades, curated legacy bytes remain untouched, and CRLF curated content remains byte-stable outside controlled spans.

- [ ] **Step 6: Run the complete generation/document/validation/sync regression set**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py \
  tests/okf/test_sync.py \
  -q
```

Expected: PASS, including new-field mismatches, absent fingerprints, malformed fingerprints, coexistence, auto-upgrade, curated conflict preservation, dry-run no-write behavior, and unknown-field round trips.

- [ ] **Step 7: Commit the independently reviewable synchronization migration**

```bash
git add \
  src/selayer/okf/generation.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_generation.py \
  tests/okf/test_sync.py
git commit -m "feat(okf): migrate generated fingerprints during sync"
```

---

### Task 4: Canonical Physical Resources for Sources and Dimensions

**Files:**

- Modify: `src/selayer/okf/generation.py:1-13,107-163`
- Modify: `src/selayer/okf/document.py:19-29`
- Modify: `tests/okf/test_generation.py:1-12,70-115,350-365`
- Modify: `tests/okf/test_sync.py` after the legacy migration tests from Task 3

**Interfaces:**

- Consumes: `SemanticLayer.data_sources: Mapping[str, DataSource]`
- Consumes: `DataSource.path: str`, `Dimension.source: str`, and `Dimension.column: str`
- Consumes: `_legacy_generated_text(concept: OkfConcept, *, legacy_type: str) -> str` from Task 3 tests.
- Produces: `_catalog_resource(layer: SemanticLayer, value: SemanticObject) -> str | None`.
- Produces: source resource equal to `DataSource.path` verbatim.
- Produces: dimension resource equal to `<resolved source path>#column=<quote(column, safe="")>`.
- Produces: `CONTROLLED_FRONTMATTER_KEYS` containing `resource`, so fingerprints cover it and sync inserts/updates it only after a trusted baseline check.

- [ ] **Step 1: Write failing source, dimension, encoding, exclusion, and legacy-upgrade tests**

Add these tests to `tests/okf/test_generation.py` after `test_projection_contains_every_semantic_object`:

```python
def test_generated_resources_cover_only_catalog_backed_physical_concepts(
    ecommerce_layer: SemanticLayer,
) -> None:
    bundle = OkfBundle.from_layer(ecommerce_layer)

    assert bundle.concepts["sources/products"].frontmatter["resource"] == (
        "data/products.parquet"
    )
    assert bundle.concepts["dimensions/product_category"].frontmatter[
        "resource"
    ] == "data/products.parquet#column=category"
    assert bundle.concepts["dimensions/order_date"].frontmatter["resource"] == (
        "data/orders.parquet#column=created_at"
    )
    assert {
        concept.frontmatter["selayer_id"]
        for concept in bundle.concepts.values()
        if "resource" not in concept.frontmatter
    } == {
        "fact.item_cost",
        "fact.item_revenue",
        "measure.total_item_cost",
        "measure.total_item_revenue",
        "metric.gross_margin",
        "relationship.product_order_items",
    }


def test_dimension_resource_percent_encodes_the_complete_column_value(
    ecommerce_layer: SemanticLayer,
) -> None:
    encoded = Dimension(
        name="encoded_column",
        source="products",
        column="part/size #?&=ümlaut",
        data_type="string",
    )
    layer = replace(
        ecommerce_layer,
        dimensions={
            **ecommerce_layer.dimensions,
            "encoded_column": encoded,
        },
    )

    concept = OkfBundle.from_layer(layer).concepts[
        "dimensions/encoded_column"
    ]

    assert concept.frontmatter["resource"] == (
        "data/products.parquet#column="
        "part%2Fsize%20%23%3F%26%3D%C3%BCmlaut"
    )
```

Add this migration test to `tests/okf/test_sync.py` after the legacy fingerprint migration tests:

```python
def test_sync_adds_resource_while_upgrading_an_untouched_legacy_source(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    bundle = OkfBundle.from_layer(ecommerce_layer)
    bundle.write(destination)
    source_path = destination / "sources" / "products.md"
    source_path.write_text(
        _legacy_generated_text(
            bundle.concepts["sources/products"],
            legacy_type="Selayer Data Source",
        ),
        encoding="utf-8",
    )
    legacy_bytes = source_path.read_bytes()

    dry_run = bundle.sync(destination, dry_run=True)

    assert "sources/products.md" in dry_run.written
    assert dry_run.conflicts == ()
    assert source_path.read_bytes() == legacy_bytes

    real = bundle.sync(destination)
    upgraded = OkfBundle.load(destination, layer=ecommerce_layer).concepts[
        "sources/products"
    ]

    assert real == dry_run
    assert upgraded.frontmatter["type"] == "Data Source"
    assert upgraded.frontmatter["resource"] == "data/products.parquet"
    assert "fingerprint" not in upgraded.frontmatter["generated"]
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py::test_generated_resources_cover_only_catalog_backed_physical_concepts \
  tests/okf/test_generation.py::test_dimension_resource_percent_encodes_the_complete_column_value \
  tests/okf/test_sync.py::test_sync_adds_resource_while_upgrading_an_untouched_legacy_source \
  -q
```

Expected: FAIL with missing `resource` keys; the legacy source migration cannot add a controlled resource because generation and controlled merge do not yet own that field.

- [ ] **Step 3: Implement pure catalog-resource construction**

Add the standard-library import to `src/selayer/okf/generation.py`:

```python
from urllib.parse import quote
```

Add this helper after `_generated_metadata`:

```python
def _catalog_resource(
    layer: SemanticLayer,
    value: SemanticObject,
) -> str | None:
    if isinstance(value, DataSource):
        return value.path
    if isinstance(value, Dimension):
        source = layer.data_sources[value.source]
        return f"{source.path}#column={quote(value.column, safe='')}"
    return None
```

In `concepts_from_layer`, insert resource construction after the optional description and before the catalog definition/fingerprint:

```python
        description = getattr(value, "description", "")
        if include_descriptive and isinstance(description, str) and description:
            frontmatter["description"] = description
        resource = _catalog_resource(layer, value)
        if resource is not None:
            frontmatter["resource"] = resource
        definition = catalog_definition(semantic_id, value)
```

Do not call `Path`, `exists`, `resolve`, DuckDB, Polars, or PyArrow from `_catalog_resource`.

- [ ] **Step 4: Make resources fingerprinted and controlled during sync**

Replace `CONTROLLED_FRONTMATTER_KEYS` in `src/selayer/okf/document.py` with:

```python
CONTROLLED_FRONTMATTER_KEYS = (
    "type",
    "title",
    "description",
    "resource",
    "selayer_id",
    "generated",
    "selayer_fingerprint",
)
```

This makes a hand-edited generated `resource` invalidate the stored baseline, while an untouched old document with no resource remains provable and receives the field during controlled merge.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: PASS with verbatim relative source paths, ordinary dimension fragments, UTF-8 percent encoding with no safe reserved characters, no abstract-semantic resources, and dry-run/real legacy-source migration parity.

- [ ] **Step 6: Verify resource construction never accesses source data**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py::test_generation_never_accesses_source_data \
  tests/okf/test_generation.py::test_generated_resources_cover_only_catalog_backed_physical_concepts \
  tests/okf/test_generation.py::test_dimension_resource_percent_encodes_the_complete_column_value \
  -q
```

Expected: PASS; monkeypatched DuckDB and Polars accessors are never called.

- [ ] **Step 7: Run the generation and sync regression sets**

Run:

```bash
uv run pytest tests/okf/test_generation.py tests/okf/test_sync.py -q
```

Expected: PASS, including deterministic fingerprints that now cover source/dimension resources and byte-preserving conflicts for curated edits.

- [ ] **Step 8: Commit the independently reviewable physical-resource change**

```bash
git add \
  src/selayer/okf/generation.py \
  src/selayer/okf/document.py \
  tests/okf/test_generation.py \
  tests/okf/test_sync.py
git commit -m "feat(okf): generate physical concept resources"
```

---

### Task 5: Descriptive Bundle-Absolute Index Entries

**Files:**

- Modify: `src/selayer/okf/generation.py:166-205`
- Modify: `tests/okf/test_generation.py:135-150,263-278`
- Modify: `tests/okf/test_sync.py:401-418`
- Modify: `tests/okf/test_cli.py:25-43`

**Interfaces:**

- Consumes: `index_documents(layer: SemanticLayer | None, concepts: Mapping[str, OkfConcept]) -> Mapping[PurePosixPath, str]`
- Consumes: generated `OkfConcept.frontmatter` keys `title` and optional `description`.
- Produces: `_index_entry(concept: OkfConcept) -> str` rendering `* [Title](/kind/name.md)` with `- description` only for a non-empty string.
- Produces: identical bundle-absolute targets in root and per-kind indexes, while authored relative links continue to be parsed and validated unchanged.

- [ ] **Step 1: Write failing absolute-link, description, omission, and ordering tests**

Replace `test_write_creates_progressive_indexes` in `tests/okf/test_generation.py` with:

```python
def test_write_creates_descriptive_bundle_absolute_progressive_indexes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"

    OkfBundle.from_layer(ecommerce_layer).write(destination)

    assert (destination / "metrics" / "gross_margin.md").is_file()
    root_index = (destination / "index.md").read_text(encoding="utf-8")
    assert "# Metrics" in root_index
    assert (
        "* [Gross margin](/metrics/gross_margin.md) - Gross margin ratio"
        in root_index
    )
    assert "* [Orders](/sources/orders.md)\n" in root_index
    assert "* [Orders](/sources/orders.md) - " not in root_index
    metric_index = (destination / "metrics" / "index.md").read_text(
        encoding="utf-8"
    )
    assert metric_index == (
        "# Metrics\n\n"
        "* [Gross margin](/metrics/gross_margin.md) - Gross margin ratio\n"
    )
    assert "\r\n" not in root_index
    assert (destination / "log.md").read_text(encoding="utf-8") == (
        "# Change Log\n"
    )
```

Add these tests after it:

```python
def test_index_entry_order_remains_deterministic(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"

    OkfBundle.from_layer(ecommerce_layer).write(destination)

    entries = tuple(
        line
        for line in (destination / "index.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("* [")
    )
    assert entries == (
        "* [Order items](/sources/order_items.md)",
        "* [Orders](/sources/orders.md)",
        "* [Products](/sources/products.md)",
        "* [Order date](/dimensions/order_date.md) - Order creation time",
        "* [Product category](/dimensions/product_category.md) - Product category",
        "* [Item cost](/facts/item_cost.md) - Extended product cost for one order item",
        "* [Item revenue](/facts/item_revenue.md) - Revenue recorded on one order item",
        "* [Total item cost](/measures/total_item_cost.md) - Extended item cost",
        "* [Total item revenue](/measures/total_item_revenue.md) - Item revenue",
        "* [Gross margin](/metrics/gross_margin.md) - Gross margin ratio",
        "* [Product order items](/relationships/product_order_items.md)",
    )


def test_default_generate_indexes_do_not_invent_descriptions(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"

    OkfBundle.generate(ecommerce_layer, destination)

    root_index = (destination / "index.md").read_text(encoding="utf-8")
    assert "* [Gross margin](/metrics/gross_margin.md)\n" in root_index
    assert "* [Gross margin](/metrics/gross_margin.md) - " not in root_index
```

In `test_sync_writes_new_concepts_and_regenerates_indexes`, replace the old relative-link assertion with:

```python
    assert (
        "* [Gross margin](/metrics/gross_margin.md) - Gross margin ratio"
        in (destination / "index.md").read_text(encoding="utf-8")
    )
```

Replace `test_generate_creates_a_bundle_and_reports_json` in `tests/okf/test_cli.py` with this CLI snapshot. The JSON response remains unchanged; the generated files prove that the CLI uses the same interoperable generation path.

```python
def test_generate_creates_an_interoperable_bundle_and_reports_stable_json(
    tmp_path: Path,
    valid_catalog_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "knowledge"

    exit_code, stdout, stderr = _invoke(
        ["generate", str(valid_catalog_path), str(destination)], capsys
    )

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "command": "generate",
        "concepts": 11,
        "destination": str(destination),
    }
    metric = (destination / "metrics/gross_margin.md").read_text(
        encoding="utf-8"
    )
    source = (destination / "sources/products.md").read_text(
        encoding="utf-8"
    )
    root_index = (destination / "index.md").read_text(encoding="utf-8")
    assert "type: Metric\n" in metric
    assert "selayer_fingerprint:" in metric
    assert "\n  fingerprint:" not in metric
    assert "type: Data Source\n" in source
    assert "resource: data/products.parquet\n" in source
    assert "* [Gross margin](/metrics/gross_margin.md)\n" in root_index
    assert "* [Gross margin](/metrics/gross_margin.md) - " not in root_index
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py::test_write_creates_descriptive_bundle_absolute_progressive_indexes \
  tests/okf/test_generation.py::test_index_entry_order_remains_deterministic \
  tests/okf/test_generation.py::test_default_generate_indexes_do_not_invent_descriptions \
  tests/okf/test_sync.py::test_sync_writes_new_concepts_and_regenerates_indexes \
  tests/okf/test_cli.py::test_generate_creates_an_interoperable_bundle_and_reports_stable_json \
  -q
```

Expected: FAIL because current indexes use `-` bullets, root-relative or directory-local targets, and no descriptions.

- [ ] **Step 3: Implement one shared absolute descriptive entry renderer**

Add this helper immediately before `index_documents` in `src/selayer/okf/generation.py`:

```python
def _index_entry(concept: OkfConcept) -> str:
    target = f"/{concept.relative_path.as_posix()}"
    description = concept.frontmatter.get("description")
    suffix = (
        f" - {description}"
        if isinstance(description, str) and description
        else ""
    )
    return f"* [{concept.frontmatter['title']}]({target}){suffix}"
```

Replace the root/local link construction inside `index_documents` with:

```python
    for directory in _KIND_DIRECTORIES.values():
        entries = grouped[directory]
        if not entries:
            continue
        heading = display_title(directory)
        links = "\n".join(_index_entry(concept) for concept in entries)
        root_parts.append(f"# {heading}\n\n{links}")
        documents[PurePosixPath(directory, "index.md")] = (
            f"# {heading}\n\n{links}\n"
        )
```

Do not change `validate_links`, `_resolved_link`, or parsing; authored relative links remain supported.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: PASS with identical absolute targets in root and per-kind indexes, descriptions only when generated into frontmatter, and the exact pre-existing kind/concept order.

- [ ] **Step 5: Run generation, validation-link, retrieval, and sync regressions**

Run:

```bash
uv run pytest \
  tests/okf/test_generation.py \
  tests/okf/test_validation.py \
  tests/okf/test_retrieval.py \
  tests/okf/test_sync.py \
  tests/okf/test_cli.py \
  -q
```

Expected: PASS; generated absolute links resolve, authored relative links remain valid, and synchronization regenerates indexes deterministically.

- [ ] **Step 6: Commit the independently reviewable index change**

```bash
git add \
  src/selayer/okf/generation.py \
  tests/okf/test_generation.py \
  tests/okf/test_sync.py \
  tests/okf/test_cli.py
git commit -m "feat(okf): render descriptive absolute indexes"
```

---

### Task 6: Public Documentation and End-to-End Interoperability Verification

**Files:**

- Modify: `README.md:93-128`
- Modify: `tests/okf/test_documentation.py:17-39`

**Interfaces:**

- Consumes: the completed generation, document, validation, and sync behavior from Tasks 1-5.
- Produces: README contract text for canonical types, catalog-backed resources, absolute index links with optional descriptions, top-level `selayer_fingerprint`, legacy read support, and safe automatic migration.
- Produces: no runtime interface changes.

- [ ] **Step 1: Add failing documentation contract assertions**

Replace `test_readme_documents_api_authority_and_explicit_exclusions` with this complete contract test:

```python
def test_readme_documents_api_authority_and_explicit_exclusions(root: Path) -> None:
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert EXPECTED_USAGE in readme
    for statement in (
        "The YAML catalog controls execution; OKF is advisory context only.",
        "`write()` creates new bundles",
        "`generate()` follows the same new-bundle-only safety contract",
        "root `index.md`, per-kind `index.md`, and root append-only `log.md`",
        "`sync()` preserves curated sections",
        "MLFB color requires a real catalog dimension before it is queryable",
        "Data values are never exported",
        "Generated catalog concepts use generic OKF types such as `Metric`",
        "Catalog-backed sources and dimensions expose physical `resource` values",
        "Generated index links are bundle-absolute and include descriptions when available",
        "`selayer_fingerprint` is private synchronization metadata",
        "unchanged legacy generated documents are upgraded automatically",
        "semantic search",
        "multi-provider brokering",
        "wiki publishing",
        "RAG",
        "embeddings",
        "orchestration",
    ):
        assert statement in readme
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run:

```bash
uv run pytest \
  tests/okf/test_documentation.py::test_readme_documents_api_authority_and_explicit_exclusions \
  -q
```

Expected: FAIL because the README does not yet contain the five interoperability statements.

- [ ] **Step 3: Document the completed generated-bundle contract**

Insert this paragraph after the existing paragraph beginning `Bundles use root index.md` and before the paragraph beginning `The deeper selayer.okf API` in `README.md`:

```markdown
Generated catalog concepts use generic OKF types such as `Metric` while loading
and synchronization continue to accept the corresponding legacy `Selayer …`
types. Catalog-backed sources and dimensions expose physical `resource` values:
a source uses its catalog path verbatim and a dimension appends a percent-encoded
`#column=` fragment. Generated index links are bundle-absolute and include
descriptions when available. `selayer_fingerprint` is private synchronization
metadata at the top level; the legacy `generated.fingerprint` location remains
readable during migration. On `sync()`, unchanged legacy generated documents are
upgraded automatically, while curated edits still produce conflicts and remain
untouched.
```

Keep the existing statement that catalog YAML is executable authority and OKF is advisory.

- [ ] **Step 4: Run documentation and complete focused OKF verification**

Run:

```bash
uv run pytest \
  tests/okf/test_documentation.py \
  tests/okf/test_generation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py \
  tests/okf/test_sync.py \
  tests/okf/test_retrieval.py \
  -q
```

Expected: PASS with all interoperability, migration, round-trip, retrieval, documentation, and conflict-preservation coverage green.

- [ ] **Step 5: Run static checks on every changed Python file**

Run:

```bash
uv run ruff check \
  src/selayer/okf/generation.py \
  src/selayer/okf/document.py \
  src/selayer/okf/validation.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_generation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py \
  tests/okf/test_sync.py \
  tests/okf/test_cli.py \
  tests/okf/test_documentation.py
uv run ruff format --check \
  src/selayer/okf/generation.py \
  src/selayer/okf/document.py \
  src/selayer/okf/validation.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_generation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py \
  tests/okf/test_sync.py \
  tests/okf/test_cli.py \
  tests/okf/test_documentation.py
uv run pyright \
  src/selayer/okf/generation.py \
  src/selayer/okf/document.py \
  src/selayer/okf/validation.py \
  src/selayer/okf/bundle.py \
  tests/okf/test_generation.py \
  tests/okf/test_document.py \
  tests/okf/test_validation.py \
  tests/okf/test_sync.py \
  tests/okf/test_cli.py \
  tests/okf/test_documentation.py
```

Expected: Ruff reports no violations and no formatting changes; Pyright reports `0 errors`.

- [ ] **Step 6: Commit the independently reviewable documentation contract**

```bash
git add README.md tests/okf/test_documentation.py
git commit -m "docs(okf): describe generation interoperability"
```

---

## Final Full Verification

- [ ] **Run the complete test suite**

```bash
uv run pytest -q
```

Expected: PASS with zero failed tests.

- [ ] **Run repository-wide Ruff lint and format verification**

```bash
uv run ruff check src tests examples
uv run ruff format --check src tests examples
```

Expected: `All checks passed!` and no files requiring formatting.

- [ ] **Run repository-wide static type checking**

```bash
uv run pyright src tests examples
```

Expected: `0 errors`.

- [ ] **Build the package**

```bash
uv build
```

Expected: wheel and source distribution build successfully without dependency or packaging changes.

- [ ] **Verify patch hygiene and a clean implementation worktree**

```bash
git diff --check
git status --short
```

Expected: `git diff --check` emits no output and `git status --short` emits no output after the focused commits.
