# OKF v0.2 Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [`) syntax for tracking.

**Goal:** Model, validate, and surface authored Attested Computation concepts so selayer fully follows OKF v0.2's flagship feature (spec §10), then close the remaining alignment gaps from the v0.2 audit.

**Architecture:** selayer keeps frontmatter as frozen mappings. The Attested Computation contract is *derived* into typed frozen dataclasses at retrieval time, mirroring how `trust_tier()` and `freshness()` derive values. New validators extend the existing optional-family validation pattern. `OkfBundle.context_for()` already follows links, so retrieving a metric that links to an Attested Computation surfaces the typed contract via a new `ContextItem.attested_computation` field. selayer does not generate attested computations from the catalog (they are authored knowledge) and never executes or attests anything.

**Tech Stack:** Python 3.13, frozen `@dataclass(slots=True)`, `collections.abc.Mapping`, PyYAML, pytest, Ruff, Pyright.

## Global Constraints

- The validated selayer catalog remains the sole authority for executable semantics; OKF stays advisory and is never read by the planner or compiler.
- Target OKF v0.2 ([spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)); preserve unknown concept types and unknown frontmatter extension fields.
- Add no runtime dependency beyond the existing PyYAML dependency.
- Do not execute, attest, or cache attestation runs; do not generate attested computations from the catalog.
- All contract fields except `runtime` are optional; a minimal `type: Attested Computation` with only `runtime` must remain valid (keeps the existing MLFB fixture valid).
- Retrieval must stay deterministic, attributed, and explicitly bounded.
- Existing tests must keep passing without behavior regressions.

---

## File responsibility map

- `src/selayer/okf/model.py` — add `OkfParameter` and `AttestedComputation` frozen dataclasses; extend `ContextItem`.
- `src/selayer/okf/validation.py` — add `parameters`/`computation`/`executor`/`attester` structural validators; wire into `validate_concept`.
- `src/selayer/okf/computation.py` — NEW: typed derivation `attested_computation(concept)` and `# Computation` body extraction.
- `src/selayer/okf/bundle.py` — populate `ContextItem.attested_computation` in `_context_item()`.
- `src/selayer/okf/__init__.py` — export `AttestedComputation`, `OkfParameter`.
- `tests/okf/fixtures/mlfb/computations/mlfb_decoder.md` — expand to a full contract.
- `tests/okf/test_validation.py`, `tests/okf/test_computation.py` (new), `tests/okf/test_retrieval.py`, `tests/okf/test_mlfb_scenario.py`, `tests/okf/test_public_api.py` — focused and integration coverage.

---

### Task 1: Model the Attested Computation contract dataclasses

**Files:**

- Modify: `src/selayer/okf/model.py`
- Test: `tests/okf/test_public_api.py`

**Interfaces:**

- Produces: `OkfParameter(name: str, type: str, required: bool)`, `AttestedComputation(runtime, parameters, computation_path, computation_body, executor_resource, executor_receipt, attester_resource)`.
- Produces: `ContextItem` gains `attested_computation: AttestedComputation | None = None`.

- [ ] **Step 1: Write the failing public-API test**

Append to `tests/okf/test_public_api.py`:

```python
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
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest -q tests/okf/test_public_api.py::test_okf_exports_attested_computation_types`
Expected: FAIL — `AttestedComputation` / `OkfParameter` are not importable from `selayer.okf`.

- [ ] **Step 3: Add the dataclasses and extend ContextItem in model.py**

In `src/selayer/okf/model.py`, add after the `OkfSection` dataclass (before `OkfConcept`):

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
    computation_path: str | None
    computation_body: str
    executor_resource: str | None
    executor_receipt: tuple[str, ...]
    attester_resource: str | None
