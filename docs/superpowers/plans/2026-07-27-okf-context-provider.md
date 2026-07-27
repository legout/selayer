# OKF Context Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic OKF v0.2 generation, synchronization, validation, and bounded advisory retrieval for selayer semantic catalogs without allowing OKF to affect query planning.

**Architecture:** `selayer.okf` is a deep optional module around the validated `SemanticLayer`. It projects catalog objects into OKF concepts, preserves curated sections during synchronization, validates trust and semantic references, and returns attributed context items through `OkfBundle.context_for()`. Multi-provider brokering, wiki adapters, embeddings, and agent orchestration remain outside this repository.

**Tech Stack:** Python 3.13, frozen dataclasses, `pathlib`, PyYAML, pytest, Ruff, Pyright.

## Global Constraints

- Execute `docs/superpowers/plans/2026-07-27-grain-aware-query-planning.md` completely before starting this plan; this plan depends on its immutable schema-version-1 catalog and expression tree.
- The validated selayer catalog remains the sole authority for executable grain, joins, expressions, measures, metrics, and dimensions.
- OKF is advisory and must never be read by the planner or compiler.
- Target OKF v0.2; preserve unknown concept types and unknown frontmatter extension fields.
- Add no runtime dependency beyond the existing PyYAML dependency.
- Do not add embeddings, a vector database, wiki integration, an agent framework, raw-data profiling, or a multi-provider context broker.
- Catalog export contains no data values.
- Retrieval must be deterministic, attributed, and explicitly bounded.
- Synchronization must never silently overwrite curated content.

---

## File responsibility map

- `src/selayer/expressions/formatting.py` — canonical, round-trippable expression formatting for generated documentation.
- `src/selayer/catalog.py` — stable typed semantic-object enumeration and resolution.
- `src/selayer/okf/model.py` — OKF concepts, sections, diagnostics, context items, and reports.
- `src/selayer/okf/document.py` — UTF-8 frontmatter/body parsing, section handling, link extraction, and deterministic rendering.
- `src/selayer/okf/validation.py` — OKF v0.2 field validation, selayer binding validation, and link diagnostics.
- `src/selayer/okf/generation.py` — catalog-to-concept projection and deterministic indexes.
- `src/selayer/okf/bundle.py` — public generation, loading, writing, synchronization, and retrieval facade.
- `src/selayer/okf/__init__.py` — curated public OKF exports.
- `tests/okf/` — focused unit, golden, round-trip, synchronization, retrieval, and scenario coverage.

---

### Task 1: Add canonical expression formatting

**Files:**

- Create: `src/selayer/expressions/formatting.py`
- Modify: `src/selayer/expressions/__init__.py`
- Create: `tests/expressions/test_formatting.py`

**Interfaces:**

- Consumes: `Expression`, `Literal`, `Reference`, `UnaryOperation`, `BinaryOperation`, and `FunctionCall` from `selayer.expressions.ast`.
- Produces: `format_expression(expression: Expression) -> str`.
- Guarantees: `parse_expression(format_expression(expression)) == expression` for every parser-supported expression tree.

- [ ] **Step 1: Write failing round-trip formatting tests**

```python
# tests/expressions/test_formatting.py
import pytest

from selayer.expressions import format_expression, parse_expression


@pytest.mark.parametrize(
    "source",
    [
        "order_items.quantity * products.cost",
        "(revenue - cost) / revenue",
        "a + (b + c)",
        "-(a + 2)",
        "coalesce(name, \"unknown\")",
        "enabled == true",
        "value == null",
    ],
)
def test_formatted_expression_round_trips(source: str) -> None:
    expression = parse_expression(source)
    assert parse_expression(format_expression(expression)) == expression


def test_formatting_is_canonical() -> None:
    assert format_expression(parse_expression("a+(b*2)")) == "a + b * 2"
```

- [ ] **Step 2: Run the formatting tests and verify RED**

Run:

```bash
uv run pytest -q tests/expressions/test_formatting.py
```

Expected: collection fails because `format_expression` is not exported.

- [ ] **Step 3: Implement the formatter as a complete AST visitor**

```python
# src/selayer/expressions/formatting.py
from __future__ import annotations

import json

from .ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
)

_BINARY_PRECEDENCE = {
    "==": 1,
    "!=": 1,
    "<": 1,
    "<=": 1,
    ">": 1,
    ">=": 1,
    "+": 2,
    "-": 2,
    "*": 3,
    "/": 3,
}
_UNARY_PRECEDENCE = 4
_ATOM_PRECEDENCE = 5


def format_expression(expression: Expression) -> str:
    return _format(expression, parent_precedence=0, right_child=False)


def _format(
    expression: Expression,
    *,
    parent_precedence: int,
    right_child: bool,
) -> str:
    if isinstance(expression, Literal):
        if expression.value is None:
            return "null"
        if expression.value is True:
            return "true"
        if expression.value is False:
            return "false"
        if isinstance(expression.value, str):
            return json.dumps(expression.value, ensure_ascii=False)
        return str(expression.value)

    if isinstance(expression, Reference):
        return ".".join(expression.parts)

    if isinstance(expression, FunctionCall):
        arguments = ", ".join(
            _format(argument, parent_precedence=0, right_child=False)
            for argument in expression.arguments
        )
        return f"{expression.name}({arguments})"

    if isinstance(expression, UnaryOperation):
        operand = _format(
            expression.operand,
            parent_precedence=_UNARY_PRECEDENCE,
            right_child=True,
        )
        rendered = f"{expression.operator}{operand}"
        if _UNARY_PRECEDENCE < parent_precedence:
            return f"({rendered})"
        return rendered

    if isinstance(expression, BinaryOperation):
        precedence = _BINARY_PRECEDENCE[expression.operator]
        left = _format(
            expression.left,
            parent_precedence=precedence,
            right_child=False,
        )
        right = _format(
            expression.right,
            parent_precedence=precedence,
            right_child=True,
        )
        rendered = f"{left} {expression.operator} {right}"
        needs_parentheses = precedence < parent_precedence or (
            right_child and precedence == parent_precedence
        )
        if needs_parentheses:
            return f"({rendered})"
        return rendered

    raise TypeError(f"unsupported expression node: {type(expression).__name__}")
```

