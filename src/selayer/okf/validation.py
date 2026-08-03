from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml

from selayer.catalog import SemanticLayer

from .model import OkfConcept, OkfIssue, Severity

_STATUS = frozenset({"draft", "stable", "deprecated"})
_COMPUTATION_SECTION = "Computation"
_CATALOG_DEFINITION_SECTION = "Catalog Definition"
_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
_SELAYER_ID = re.compile(
    r"(source|dimension|fact|measure|metric|relationship)\.([a-z][a-z0-9_]*)"
)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_LOG_HEADING = re.compile(r"^## (.+?)\s*$")
_SLUG_STRIP = re.compile(r"[^\w\s-]+", re.UNICODE)
_SLUG_COLLAPSE = re.compile(r"[\s_-]+")


def _issue(
    concept: OkfConcept,
    field: str,
    message: str,
    *,
    code: str = "okf.invalid",
) -> OkfIssue:
    base = f"{concept.relative_path.as_posix()}.frontmatter"
    return OkfIssue(f"{base}.{field}" if field else base, message, code=code)


def _optional_issue(
    concept: OkfConcept,
    field: str,
    message: str,
    severity: Severity,
    *,
    code: str = "okf.invalid",
) -> OkfIssue:
    issue = _issue(concept, field, message, code=code)
    return OkfIssue(
        path=issue.path, message=issue.message, severity=severity, code=code
    )


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_iso_date(value: object) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_iso_datetime(value: object) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


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