```

Extend `ContextItem` with the new optional field (keep field order; append last so existing positional construction still works):

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

Add the two new names to `__all__`:

```python
__all__ = [
    "AttestedComputation",
    "ContextBudgetError",
    "ContextItem",
    "ContextLookupError",
    "ContextResult",
    "Freshness",
    "OkfConcept",
    "OkfIssue",
    "OkfMetadataError",
    "OkfParameter",
    "OkfSection",
    "OkfValidationError",
    "Severity",
    "SyncReport",
    "TrustTier",
]
```

- [ ] **Step 4: Export the new types from the package init**

In `src/selayer/okf/__init__.py`, add to the model import and `__all__`:

```python
from .model import (
    AttestedComputation,
    ContextBudgetError,
    ContextItem,
    ContextLookupError,
    ContextResult,
    OkfConcept,
    OkfIssue,
    OkfParameter,
    OkfValidationError,
    SyncReport,
)

__all__ = [
    "AttestedComputation",
    "ContextBudgetError",
    "ContextItem",
    "ContextLookupError",
    "ContextResult",
    "OkfBundle",
    "OkfConcept",
    "OkfIssue",
    "OkfParameter",
    "OkfValidationError",
    "SyncReport",
]
```

- [ ] **Step 5: Run the test and verify GREEN, then lint**

Run: `uv run pytest -q tests/okf/test_public_api.py::test_okf_exports_attested_computation_types && uv run ruff check src/selayer/okf/model.py src/selayer/okf/__init__.py tests/okf/test_public_api.py && uv run pyright src/selayer/okf/model.py src/selayer/okf/__init__.py`
Expected: PASS, no lint/type errors.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/okf/model.py src/selayer/okf/__init__.py tests/okf/test_public_api.py
git commit -m "feat(okf): model attested computation contract"
```

---

### Task 2: Validate the contract frontmatter fields

**Files:**

- Modify: `src/selayer/okf/validation.py`
- Test: `tests/okf/test_validation.py`

**Interfaces:**

- Consumes: `OkfConcept`, `OkfIssue` from `.model`; `_issue`, `_is_nonempty_string` (existing helpers in this module).
- Produces: structural validation of `parameters`, `computation`, `executor`, `attester` when present on an `Attested Computation`; reported as sorted error `OkfIssue`s.

- [ ] **Step 1: Write the failing validation tests**

Append to `tests/okf/test_validation.py`:

```python
@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        ("type: Attested Computation\nruntime: python\nparameters: nope", "parameters"),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{type: year}]",
            "parameters[0].name",
        ),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{name: year}]",
            "parameters[0].type",
        ),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{name: year, type: int, required: yes}]",
            "parameters[0].required",
        ),
        (
            "type: Attested Computation\nruntime: python\ncomputation: ''",
            "computation",
        ),
        (
            "type: Attested Computation\nruntime: python\nexecutor: nope",
            "executor",
        ),
        (
            "type: Attested Computation\nruntime: python\nexecutor: {receipt: nope}",
            "executor.resource",
        ),
        (
            "type: Attested Computation\nruntime: python\nexecutor: {resource: run.md, receipt: []}",
            "executor.receipt",
        ),
        (
            "type: Attested Computation\nruntime: python\nattester: nope",
            "attester",
        ),
        (
            "type: Attested Computation\nruntime: python\nattester: {}",
            "attester.resource",
        ),
    ],
)
def test_invalid_attested_computation_fields_are_rejected(
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


def test_minimal_attested_computation_remains_valid(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Attested Computation\nruntime: python")
    assert OkfBundle.load(tmp_path).concepts["concept"]


def test_full_attested_computation_contract_is_valid(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Attested Computation\n"
        "runtime: bigquery\n"
        "parameters:\n"
        "  - {name: year, type: integer, required: true}\n"
        "computation: references/computations/revenue.sql\n"
        "executor:\n"
        "  resource: references/skills/run-on-bq.md\n"
        "  receipt: [job_id, executed_sql, result]\n"
        "attester:\n"
        "  resource: references/attesters/revenue.py\n",
    )
    assert OkfBundle.load(tmp_path).concepts["concept"]
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest -q tests/okf/test_validation.py -k attested`
Expected: FAIL — the new contract-field checks are not implemented, so malformed `parameters`/`executor`/`attester`/`computation` are accepted (no error raised).

- [ ] **Step 3: Implement the validators**