Export `format_expression` from `src/selayer/expressions/__init__.py` beside `parse_expression`.

- [ ] **Step 4: Run parser and formatter tests**

```bash
uv run pytest -q tests/expressions/test_parser.py tests/expressions/test_formatting.py
uv run ruff check src/selayer/expressions tests/expressions
uv run pyright src/selayer/expressions tests/expressions
```

Expected: all commands exit zero.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/selayer/expressions tests/expressions/test_formatting.py
git commit -m "feat(expressions): add canonical formatter"
```

---

### Task 2: Expose stable typed semantic objects

**Files:**

- Modify: `src/selayer/catalog.py`
- Modify: `tests/test_catalog.py`

**Interfaces:**

- Consumes: immutable catalog mappings introduced by the grain-aware query plan.
- Produces: `SemanticObject` type alias.
- Produces: `SemanticLayer.semantic_objects() -> Mapping[str, SemanticObject]`.
- Produces: `SemanticLayer.resolve(semantic_id: str) -> SemanticObject`.
- Guarantees: identifiers are sorted and use `source`, `dimension`, `fact`, `measure`, `metric`, and `relationship` prefixes.

- [ ] **Step 1: Write failing semantic-identifier tests**

```python
# append to tests/test_catalog.py
import pytest


def test_semantic_objects_have_stable_typed_identifiers(
    ecommerce_layer: SemanticLayer,
) -> None:
    objects = ecommerce_layer.semantic_objects()
    assert tuple(objects) == tuple(sorted(objects))
    assert objects["source.order_items"] is ecommerce_layer.data_sources["order_items"]
    assert objects["dimension.product_category"] is ecommerce_layer.dimensions[
        "product_category"
    ]
    assert objects["metric.gross_margin"] is ecommerce_layer.metrics["gross_margin"]


def test_resolve_rejects_unknown_semantic_identifier(
    ecommerce_layer: SemanticLayer,
) -> None:
    with pytest.raises(KeyError, match="dimension.product_color"):
        ecommerce_layer.resolve("dimension.product_color")
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
uv run pytest -q tests/test_catalog.py -k "semantic_objects or resolve"
```

Expected: failures because `semantic_objects()` and `resolve()` do not exist.

- [ ] **Step 3: Implement deterministic semantic enumeration**

Add this type alias and methods to `src/selayer/catalog.py`, using the final model imports produced by the prerequisite plan:

```python
from types import MappingProxyType
from typing import TypeAlias

from .model import DataSource, Dimension, Fact, Measure, Metric, Relationship

SemanticObject: TypeAlias = (
    DataSource | Dimension | Fact | Measure | Metric | Relationship
)


# methods on SemanticLayer

def semantic_objects(self) -> Mapping[str, SemanticObject]:
    collections: tuple[tuple[str, Mapping[str, SemanticObject]], ...] = (
        ("source", self.data_sources),
        ("dimension", self.dimensions),
        ("fact", self.facts),
        ("measure", self.measures),
        ("metric", self.metrics),
        ("relationship", self.relationships),
    )
    objects = {
        f"{kind}.{name}": value
        for kind, values in collections
        for name, value in values.items()
    }
    return MappingProxyType(dict(sorted(objects.items())))


def resolve(self, semantic_id: str) -> SemanticObject:
    try:
        return self.semantic_objects()[semantic_id]
    except KeyError:
        raise KeyError(f"unknown semantic identifier: {semantic_id}") from None
```

Keep these as methods rather than cached mutable state. The immutable mappings make repeated projection safe.

- [ ] **Step 4: Run catalog and type checks**

```bash
uv run pytest -q tests/test_catalog.py
uv run ruff check src/selayer/catalog.py tests/test_catalog.py
uv run pyright src/selayer/catalog.py tests/test_catalog.py
```

Expected: all commands exit zero.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/selayer/catalog.py tests/test_catalog.py
git commit -m "feat(catalog): expose typed semantic identifiers"
```

---

### Task 3: Parse and validate OKF concept documents

**Files:**

- Create: `src/selayer/okf/model.py`
- Create: `src/selayer/okf/document.py`
- Create: `src/selayer/okf/validation.py`
- Create: `src/selayer/okf/bundle.py`
- Create: `src/selayer/okf/__init__.py`
- Create: `tests/okf/test_document.py`
- Create: `tests/okf/test_validation.py`

**Interfaces:**

- Consumes: PyYAML and optional `SemanticLayer` bindings.
- Produces: `OkfSection`, `OkfConcept`, `OkfIssue`, and `OkfValidationError`.
- Produces: `parse_concept(path: Path, root: Path) -> OkfConcept` and `render_concept(concept: OkfConcept) -> str`.
- Produces: `OkfBundle.load(path: str | Path, layer: SemanticLayer | None = None) -> OkfBundle`.
- Guarantees: fatal issues are aggregated and sorted by `(path, message)`; broken links are warnings; unknown types and extension fields remain valid.