def _validate_sources(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [_optional_issue(concept, "sources", "sources must be a list", severity)]
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
    expected_type = _KIND_TYPES[prefix]
    issues: list[OkfIssue] = []
    if concept.frontmatter.get("type") != expected_type:
        issues.append(
            _issue(
                concept,
                "type",
                f"type must be '{expected_type}' for selayer_id '{semantic_id}'",
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
                    concept,
                    f"{field}.name",
                    "name must be a non-empty string",
                    severity,
                )
            )
        if not _is_nonempty_string(param.get("type")):
            issues.append(
                _optional_issue(
                    concept,
                    f"{field}.type",
                    "type must be a non-empty string",
                    severity,
                )
            )
        if "required" in param and not isinstance(param.get("required"), bool):
            issues.append(
                _optional_issue(
                    concept, f"{field}.required", "required must be a boolean", severity
                )
            )
    return issues


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


def _validate_executor(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [
            _optional_issue(concept, "executor", "executor must be a mapping", severity)
        ]
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


def _validate_attester(
    concept: OkfConcept,
    value: object,
    *,
    severity: Severity,
) -> list[OkfIssue]:
    if not isinstance(value, Mapping):
        return [
            _optional_issue(concept, "attester", "attester must be a mapping", severity)
        ]
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


def validate_duplicate_bindings(
    concepts: Mapping[str, OkfConcept],
) -> tuple[OkfIssue, ...]:
    bindings = [
        value
        for concept in concepts.values()
        if isinstance((value := concept.frontmatter.get("selayer_id")), str)
    ]
    duplicates = {value for value, count in Counter(bindings).items() if count > 1}
    issues: list[OkfIssue] = []
    for concept in concepts.values():
        binding = concept.frontmatter.get("selayer_id")
        if isinstance(binding, str) and binding in duplicates:
            issues.append(
                _issue(
                    concept,
                    "selayer_id",
                    f"duplicate selayer_id '{binding}'",
                )
            )
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


def _heading_slug(heading: str) -> str:
    """GitHub-style lowercase hyphenated anchor for a Markdown heading."""
    text = heading.strip().lower()
    text = _SLUG_STRIP.sub("", text)
    text = _SLUG_COLLAPSE.sub("-", text)
    return text.strip("-")


def _section_slugs(concept: OkfConcept) -> frozenset[str]:
    return frozenset(_heading_slug(section.title) for section in concept.sections)


def _resolve_link_concept(
    source: OkfConcept,
    link: str,
    concepts_by_path: Mapping[PurePosixPath, OkfConcept],
) -> OkfConcept | None:
    try:
        split = urlsplit(link)
    except ValueError:
        return None
    path_text = unquote(split.path)
    if not path_text:
        return None
    if path_text.startswith("/"):
        normalized = posixpath.normpath(path_text.lstrip("/"))
    else:
        normalized = posixpath.normpath(
            posixpath.join(source.relative_path.parent.as_posix(), path_text)
        )
    if normalized == ".." or normalized.startswith("../"):
        return None
    return concepts_by_path.get(PurePosixPath(normalized))


def validate_links(
    root: Path,
    concepts: Mapping[str, OkfConcept],
) -> tuple[OkfIssue, ...]:
    """Return warnings for internal links with absent targets or stale fragments."""
    resolved_root = root.resolve()
    concepts_by_path = {
        concept.relative_path: concept for concept in concepts.values()
    }
    issues: list[OkfIssue] = []
    for concept in concepts.values():
        source = root / Path(concept.relative_path.as_posix())
        for link in concept.links:
            split = urlsplit(link)
            if split.scheme or split.netloc or (not split.path and split.fragment):
                continue
            path_text = unquote(split.path)
            if not path_text:
                continue
            target = (
                root / path_text.lstrip("/")
                if path_text.startswith("/")
                else source.parent / path_text
            ).resolve()
            if not target.is_relative_to(resolved_root) or not target.exists():
                issues.append(
                    OkfIssue(
                        path=f"{concept.relative_path.as_posix()}.links",
                        message=f"broken internal link '{link}'",
                        severity="warning",
                    )
                )
                continue
            fragment = unquote(split.fragment) if split.fragment else ""
            if not fragment:
                continue
            target_concept = _resolve_link_concept(concept, link, concepts_by_path)
            if target_concept is not None and fragment not in _section_slugs(
                target_concept
            ):
                issues.append(
                    OkfIssue(
                        path=f"{concept.relative_path.as_posix()}.links",
                        message=f"link '{link}' fragment heading not found",
                        severity="warning",
                        code="okf.link.missing_fragment",
                    )
                )
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


def validate_index(path: Path, root: Path) -> tuple[OkfIssue, ...]:
    relative = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if path.parent != root:
        if text.startswith("---\n"):
            return (OkfIssue(relative, "nested index.md must not have frontmatter"),)
        return ()
    if not text.startswith("---\n"):
        return ()
    match = _FRONTMATTER.match(text)
    if match is None:
        return (OkfIssue(relative, "invalid root index.md frontmatter"),)
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return (OkfIssue(relative, "invalid root index.md frontmatter"),)
    if loaded is None:
        return ()
    if not isinstance(loaded, Mapping):
        return (OkfIssue(relative, "root index.md frontmatter must be a mapping"),)
    if set(loaded) - {"okf_version"}:
        return (
            OkfIssue(relative, "root index.md frontmatter allows only okf_version"),
        )
    version = loaded.get("okf_version")
    if "okf_version" in loaded and not _is_nonempty_string(version):
        return (OkfIssue(relative, "okf_version must be a non-empty string"),)
    return ()


def validate_log(path: Path, root: Path) -> tuple[OkfIssue, ...]:
    relative = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---\n"):
        return (OkfIssue(relative, "log.md must not have frontmatter"),)
    for line in text.splitlines():
        match = _LOG_HEADING.match(line)
        if match is not None and not _is_iso_date(match.group(1)):
            return (OkfIssue(relative, "log date headings must use YYYY-MM-DD"),)
    return ()


def _validate_generated_indexes(
    root: Path,
    expected: Mapping[str, OkfConcept],
    layer: SemanticLayer,
    severity: Severity,
) -> list[OkfIssue]:
    from .generation import index_documents

    issues: list[OkfIssue] = []
    for relative_path, expected_text in index_documents(layer, expected).items():
        relative = relative_path.as_posix()
        index_file = root / Path(relative)
        if not index_file.is_file():
            issues.append(
                OkfIssue(
                    path=relative,
                    message=f"missing generated index '{relative}'",
                    severity=severity,
                    code="okf.generated.index_mismatch",
                )
            )
            continue
        actual = index_file.read_text(encoding="utf-8")
        if actual != expected_text:
            issues.append(
                OkfIssue(
                    path=relative,
                    message=(
                        f"generated index '{relative}' does not match the catalog"
                    ),
                    severity=severity,
                    code="okf.generated.index_mismatch",
                )
            )
    return issues


def validate_generated_integrity(
    root: Path,
    concepts: Mapping[str, OkfConcept],
    layer: SemanticLayer,
    *,
    strict: bool = True,
) -> tuple[OkfIssue, ...]:
    """Compare a catalog-aware bundle against the fresh generated projection."""
    from .document import generated_fingerprint
    from .generation import concepts_from_layer

    severity: Severity = "error" if strict else "warning"
    expected = concepts_from_layer(layer, include_descriptive=False)
    expected_by_selayer = {
        concept.frontmatter["selayer_id"]: concept
        for concept in expected.values()
    }
    issues: list[OkfIssue] = []
    has_generated = any(
        isinstance(concept.frontmatter.get("generated"), Mapping)
        for concept in concepts.values()
    )

    if has_generated:
        for concept_id, expected_concept in expected.items():
            if concept_id not in concepts:
                issues.append(
                    OkfIssue(
                        path=expected_concept.relative_path.as_posix(),
                        message=(
                            f"missing generated concept "
                            f"'{expected_concept.frontmatter['selayer_id']}'"
                        ),
                        severity=severity,
                        code="okf.generated.missing_concept",
                    )
                )

    for concept in concepts.values():
        if not isinstance(concept.frontmatter.get("generated"), Mapping):
            continue
        semantic_id = concept.frontmatter.get("selayer_id")
        if not isinstance(semantic_id, str):
            continue
        expected_concept = expected_by_selayer.get(semantic_id)
        if expected_concept is None:
            issues.append(
                OkfIssue(
                    path=f"{concept.relative_path.as_posix()}.frontmatter.selayer_id",
                    message=f"orphan generated selayer_id '{semantic_id}'",
                    severity=severity,
                    code="okf.generated.orphan_selayer_id",
                )
            )
            continue
        if concept.relative_path != expected_concept.relative_path:
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' is in the wrong "
                        f"semantic kind directory"
                    ),
                    severity=severity,
                    code="okf.generated.path_mismatch",
                )
            )
            continue
        definitions = [
            section
            for section in concept.sections
            if section.title == _CATALOG_DEFINITION_SECTION
        ]
        expected_definitions = [
            section
            for section in expected_concept.sections
            if section.title == _CATALOG_DEFINITION_SECTION
        ]
        if len(definitions) != 1 or not expected_definitions:
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' must have exactly "
                        f"one Catalog Definition section"
                    ),
                    severity=severity,
                    code="okf.generated.definition_mismatch",
                )
            )
            continue
        loaded_definition = definitions[0].content
        if loaded_definition != expected_definitions[0].content:
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' Catalog Definition "
                        f"does not match the catalog"
                    ),
                    severity=severity,
                    code="okf.generated.definition_mismatch",
                )
            )
        generated = concept.frontmatter.get("generated")
        stored_fingerprint = (
            generated.get("fingerprint")
            if isinstance(generated, Mapping)
            else None
        )
        if isinstance(stored_fingerprint, str):
            recomputed = generated_fingerprint(concept.frontmatter, loaded_definition)
            if stored_fingerprint.lower() != recomputed:
                issues.append(
                    OkfIssue(
                        path=concept.relative_path.as_posix(),
                        message=(
                            f"generated concept '{semantic_id}' fingerprint "
                            f"does not match its content"
                        ),
                        severity=severity,
                        code="okf.generated.fingerprint_mismatch",
                    )
                )

    if has_generated:
        issues.extend(_validate_generated_indexes(root, expected, layer, severity))

    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


__all__ = [
    "validate_concept",
    "validate_duplicate_bindings",
    "validate_generated_integrity",
    "validate_index",
    "validate_links",
    "validate_log",
]