In `src/selayer/okf/validation.py`, add these helpers after `_validate_sources`:

```python
def _validate_parameters(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [_issue(concept, "parameters", "parameters must be a list")]
    issues: list[OkfIssue] = []
    for index, param in enumerate(value):
        field = f"parameters[{index}]"
        if not isinstance(param, Mapping):
            issues.append(_issue(concept, field, "parameter must be a mapping"))
            continue
        if not _is_nonempty_string(param.get("name")):
            issues.append(
                _issue(concept, f"{field}.name", "name must be a non-empty string")
            )
        if not _is_nonempty_string(param.get("type")):
            issues.append(
                _issue(concept, f"{field}.type", "type must be a non-empty string")
            )
        if "required" in param and not isinstance(param.get("required"), bool):
            issues.append(
                _issue(concept, f"{field}.required", "required must be a boolean")
            )
    return issues


def _validate_computation(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if not _is_nonempty_string(value):
        return [_issue(concept, "computation", "computation must be a non-empty string path")]
    return []


def _validate_executor(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [_issue(concept, "executor", "executor must be a mapping")]
    issues: list[OkfIssue] = []
    if not _is_nonempty_string(value.get("resource")):
        issues.append(
            _issue(concept, "executor.resource", "resource must be a non-empty string")
        )
    receipt = value.get("receipt")
    if "receipt" in value:
        if not isinstance(receipt, (list, tuple)):
            issues.append(_issue(concept, "executor.receipt", "receipt must be a list"))
        elif not receipt:
            issues.append(
                _issue(
                    concept,
                    "executor.receipt",
                    "receipt must contain at least one field",
                )
            )
        else:
            for index, name in enumerate(receipt):
                if not _is_nonempty_string(name):
                    issues.append(
                        _issue(
                            concept,
                            f"executor.receipt[{index}]",
                            "receipt field must be a non-empty string",
                        )
                    )
    return issues


def _validate_attester(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [_issue(concept, "attester", "attester must be a mapping")]
    if not _is_nonempty_string(value.get("resource")):
        return [
            _issue(concept, "attester.resource", "resource must be a non-empty string")
        ]
    return []
```

Then replace the existing single `runtime` check in `validate_concept`:

```python
    if concept_type == "Attested Computation" and not _is_nonempty_string(
        frontmatter.get("runtime")
    ):
        issues.append(_issue(concept, "runtime", "runtime must be a non-empty string"))
```

with the expanded block:

```python
    if concept_type == "Attested Computation":
        if not _is_nonempty_string(frontmatter.get("runtime")):
            issues.append(
                _issue(concept, "runtime", "runtime must be a non-empty string")
            )
        if "parameters" in frontmatter:
            issues.extend(_validate_parameters(concept, frontmatter["parameters"]))
        if "computation" in frontmatter:
            issues.extend(_validate_computation(concept, frontmatter["computation"]))
        if "executor" in frontmatter:
            issues.extend(_validate_executor(concept, frontmatter["executor"]))
        if "attester" in frontmatter:
            issues.extend(_validate_attester(concept, frontmatter["attester"]))
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_validation.py`
Expected: PASS (new tests and all existing validation tests).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/selayer/okf/validation.py tests/okf/test_validation.py && uv run pyright src/selayer/okf/validation.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/okf/validation.py tests/okf/test_validation.py
git commit -m "feat(okf): validate attested computation contract"
```

---

### Task 3: Derive the typed contract and extract the Computation body

**Files:**

- Create: `src/selayer/okf/computation.py`
- Test: `tests/okf/test_computation.py`

**Interfaces:**

- Consumes: `AttestedComputation`, `OkfConcept`, `OkfParameter`, `OkfSection` from `.model`; `Mapping` from `collections.abc`.
- Produces: `attested_computation(concept: OkfConcept) -> AttestedComputation | None` — returns the typed contract for an `Attested Computation` concept, else `None`.

- [ ] **Step 1: Write the failing derivation tests**

Create `tests/okf/test_computation.py`:

```python
from pathlib import PurePosixPath
from types import MappingProxyType