- [ ] **Step 1: Write failing frontmatter, section, and round-trip tests**

```python
# tests/okf/test_document.py
from pathlib import Path

from selayer.okf.document import parse_concept, render_concept


def test_parse_and_render_preserves_extensions_and_curated_sections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metrics" / "gross_margin.md"
    path.parent.mkdir()
    path.write_text(
        "---\n"
        "type: Selayer Metric\n"
        "selayer_id: metric.gross_margin\n"
        "custom_owner: finance\n"
        "---\n\n"
        "# Catalog Definition\n\nGenerated definition.\n\n"
        "# Usage Guidance\n\nUse item revenue.\n",
        encoding="utf-8",
    )

    concept = parse_concept(path, tmp_path)

    assert concept.concept_id == "metrics/gross_margin"
    assert concept.frontmatter["custom_owner"] == "finance"
    assert [section.title for section in concept.sections] == [
        "Catalog Definition",
        "Usage Guidance",
    ]
    assert "Use item revenue." in render_concept(concept)
```

Add the fenced-heading regression explicitly:

```python
def test_heading_inside_fence_is_not_a_section(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Reference\n---\n\n"
        "# Examples\n\n```text\n# Not a section\n```\n",
        encoding="utf-8",
    )

    concept = parse_concept(path, tmp_path)

    assert [section.title for section in concept.sections] == ["Examples"]
    assert "# Not a section" in concept.sections[0].content
```

- [ ] **Step 2: Write failing aggregate validation tests**

```python
# tests/okf/test_validation.py
from pathlib import Path

import pytest

from selayer.okf import OkfBundle, OkfValidationError


