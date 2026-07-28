from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from selayer.catalog import SemanticLayer

from .model import OkfConcept, OkfIssue

_STATUS = frozenset({"draft", "stable", "deprecated"})
_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SELAYER_ID = re.compile(
    r"(source|dimension|fact|measure|metric|relationship)\.([a-z][a-z0-9_]*)"
)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_LOG_HEADING = re.compile(r"^## (.+?)\s*$")


def _issue(concept: OkfConcept, field: str, message: str) -> OkfIssue:
    base = f"{concept.relative_path.as_posix()}.frontmatter"
    return OkfIssue(f"{base}.{field}" if field else base, message)


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
) -> list[OkfIssue]:
    if not isinstance(event, Mapping):
        return [_issue(concept, field, f"{field.rsplit('.', 1)[-1]} must be a mapping")]
    issues: list[OkfIssue] = []
    actor = event.get("by")
    if not _is_nonempty_string(actor):
        issues.append(_issue(concept, f"{field}.by", "by must be a non-empty string"))
    if "at" not in event:
        if require_at:
            issues.append(_issue(concept, f"{field}.at", "at is required"))
    elif not _is_iso_datetime(event["at"]):
        issues.append(_issue(concept, f"{field}.at", "at must be an ISO 8601 datetime"))
    return issues


def _validate_generated(concept: OkfConcept, value: object) -> list[OkfIssue]:
    return _validate_event(concept, value, "generated", require_at=False)


def _validate_verified(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if isinstance(value, Mapping):
        events: tuple[object, ...] = (value,)
        is_sequence = False
    elif isinstance(value, (list, tuple)):
        events = tuple(value)
        is_sequence = True
    else:
        return [_issue(concept, "verified", "verified must be a mapping or list")]
    return [
        issue
        for index, event in enumerate(events)
        for issue in _validate_event(
            concept,
            event,
            f"verified[{index}]" if is_sequence else "verified",
            require_at=True,
        )
    ]


def _validate_sources(concept: OkfConcept, value: object) -> list[OkfIssue]:
    if not isinstance(value, (list, tuple)):
        return [_issue(concept, "sources", "sources must be a list")]
    issues: list[OkfIssue] = []
    for index, source in enumerate(value):
        field = f"sources[{index}]"
        if not isinstance(source, Mapping):
            issues.append(_issue(concept, field, "source must be a mapping"))
            continue
        if not _is_nonempty_string(source.get("resource")):
            issues.append(
                _issue(
                    concept,
                    f"{field}.resource",
                    "resource must be a non-empty string",
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
) -> tuple[OkfIssue, ...]:
    """Validate required and recognized OKF v0.2 frontmatter fields."""
    frontmatter = concept.frontmatter
    issues: list[OkfIssue] = []
    concept_type = frontmatter.get("type")
    if not _is_nonempty_string(concept_type):
        issues.append(_issue(concept, "type", "type must be a non-empty string"))
    status = frontmatter.get("status")
    if "status" in frontmatter and (
        not isinstance(status, str) or status not in _STATUS
    ):
        issues.append(
            _issue(
                concept,
                "status",
                "status must be one of: deprecated, draft, stable",
            )
        )
    if "stale_after" in frontmatter and not _is_iso_date(frontmatter["stale_after"]):
        issues.append(
            _issue(concept, "stale_after", "stale_after must be an ISO 8601 date")
        )
    if "generated" in frontmatter:
        issues.extend(_validate_generated(concept, frontmatter["generated"]))
    if "verified" in frontmatter:
        issues.extend(_validate_verified(concept, frontmatter["verified"]))
    if "sources" in frontmatter:
        issues.extend(_validate_sources(concept, frontmatter["sources"]))
    if concept_type == "Attested Computation" and not _is_nonempty_string(
        frontmatter.get("runtime")
    ):
        issues.append(_issue(concept, "runtime", "runtime must be a non-empty string"))
    if "selayer_id" in frontmatter:
        issues.extend(_validate_selayer_id(concept, frontmatter["selayer_id"], layer))
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


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


def validate_links(
    root: Path,
    concepts: Mapping[str, OkfConcept],
) -> tuple[OkfIssue, ...]:
    """Return warnings for internal links whose filesystem targets are absent."""
    resolved_root = root.resolve()
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


__all__ = [
    "validate_concept",
    "validate_duplicate_bindings",
    "validate_index",
    "validate_links",
    "validate_log",
]