from selayer.okf.computation import attested_computation
from selayer.okf.model import AttestedComputation, OkfConcept, OkfSection


def _concept(frontmatter: dict, sections: tuple[OkfSection, ...] = ()) -> OkfConcept:
    return OkfConcept.create(
        concept_id="c",
        relative_path=PurePosixPath("c.md"),
        frontmatter=frontmatter,
        sections=sections,
    )


def test_non_attested_concept_returns_none() -> None:
    assert attested_computation(_concept({"type": "Metric"})) is None


def test_minimal_attested_computation_derives_empty_contract() -> None:
    contract = attested_computation(_concept({"type": "Attested Computation", "runtime": "bigquery"}))
    assert contract == AttestedComputation(
        runtime="bigquery",
        parameters=(),
        computation_path=None,
        computation_body="",
        executor_resource=None,
        executor_receipt=(),
        attester_resource=None,
    )


def test_inline_computation_body_is_extracted() -> None:
    concept = _concept(
        {"type": "Attested Computation", "runtime": "python"},
        sections=(
            OkfSection("Computation", "    def decode(mlfb): ..."),
            OkfSection("Meaning", "Interpret documented positions."),
        ),
    )
    contract = attested_computation(concept)
    assert contract is not None
    assert contract.computation_body == "    def decode(mlfb): ..."


def test_file_path_computation_and_contract_fields_are_derived() -> None:
    concept = _concept(
        {
            "type": "Attested Computation",
            "runtime": "dbt",
            "parameters": [
                {"name": "year", "type": "integer", "required": True},
                {"name": "segment", "type": "string"},
            ],
            "computation": "references/computations/profit.sql",
            "executor": {
                "resource": "references/skills/run-dbt.md",
                "receipt": ["run_id", "compiled_sql", "result"],
            },
            "attester": {"resource": "references/attesters/dbt-binding.py"},
        }
    )
    contract = attested_computation(concept)
    assert contract is not None
    assert contract.computation_path == "references/computations/profit.sql"
    assert contract.parameters == (
        # type(note): required coerced to bool via .get default
    ) or contract.parameters[0].name == "year"
    assert contract.parameters[0].required is True
    assert contract.parameters[1].required is False
    assert contract.executor_resource == "references/skills/run-dbt.md"
    assert contract.executor_receipt == ("run_id", "compiled_sql", "result")
    assert contract.attester_resource == "references/attesters/dbt-binding.py"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest -q tests/okf/test_computation.py`
Expected: FAIL — `selayer.okf.computation` does not exist.

- [ ] **Step 3: Implement the derivation module**

Create `src/selayer/okf/computation.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

from .model import AttestedComputation, OkfConcept, OkfParameter

_COMPUTATION_SECTION = "Computation"


def _computation_body(concept: OkfConcept) -> str:
    for section in concept.sections:
        if section.title == _COMPUTATION_SECTION:
            return section.content
    return ""


def _parameters(frontmatter: Mapping[str, object]) -> tuple[OkfParameter, ...]:
    raw = frontmatter.get("parameters")
    if not isinstance(raw, (list, tuple)):
        return ()
    derived: list[OkfParameter] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        param_type = entry.get("type")
        if not isinstance(name, str) or not isinstance(param_type, str):
            continue
        required = entry.get("required", False)
        derived.append(
            OkfParameter(name=name, type=param_type, required=bool(required))
        )
    return tuple(derived)


def _executor(
    frontmatter: Mapping[str, object]
) -> tuple[str | None, tuple[str, ...]]:
    executor = frontmatter.get("executor")
    if not isinstance(executor, Mapping):
        return None, ()
    resource = executor.get("resource")
    receipt = executor.get("receipt")
    resource_value = resource if isinstance(resource, str) else None
    receipt_value = (
        tuple(item for item in receipt if isinstance(item, str))
        if isinstance(receipt, (list, tuple))
        else ()
    )
    return resource_value, receipt_value


def _attester(frontmatter: Mapping[str, object]) -> str | None:
    attester = frontmatter.get("attester")
    if not isinstance(attester, Mapping):
        return None
    resource = attester.get("resource")
    return resource if isinstance(resource, str) else None