def test_load_collects_and_sorts_invalid_documents(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\ntitle: Missing type\n---\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(
        "---\ntype: Metric\nstatus: unknown\n---\n", encoding="utf-8"
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert list(caught.value.issues) == sorted(
        caught.value.issues,
        key=lambda issue: (issue.path, issue.message),
    )
    assert {issue.path for issue in caught.value.issues} == {
        "a.md.frontmatter.type",
        "b.md.frontmatter.status",
    }
```

Add these exact validation cases:

```python
@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        ("type: Metric\ngenerated: []", "concept.md.frontmatter.generated"),
        ("type: Metric\nsources: nope", "concept.md.frontmatter.sources"),
        (
            "type: Metric\nstale_after: not-a-date",
            "concept.md.frontmatter.stale_after",
        ),
        (
            "type: Attested Computation",
            "concept.md.frontmatter.runtime",
        ),
    ],
)
def test_invalid_optional_families_are_rejected(
    tmp_path: Path,
    frontmatter: str,
    issue_path: str,
) -> None:
    (tmp_path / "concept.md").write_text(
        f"---\n{frontmatter}\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert issue_path in {issue.path for issue in caught.value.issues}


@pytest.mark.parametrize(
    "verified",
    [
        "{by: human:owner, at: 2026-07-27T10:00:00Z}",
        "[{by: process:nightly, at: 2026-07-27T02:00:00Z}]",
    ],
)
def test_verified_accepts_mapping_and_list_forms(
    tmp_path: Path,
    verified: str,
) -> None:
    (tmp_path / "concept.md").write_text(
        f"---\ntype: Metric\nverified: {verified}\n---\n",
        encoding="utf-8",
    )
    assert OkfBundle.load(tmp_path).concepts["concept"]


def test_unknown_type_and_extension_are_preserved(tmp_path: Path) -> None:
    (tmp_path / "concept.md").write_text(
        "---\ntype: Product Identifier Scheme\ncustom_owner: product\n---\n",
        encoding="utf-8",
    )
    concept = OkfBundle.load(tmp_path).concepts["concept"]
    assert concept.frontmatter["custom_owner"] == "product"


def test_malformed_yaml_is_reported_at_document_path(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        "---\ntype: [unterminated\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "bad.md"
```

- [ ] **Step 3: Run the tests and verify RED**

```bash
uv run pytest -q tests/okf/test_document.py tests/okf/test_validation.py
```

Expected: collection fails because `selayer.okf` does not exist.

- [ ] **Step 4: Implement immutable document and diagnostic models**

```python
# src/selayer/okf/model.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, Mapping

Severity = Literal["error", "warning"]
TrustTier = Literal["unverified", "machine_confirmed", "human_reviewed"]
Freshness = Literal["current", "stale", "unspecified"]


@dataclass(frozen=True, slots=True)
class OkfIssue:
    path: str
    message: str
    severity: Severity = "error"


class OkfValidationError(ValueError):
    def __init__(self, issues: tuple[OkfIssue, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


@dataclass(frozen=True, slots=True)
class OkfSection:
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class OkfConcept:
    concept_id: str
    relative_path: PurePosixPath
    frontmatter: Mapping[str, Any]
    preamble: str
    sections: tuple[OkfSection, ...]
    links: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        concept_id: str,
        relative_path: PurePosixPath,
        frontmatter: Mapping[str, Any],
        preamble: str = "",
        sections: tuple[OkfSection, ...] = (),
        links: tuple[str, ...] = (),
    ) -> "OkfConcept":
        return cls(
            concept_id=concept_id,
            relative_path=relative_path,
            frontmatter=MappingProxyType(dict(frontmatter)),
            preamble=preamble,
            sections=sections,
            links=links,
        )
```

- [ ] **Step 5: Implement parsing and deterministic rendering**

In `src/selayer/okf/document.py`:

```python
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .model import OkfConcept, OkfSection

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^# ([^#].*)$")
_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


class OkfDocumentError(ValueError):
    pass


def parse_concept(path: Path, root: Path) -> OkfConcept:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise OkfDocumentError("missing YAML frontmatter")
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise OkfDocumentError(f"invalid YAML frontmatter: {error}") from error
    if not isinstance(loaded, dict):
        raise OkfDocumentError("frontmatter must be a mapping")
    body = text[match.end() :].lstrip("\n")
    preamble, sections = split_sections(body)
    relative_path = PurePosixPath(path.relative_to(root).as_posix())
    return OkfConcept.create(
        concept_id=relative_path.with_suffix("").as_posix(),
        relative_path=relative_path,
        frontmatter=loaded,
        preamble=preamble,
        sections=sections,
        links=tuple(_LINK.findall(body)),
    )


def split_sections(body: str) -> tuple[str, tuple[OkfSection, ...]]:
    preamble: list[str] = []
    sections: list[OkfSection] = []
    title: str | None = None
    content: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is not None:
            if title is None:
                preamble = content
            else:
                sections.append(OkfSection(title, "\n".join(content).strip()))
            title = heading.group(1).strip()
            content = []
        else:
            content.append(line)
    if title is None:
        preamble = content
    else:
        sections.append(OkfSection(title, "\n".join(content).strip()))
    return "\n".join(preamble).strip(), tuple(sections)


def render_concept(concept: OkfConcept) -> str:
    frontmatter = yaml.safe_dump(
        dict(concept.frontmatter),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    parts = [f"---\n{frontmatter}\n---"]
    if concept.preamble:
        parts.append(concept.preamble.rstrip())
    parts.extend(
        f"# {section.title}\n\n{section.content.rstrip()}".rstrip()
        for section in concept.sections
    )
    return "\n\n".join(parts).rstrip() + "\n"
```

- [ ] **Step 6: Implement validation and bundle loading**

Implement `validate_concept()` and `validate_links()` in `validation.py`. Use these exact policies:

```python
_STATUS = frozenset({"draft", "stable", "deprecated"})
_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}
```

Validation must:

1. require a non-empty string `type`;
2. accept unknown type names;
3. validate optional `status`, ISO `stale_after`, `generated`, `verified`, and `sources` shapes;
4. require `runtime` when `type == "Attested Computation"`;
5. require a string `selayer_id`, resolve it through the supplied layer, and require `_KIND_TYPES[prefix] == type`;
6. preserve unknown fields;
7. emit warning-severity issues for broken internal markdown links;
8. sort all issues by `(path, message)`.

Implement `OkfBundle.load()` in `bundle.py` as follows:

```python
@dataclass(frozen=True, slots=True)
class OkfBundle:
    root: Path | None
    concepts: Mapping[str, OkfConcept]
    diagnostics: tuple[OkfIssue, ...] = ()
    layer: SemanticLayer | None = None

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        layer: SemanticLayer | None = None,
    ) -> "OkfBundle":
        root = Path(path)
        concepts: dict[str, OkfConcept] = {}
        issues: list[OkfIssue] = []
        for concept_path in sorted(root.rglob("*.md")):
            if concept_path.name in {"index.md", "log.md"}:
                continue
            try:
                concept = parse_concept(concept_path, root)
            except OkfDocumentError as error:
                relative = concept_path.relative_to(root).as_posix()
                issues.append(OkfIssue(relative, str(error)))
                continue
            concepts[concept.concept_id] = concept
            issues.extend(validate_concept(concept, layer))
        issues.extend(validate_links(root, concepts))
        ordered = tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
        fatal = tuple(issue for issue in ordered if issue.severity == "error")
        if fatal:
            raise OkfValidationError(fatal)
        return cls(
            root=root,
            concepts=MappingProxyType(dict(sorted(concepts.items()))),
            diagnostics=ordered,
            layer=layer,
        )
```

Validate `index.md` and `log.md` structure when present: root `index.md` may contain only optional `okf_version` frontmatter; nested indexes contain no frontmatter; log date headings match `YYYY-MM-DD`. Add these tests before completing the implementation:

```python
def test_nested_index_rejects_frontmatter(tmp_path: Path) -> None:
    nested = tmp_path / "metrics"
    nested.mkdir()
    (nested / "index.md").write_text(
        "---\nokf_version: '0.2'\n---\n\n# Metrics\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "metrics/index.md"


def test_log_requires_iso_date_headings(tmp_path: Path) -> None:
    (tmp_path / "log.md").write_text(
        "# Directory Update Log\n\n## July 27\n* Update\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "log.md"
```

- [ ] **Step 7: Run the Task 3 suite and checks**

```bash
uv run pytest -q tests/okf/test_document.py tests/okf/test_validation.py
uv run ruff check src/selayer/okf tests/okf
uv run pyright src/selayer/okf tests/okf
```

Expected: all commands exit zero.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/selayer/okf tests/okf/test_document.py tests/okf/test_validation.py
git commit -m "feat(okf): parse and validate concept bundles"
```

---

### Task 4: Generate deterministic bundles from catalogs

**Files:**

- Create: `src/selayer/okf/generation.py`
- Modify: `src/selayer/okf/bundle.py`
- Create: `tests/okf/test_generation.py`

**Interfaces:**

- Consumes: `SemanticLayer.semantic_objects()` and `format_expression()`.
- Produces: `OkfBundle.from_layer(layer: SemanticLayer, generated_at: datetime | None = None) -> OkfBundle`.
- Produces: `OkfBundle.write(path: str | Path) -> None` for new bundles only.
- Guarantees: no data access; deterministic paths, frontmatter, definitions, and indexes; `generated.at` is emitted only when explicitly supplied.

- [ ] **Step 1: Write failing golden generation tests**

```python
# tests/okf/test_generation.py
from datetime import UTC, datetime
from pathlib import Path

from selayer.okf import OkfBundle


def test_generate_metric_concept(ecommerce_layer: SemanticLayer) -> None:
    bundle = OkfBundle.from_layer(
        ecommerce_layer,
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    concept = bundle.concepts["metrics/gross_margin"]

    assert concept.frontmatter == {
        "type": "Selayer Metric",
        "title": "Gross margin",
        "description": "Gross margin ratio",
        "selayer_id": "metric.gross_margin",
        "generated": {
            "by": "process:selayer-okf",
            "at": "2026-07-27T12:00:00Z",
        },
        "status": "stable",
    }
    definition = concept.sections[0]
    assert definition.title == "Catalog Definition"
    assert "(total_item_revenue - total_item_cost) / total_item_revenue" in (
        definition.content
    )


def test_generation_without_timestamp_is_deterministic(
    ecommerce_layer: SemanticLayer,
) -> None:
    first = OkfBundle.from_layer(ecommerce_layer)
    second = OkfBundle.from_layer(ecommerce_layer)
    assert first.concepts == second.concepts
```

- [ ] **Step 2: Write failing bundle-write tests**

```python
def test_write_creates_progressive_index(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)

    assert (destination / "metrics" / "gross_margin.md").is_file()
    index = (destination / "index.md").read_text(encoding="utf-8")
    assert "# Metrics" in index
    assert "[Gross margin](metrics/gross_margin.md)" in index


def test_write_refuses_to_replace_existing_bundle(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    destination.mkdir()
    (destination / "notes.md").write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="use sync"):
        OkfBundle.from_layer(ecommerce_layer).write(destination)
```

- [ ] **Step 3: Run generation tests and verify RED**

```bash
uv run pytest -q tests/okf/test_generation.py
```

Expected: failures because `from_layer()` and `write()` do not exist.

- [ ] **Step 4: Implement catalog projection**

In `generation.py`, define the exact path and type mappings:

```python
_KIND_DIRECTORIES = {
    "source": "sources",
    "dimension": "dimensions",
    "fact": "facts",
    "measure": "measures",
    "metric": "metrics",
    "relationship": "relationships",
}
_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}


def concept_path(semantic_id: str) -> PurePosixPath:
    kind, name = semantic_id.split(".", 1)
    return PurePosixPath(_KIND_DIRECTORIES[kind], f"{name}.md")


def display_title(name: str) -> str:
    return name.replace("_", " ").capitalize()
```

Implement one exhaustive `catalog_definition(semantic_id, value) -> str` visitor using `isinstance`. Render:

- source: physical type, path, and ordered grain;
- dimension: source, column, and data type;
- fact: source, data type, and canonical expression;
- measure: fact and aggregation;
- metric: declared measures and canonical expression;
- relationship: source/target columns and cardinality.

Raise `TypeError` for an unknown semantic object class; do not fall back to `repr()`.

Create concepts with `Catalog Definition` as the first section and empty `Usage Guidance`, `Examples`, `Caveats`, and `Related Concepts` sections. Include `description` only when the catalog object has a non-empty description. Emit `generated.at` as UTC `Z` only when `generated_at` is supplied; otherwise emit only `generated: {by: process:selayer-okf}`.

- [ ] **Step 5: Implement deterministic writing and indexes**

Add `from_layer()` and `write()` to `OkfBundle`. `write()` must:

1. refuse a destination containing any file;
2. write concepts in sorted concept-ID order;
3. create parent directories;
4. render each concept through `render_concept()`;
5. generate root and per-kind `index.md` files with sorted links and frontmatter descriptions;
6. use UTF-8 and `\n` endings;
7. never open a catalog source or call `DataSource.get_data()`.

Use atomic file creation:

```python
def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
```

- [ ] **Step 6: Run generation, golden, and no-data-access tests**

Add an explicit test proving that generation does not touch execution engines or data files:

```python
def test_generation_never_accesses_source_data(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import duckdb
    import polars as pl

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("OKF generation attempted data access")

    monkeypatch.setattr(duckdb, "connect", fail)
    monkeypatch.setattr(pl, "read_parquet", fail)

    OkfBundle.from_layer(ecommerce_layer).write(tmp_path / "knowledge")
```

```bash
uv run pytest -q tests/okf/test_generation.py
uv run ruff check src/selayer/okf tests/okf/test_generation.py
uv run pyright src/selayer/okf tests/okf/test_generation.py
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/selayer/okf tests/okf/test_generation.py
git commit -m "feat(okf): generate catalog knowledge bundles"
```

---

### Task 5: Synchronize without overwriting curated knowledge

**Files:**

- Modify: `src/selayer/okf/model.py`
- Modify: `src/selayer/okf/bundle.py`
- Create: `tests/okf/test_sync.py`

**Interfaces:**

- Consumes: an existing bundle directory and a newly generated bundle.
- Produces: `OkfBundle.sync(path: str | Path) -> SyncReport`.
- Produces: `SyncReport(written, unchanged, conflicts, orphaned)` as sorted tuples of relative paths.
- Guarantees: only controlled frontmatter keys and the single top-level `Catalog Definition` section may change; each file is updated atomically or left unchanged.

- [ ] **Step 1: Write failing curated-content preservation tests**

```python
# tests/okf/test_sync.py
from pathlib import Path

from selayer.okf import OkfBundle


def test_sync_preserves_curated_sections_and_extensions(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "status: stable",
        "status: stable\ncustom_owner: finance",
    ).replace(
        "# Usage Guidance\n",
        "# Usage Guidance\n\nUse item revenue as the denominator.\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)
    updated = metric_path.read_text(encoding="utf-8")

    assert "custom_owner: finance" in updated
    assert "Use item revenue as the denominator." in updated
    assert report.conflicts == ()
```

- [ ] **Step 2: Write failing conflict and verification invalidation tests**

```python
def test_sync_leaves_duplicate_generated_sections_unchanged(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    original = metric_path.read_text(encoding="utf-8")
    metric_path.write_text(
        original + "\n# Catalog Definition\n\nSecond generated section.\n",
        encoding="utf-8",
    )
    unsafe = metric_path.read_text(encoding="utf-8")

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert report.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_text(encoding="utf-8") == unsafe


def test_changed_definition_removes_current_verification(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        metric_path.read_text(encoding="utf-8").replace(
            "status: stable",
            "status: stable\nverified: {by: human:finance, at: 2026-07-27T12:00:00Z}",
        ),
        encoding="utf-8",
    )

    OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert "verified:" not in metric_path.read_text(encoding="utf-8")
```

The `changed_ecommerce_layer` fixture must change one metric expression through valid YAML rather than mutating the frozen catalog.

- [ ] **Step 3: Run sync tests and verify RED**

```bash
uv run pytest -q tests/okf/test_sync.py
```

Expected: failures because `SyncReport` and `sync()` do not exist.

- [ ] **Step 4: Implement synchronization reports and controlled merge**

```python
# add to src/selayer/okf/model.py
@dataclass(frozen=True, slots=True)
class SyncReport:
    written: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflicts: tuple[str, ...]
    orphaned: tuple[str, ...]
```

Use these controlled frontmatter keys:

```python
_GENERATED_KEYS = frozenset(
    {"type", "title", "description", "selayer_id", "generated"}
)
_GENERATED_SECTION = "Catalog Definition"
```

For every generated concept:

1. parse the existing file when present;
2. report a conflict and leave it byte-for-byte unchanged when it has zero or more than one `Catalog Definition` section and cannot be merged unambiguously;
3. replace only `_GENERATED_KEYS` from the generated concept;
4. preserve all unknown keys, curated sections, section order, and links;
5. compare the old and new generated definition content;
6. remove `verified` only when that semantic content changed;
7. write the complete merged file atomically;
8. regenerate indexes after concept writes.

New catalog concepts are written normally. Existing concepts whose `selayer_id` no longer resolves are reported in `orphaned` and left unchanged; synchronization does not delete or silently deprecate curated files.

Return every report tuple in sorted path order. A file appears in exactly one of `written`, `unchanged`, or `conflicts`; `orphaned` is an additional classification.

- [ ] **Step 5: Add partial-failure and atomicity tests**

Add explicit partial-failure and atomicity tests. Extend `changed_ecommerce_layer` so that it changes both the gross-margin expression and the product-category description:

```python
def test_conflict_does_not_block_other_safe_updates(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        metric_path.read_text(encoding="utf-8")
        + "\n# Catalog Definition\n\nDuplicate.\n",
        encoding="utf-8",
    )

    report = OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert "metrics/gross_margin.md" in report.conflicts
    assert "dimensions/product_category.md" in report.written


def test_successful_sync_leaves_no_temporary_files(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    OkfBundle.from_layer(ecommerce_layer).sync(destination)
    assert list(destination.rglob("*.tmp")) == []


def test_failed_atomic_replace_preserves_original(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    original = metric_path.read_bytes()
    real_replace = Path.replace

    def fail_metric_replace(source: Path, target: Path) -> Path:
        if target == metric_path:
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_metric_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert metric_path.read_bytes() == original
    assert list(destination.rglob("*.tmp")) == []
```

```bash
uv run pytest -q tests/okf/test_sync.py
```

Expected: all sync tests pass.

- [ ] **Step 6: Run the complete OKF suite and checks**

```bash
uv run pytest -q tests/okf
uv run ruff check src/selayer/okf tests/okf
uv run pyright src/selayer/okf tests/okf
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/selayer/okf tests/okf/test_sync.py
git commit -m "feat(okf): preserve curated context during sync"
```

---

### Task 6: Retrieve bounded attributed context

**Files:**

- Modify: `src/selayer/okf/model.py`
- Modify: `src/selayer/okf/bundle.py`
- Create: `tests/okf/test_retrieval.py`

**Interfaces:**

- Produces: `ContextItem`, `ContextResult`, `ContextLookupError`, and `ContextBudgetError`.
- Produces: `OkfBundle.context_for(semantic_ids, include_linked=True, max_chars=12_000, max_depth=1, today=None) -> ContextResult`.
- Guarantees: required semantic concepts are never silently truncated; linked concepts are added breadth-first in deterministic order; every item carries trust, freshness, sources, and semantic references.

- [ ] **Step 1: Write failing direct and linked retrieval tests**

```python
# tests/okf/test_retrieval.py
from datetime import date

from selayer.okf import OkfBundle


def test_context_for_returns_attributed_direct_concept(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["metric.gross_margin"],
        include_linked=False,
        max_chars=12_000,
        today=date(2026, 7, 27),
    )

    assert [item.semantic_refs for item in result.items] == [
        ("metric.gross_margin",)
    ]
    assert result.items[0].provider == "selayer"
    assert result.items[0].trust == "human_reviewed"
    assert result.total_chars <= 12_000


def test_context_for_follows_internal_links_breadth_first(
    loaded_okf_bundle: OkfBundle,
) -> None:
    result = loaded_okf_bundle.context_for(
        ["dimension.mlfb"],
        include_linked=True,
        max_depth=1,
        max_chars=12_000,
    )

    assert [item.concept_id for item in result.items] == [
        "dimensions/mlfb",
        "computations/mlfb_decoder",
        "concepts/mlfb_scheme",
        "references/mlfb_coding_guide",
    ]
```

- [ ] **Step 2: Write failing trust, freshness, and budget tests**

```python
def test_stale_concept_is_visible_and_diagnosed(
    stale_okf_bundle: OkfBundle,
) -> None:
    result = stale_okf_bundle.context_for(
        ["dimension.mlfb"],
        today=date(2026, 7, 27),
    )
    assert result.items[0].freshness == "stale"
    assert any("stale" in issue.message for issue in result.diagnostics)


def test_mandatory_concept_must_fit_budget(
    loaded_okf_bundle: OkfBundle,
) -> None:
    with pytest.raises(ContextBudgetError) as caught:
        loaded_okf_bundle.context_for(
            ["metric.gross_margin"],
            include_linked=False,
            max_chars=10,
        )
    assert caught.value.max_chars == 10


def test_unknown_semantic_id_is_explicit(
    loaded_okf_bundle: OkfBundle,
) -> None:
    with pytest.raises(ContextLookupError, match="dimension.product_color"):
        loaded_okf_bundle.context_for(["dimension.product_color"])
```

- [ ] **Step 3: Run retrieval tests and verify RED**

```bash
uv run pytest -q tests/okf/test_retrieval.py
```

Expected: failures because the context result types and `context_for()` do not exist.

- [ ] **Step 4: Implement context result types and errors**

```python
# add to src/selayer/okf/model.py
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


@dataclass(frozen=True, slots=True)
class ContextResult:
    items: tuple[ContextItem, ...]
    diagnostics: tuple[OkfIssue, ...]
    total_chars: int


class ContextLookupError(LookupError):
    pass


class ContextBudgetError(ValueError):
    def __init__(self, required_chars: int, max_chars: int) -> None:
        self.required_chars = required_chars
        self.max_chars = max_chars
        super().__init__(
            f"mandatory context requires {required_chars} characters; "
            f"budget is {max_chars}"
        )
```

- [ ] **Step 5: Implement trust, freshness, and source derivation**

Use these exact rules:

```python
def trust_tier(frontmatter: Mapping[str, Any]) -> TrustTier:
    verified = frontmatter.get("verified")
    if verified is None:
        return "unverified"
    events = [verified] if isinstance(verified, dict) else verified
    actors = [event["by"] for event in events]
    if any(actor.startswith("human:") for actor in actors):
        return "human_reviewed"
    return "machine_confirmed"


def freshness(frontmatter: Mapping[str, Any], today: date) -> Freshness:
    value = frontmatter.get("stale_after")
    if value is None:
        return "unspecified"
    stale_after = value if isinstance(value, date) else date.fromisoformat(value)
    return "stale" if today >= stale_after else "current"
```

Extract `sources[].resource` in declared order and semantic references from `selayer_id`. Do not infer trust from similarity scores, tags, or prose.

- [ ] **Step 6: Implement deterministic bounded traversal**

`context_for()` must:

1. reject `max_chars <= 0` and `max_depth < 0`;
2. build a unique `selayer_id -> concept` index and reject missing or duplicate bindings;
3. preserve caller semantic-ID order for required concepts;
4. render required concepts into attributed `ContextItem` values;
5. fail with `ContextBudgetError` when required items exceed the budget;
6. when enabled, resolve internal markdown links and traverse them breadth-first up to `max_depth`;
7. sort sibling links by resolved concept ID;
8. skip external URLs and broken links already present in diagnostics;
9. stop before adding an optional linked item that would exceed the budget;
10. append one warning diagnostic describing omitted linked context;
11. add a warning for each stale or unverified returned concept;
12. return `total_chars == sum(len(item.content) for item in items)`.

Render context item content from the concept itself, including its title, description, body sections, and canonical source links. Do not inject planner instructions or alternative executable formulas.

- [ ] **Step 7: Run retrieval and full OKF tests**

```bash
uv run pytest -q tests/okf/test_retrieval.py
uv run pytest -q tests/okf
uv run ruff check src/selayer/okf tests/okf
uv run pyright src/selayer/okf tests/okf
```

Expected: all commands exit zero.

- [ ] **Step 8: Commit Task 6**

```bash
git add src/selayer/okf tests/okf/test_retrieval.py
git commit -m "feat(okf): retrieve bounded attributed context"
```

---

### Task 7: Prove the MLFB scenario and publish the interface

**Files:**

- Modify: `src/selayer/okf/__init__.py`
- Modify: `src/selayer/__init__.py`
- Create: `tests/okf/fixtures/mlfb/dimensions/mlfb.md`
- Create: `tests/okf/fixtures/mlfb/concepts/mlfb_scheme.md`
- Create: `tests/okf/fixtures/mlfb/computations/mlfb_decoder.md`
- Create: `tests/okf/fixtures/mlfb/references/mlfb_coding_guide.md`
- Create: `tests/okf/test_mlfb_scenario.py`
- Modify: `README.md`

**Interfaces:**

- Exports from `selayer.okf`: `OkfBundle`, `OkfConcept`, `OkfIssue`, `OkfValidationError`, `SyncReport`, `ContextItem`, `ContextResult`, `ContextLookupError`, and `ContextBudgetError`.
- Leaves root `selayer` exports focused on the semantic layer; export only `OkfBundle` at the package root as the convenience entrypoint.
- Documents that MLFB-derived attributes require catalog dimensions before they become queryable.

- [ ] **Step 1: Create the synthetic MLFB fixture bundle**

Use non-proprietary synthetic content. The dimension file must bind `dimension.mlfb` and link to all three supporting concepts:

```markdown
---
type: Selayer Dimension
title: MLFB number
description: Structured product identifier used by the synthetic fixture.
selayer_id: dimension.mlfb
status: stable
verified: {by: human:product-data-owner, at: 2026-07-27T10:00:00Z}
sources:
  - id: coding-guide
    resource: ../references/mlfb_coding_guide.md
---

# Catalog Definition

Generated from `dimension.mlfb`.

# Usage Guidance

Use the [MLFB scheme](../concepts/mlfb_scheme.md) to interpret the identifier.
Use the [approved decoder](../computations/mlfb_decoder.md) for decoding.
See the [coding guide](../references/mlfb_coding_guide.md) for provenance.
Do not infer undocumented positions from samples.
```

The decoder fixture uses `type: Attested Computation`, declares `runtime: python`, and contains no executable proprietary logic. The scheme and guide explicitly state that the fixture is illustrative.

- [ ] **Step 2: Write the failing end-to-end scenario test**

```python
# tests/okf/test_mlfb_scenario.py
from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.okf import ContextLookupError, OkfBundle


def test_mlfb_context_links_interpretation_knowledge_without_adding_dimensions(
    tmp_path: Path,
    root: Path,
) -> None:
    catalog_path = tmp_path / "products.yaml"
    catalog_path.write_text(
        "version: 1\n"
        "name: products\n"
        "data_sources:\n"
        "  products:\n"
        "    type: parquet\n"
        "    path: data/products.parquet\n"
        "    grain: [id]\n"
        "dimensions:\n"
        "  mlfb:\n"
        "    source: products\n"
        "    column: mlfb\n"
        "    data_type: string\n"
        "    description: Product MLFB identifier\n"
        "facts: {}\n"
        "measures: {}\n"
        "metrics: {}\n"
        "relationships: {}\n",
        encoding="utf-8",
    )
    layer = SemanticLayer.load(catalog_path)
    bundle = OkfBundle.load(root / "tests/okf/fixtures/mlfb", layer=layer)

    result = bundle.context_for(["dimension.mlfb"], max_depth=1)

    assert {item.concept_id for item in result.items} == {
        "dimensions/mlfb",
        "concepts/mlfb_scheme",
        "computations/mlfb_decoder",
        "references/mlfb_coding_guide",
    }
    with pytest.raises(ContextLookupError):
        bundle.context_for(["dimension.product_color"])
```

- [ ] **Step 3: Run the scenario test and verify RED**

```bash
uv run pytest -q tests/okf/test_mlfb_scenario.py
```

Expected: fail until the fixture, public exports, and final link resolution are complete.

- [ ] **Step 4: Curate public exports**

Set `src/selayer/okf/__init__.py` to import and define `__all__` for the approved OKF types. Add only this convenience export to root `src/selayer/__init__.py`:

```python
from .okf import OkfBundle
```

Do not export document parser helpers, generation visitors, validation internals, or synchronization merge helpers from the root package.

- [ ] **Step 5: Document generation, synchronization, retrieval, and authority**

Add a concise README section containing this exact usage shape:

```python
from selayer import OkfBundle, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")

OkfBundle.from_layer(layer).write("knowledge")

bundle = OkfBundle.load("knowledge", layer=layer)
context = bundle.context_for(
    ["metric.gross_margin", "dimension.product_category"],
    include_linked=True,
    max_chars=12_000,
)
```

State explicitly:

- the catalog controls execution;
- OKF is advisory context;
- `write()` creates new bundles and `sync()` preserves curated sections;
- queryable decoded attributes such as MLFB color require real catalog dimensions;
- semantic search and multi-provider brokering belong outside selayer;
- data values are never exported by this feature.

- [ ] **Step 6: Run all scenario, documentation, and package checks**

```bash
uv run pytest -q tests/okf/test_mlfb_scenario.py tests/okf
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests
uv build
```

Expected: all commands exit zero and the wheel contains `selayer/okf`.

- [ ] **Step 7: Commit Task 7**

```bash
git add src/selayer/__init__.py src/selayer/okf tests/okf README.md
git commit -m "docs(okf): publish advisory context workflow"
```

---

## Final verification

- [ ] Run the complete verification suite from a clean working tree:

```bash
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests
uv build
git status --short
```

Expected: tests, Ruff, Pyright, and build exit zero; `git status --short` prints nothing.

- [ ] Audit the implementation against the delivery boundary:

```bash
rg -n "duckdb|plan_query|compile_duckdb|sentence_transformers|chromadb|qdrant|obsidian|langchain" src/selayer/okf
```

Expected: no planner/compiler, embedding, vector-store, wiki, or agent-framework integration appears under `src/selayer/okf`. A reference to DuckDB or planner concepts in user-facing explanatory text is acceptable only when it states that OKF does not control execution.

- [ ] Confirm that the implementation contains no data profiling or sample export:

```bash
rg -n "get_data\(|read_parquet|SELECT|sample\(" src/selayer/okf
```

Expected: no matches.