def attested_computation(concept: OkfConcept) -> AttestedComputation | None:
    """Derive the typed Attested Computation contract, or None for other types."""
    if concept.frontmatter.get("type") != "Attested Computation":
        return None
    frontmatter = concept.frontmatter
    runtime = frontmatter.get("runtime")
    computation = frontmatter.get("computation")
    executor_resource, executor_receipt = _executor(frontmatter)
    return AttestedComputation(
        runtime=runtime if isinstance(runtime, str) else "",
        parameters=_parameters(frontmatter),
        computation_path=computation if isinstance(computation, str) else None,
        computation_body=_computation_body(concept),
        executor_resource=executor_resource,
        executor_receipt=executor_receipt,
        attester_resource=_attester(frontmatter),
    )


__all__ = ["attested_computation"]
```

Fix the placeholder assertion in the test from Step 1 — replace the awkward `parameters == (...)` line so the test asserts the first parameter name cleanly:

```python
    assert contract.parameters[0].name == "year"
    assert contract.parameters[0].required is True
    assert contract.parameters[1].required is False
```

(Delete the `assert contract.parameters == (` / `) or contract.parameters[0].name == "year"` two-line assertion entirely.)

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_computation.py`
Expected: PASS.

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/selayer/okf/computation.py tests/okf/test_computation.py && uv run pyright src/selayer/okf/computation.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/okf/computation.py tests/okf/test_computation.py
git commit -m "feat(okf): derive attested computation contract"
```

---

### Task 4: Surface attested computations in bounded retrieval

**Files:**

- Modify: `src/selayer/okf/bundle.py`
- Test: `tests/okf/test_retrieval.py`

**Interfaces:**

- Consumes: `attested_computation` from `.computation`; `OkfConcept`, `ContextItem` from `.model`.
- Produces: `ContextItem.attested_computation` populated for every retrieved `Attested Computation` concept.

- [ ] **Step 1: Write the failing retrieval test**

Append to `tests/okf/test_retrieval.py` (add `Path` to imports if absent):

```python
def test_attested_computation_contract_is_surfaced_on_linked_context(
    tmp_path: Path,
) -> None:
    decoder = tmp_path / "computations" / "decoder.md"
    decoder.parent.mkdir_parent()
    decoder.write_text(
        "---\n"
        "type: Attested Computation\n"
        "runtime: python\n"
        "parameters:\n"
        "  - {name: mlfb, type: string, required: true}\n"
        "executor:\n"
        "  resource: run.md\n"
        "  receipt: [decoded]\n"
        "attester:\n"
        "  resource: check.py\n"
        "---\n\n"
        "# Computation\n\n"
        "    def decode(mlfb): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "m.md").write_text(
        "---\ntype: Selayer Metric\nselayer_id: metric.gross_margin\n---\n\n"
        "# Definition\n\nDecoded by [the decoder](../computations/decoder.md).\n",
        encoding="utf-8",
    )

    bundle = OkfBundle.load(tmp_path)
    result = bundle.context_for(["metric.gross_margin"], max_depth=1)

    decoder_item = next(
        item for item in result.items if item.concept_id == "computations/decoder"
    )
    contract = decoder_item.attested_computation
    assert contract is not None
    assert contract.runtime == "python"
    assert contract.parameters[0].name == "mlfb"
    assert contract.executor_resource == "run.md"
    assert contract.attester_resource == "check.py"
    assert "def decode" in contract.computation_body
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest -q tests/okf/test_retrieval.py::test_attested_computation_contract_is_surfaced_on_linked_context`
Expected: FAIL — `decoder_item.attested_computation` is `None` (not yet populated).

- [ ] **Step 3: Wire the derivation into `_context_item`**

In `src/selayer/okf/bundle.py`, add the import near the other local imports (after the `.validation` import block):

```python
from .computation import attested_computation
```

Then extend `_context_item` to populate the new field:

```python
def _context_item(concept: OkfConcept, today: date) -> ContextItem:
    frontmatter = concept.frontmatter
    sources = _sources(frontmatter)
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
    )
```

- [ ] **Step 4: Run the retrieval tests and verify GREEN**

Run: `uv run pytest -q tests/okf/test_retrieval.py`
Expected: PASS (new and existing retrieval tests).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/selayer/okf/bundle.py tests/okf/test_retrieval.py && uv run pyright src/selayer/okf/bundle.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/selayer/okf/bundle.py tests/okf/test_retrieval.py
git commit -m "feat(okf): surface attested computations in context retrieval"
```

---

### Task 5: Complete the MLFB fixture and extend the scenario test

**Files:**

- Modify: `tests/okf/fixtures/mlfb/computations/mlfb_decoder.md`
- Test: `tests/okf/test_mlfb_scenario.py`

**Interfaces:**

- Consumes: the existing MLFB fixture bundle and `OkfBundle.context_for()`.
- Produces: a scenario proving `dimension.mlfb` retrieval surfaces a complete attested computation contract without adding queryable dimensions.

- [ ] **Step 1: Write the failing scenario assertion**

Append to `tests/okf/test_mlfb_scenario.py`:

```python
def test_mlfb_retrieval_surfaces_the_attested_computation_contract(
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

    decoder = next(
        item for item in result.items if item.concept_id == "computations/mlfb_decoder"
    )
    contract = decoder.attested_computation
    assert contract is not None
    assert contract.runtime == "python"
    assert contract.parameters[0].name == "mlfb"
    assert contract.attester_resource is not None
    with pytest.raises(ContextLookupError):
        bundle.context_for(["dimension.product_color"])
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest -q tests/okf/test_mlfb_scenario.py::test_mlfb_retrieval_surfaces_the_attested_computation_contract`
Expected: FAIL — `contract.parameters[0].name` raises (the fixture has no parameters) and `attester_resource` is `None`.

- [ ] **Step 3: Expand the MLFB fixture to a full contract**

Overwrite `tests/okf/fixtures/mlfb/computations/mlfb_decoder.md`:

```markdown
---
type: Attested Computation
title: Synthetic MLFB decoder
runtime: python
status: stable
parameters:
  - {name: mlfb, type: string, required: true}
executor:
  resource: ../references/mlfb_coding_guide.md
  receipt: [decoded_value]
attester:
  resource: ../references/mlfb_coding_guide.md
---

# Computation

    def decode(mlfb: str) -> str:
        # Illustrative only; contains no proprietary decoding logic.
        return mlfb

# Meaning

This illustrative fixture records that an approved decoder may interpret documented
positions. It contains no executable or proprietary decoding logic.
```

- [ ] **Step 4: Run the full OKF suite and verify GREEN**

Run: `uv run pytest -q tests/okf`
Expected: PASS (all OKF unit, retrieval, scenario, and fixture-based tests).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check tests/okf && uv run pyright src/selayer/okf`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add tests/okf/fixtures/mlfb/computations/mlfb_decoder.md tests/okf/test_mlfb_scenario.py
git commit -m "test(okf): surface full attested computation in mlfb scenario"
```

---

## Subsequent alignment tasks (later)

These are conformant today and are tracked in the design doc (`2026-07-28-okf-v02-alignment-design.md` §6). Each becomes its own TDD task after the Attested Computation work lands:

1. Lenient consumer mode (`load(strict=False)`).
2. Type-naming decision (`Selayer Metric` vs `Metric`).
3. Index descriptions (§8).
4. Absolute links (§6.1).
5. Relocate `generated.fingerprint` to `selayer_fingerprint`.
6. Surface credibility signals + per-claim attribution.
7. Generate top-level `resource` for catalog-backed assets.
8. v0.1 fallback (`timestamp`/`# Citations`).

---

## Verification (run after all tasks)

```bash
uv run pytest -q
uv run ruff check src tests
uv run pyright src/selayer/okf
```

Expected: full test suite passes, no lint or type errors, and the MLFB scenario proves an authored Attested Computation is validated, round-tripped, and surfaced as a typed contract in bounded advisory context without affecting query planning.
