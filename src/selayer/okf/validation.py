from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, unquote, urlsplit

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
# Maximum length of a path/fragment label echoed in a link diagnostic, so an
# attacker-controlled link cannot bloat messages or smuggle long secrets.
_MAX_LINK_LABEL = 80


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


def _heading_slugs_from_titles(titles: Iterable[str]) -> frozenset[str]:
    """GitHub-style anchors with duplicate-heading suffix disambiguation.

    The first occurrence of a heading keeps the bare lowercase hyphenated
    slug; subsequent duplicates gain an incrementing suffix (``foo``,
    ``foo-1``, ``foo-2``). A suffix that would collide with a distinct heading
    that already produced the same slug is skipped past, matching GitHub.
    """
    emitted: set[str] = set()
    counts: dict[str, int] = {}
    for title in titles:
        base = _heading_slug(title)
        candidate = base
        if base in emitted:
            counts[base] = counts.get(base, 0) + 1
            candidate = f"{base}-{counts[base]}"
            while candidate in emitted:
                counts[base] += 1
                candidate = f"{base}-{counts[base]}"
        emitted.add(candidate)
    return frozenset(emitted)


def _section_slugs(concept: OkfConcept) -> frozenset[str]:
    return _heading_slugs_from_titles(section.title for section in concept.sections)


def _link_issue(
    concept: OkfConcept, message: str, *, code: str = "okf.invalid"
) -> OkfIssue:
    return OkfIssue(
        path=f"{concept.relative_path.as_posix()}.links",
        message=message,
        severity="warning",
        code=code,
    )


def _bounded_label(value: str) -> str:
    """Truncate a diagnostic label so a hostile link cannot bloat messages."""
    return (
        value
        if len(value) <= _MAX_LINK_LABEL
        else value[:_MAX_LINK_LABEL] + "..."
    )


def _fragment_link_message() -> str:
    """Fixed, secret-safe message for a missing fragment heading.

    The raw link, the path, the query, and the URL-decoded fragment are never
    echoed: a fragment may itself carry a secret (e.g. ``#token=...``), so the
    diagnostic is a fixed string. The issue code and path still identify the
    source document.
    """
    return "internal link references a fragment heading that does not exist"


def _broken_link_message(normalized: PurePosixPath | None) -> str:
    """Message for a broken internal link that echoes only the safe path.

    The normalized bundle-relative path excludes any query/fragment, so a
    ``?query=...`` secret on the raw link is never surfaced.
    """
    if normalized is None:
        return "broken internal link"
    return f"broken internal link to '{_bounded_label(normalized.as_posix())}'"


def _malformed_link_message() -> str:
    """Fixed, secret-safe message for a link that could not be parsed.

    The raw link is never echoed: a malformed URL may carry a secret in its
    scheme/userinfo/query/fragment, and ``urllib.parse.urlsplit`` fails before
    those components can be separated, so the diagnostic is a fixed string.
    The issue code and path still identify the source document.
    """
    return "link could not be parsed"


def _safe_urlsplit(link: str) -> SplitResult | None:
    """Parse a link URL, returning None when it is malformed.

    ``urllib.parse.urlsplit`` raises ``ValueError`` for some malformed inputs
    (e.g. an unterminated IPv6 literal such as ``http://[``). A validator must
    never crash on attacker-controlled link text, so a parse failure is
    surfaced as a coded, secret-safe diagnostic instead.
    """
    try:
        return urlsplit(link)
    except ValueError:
        return None


def _normalized_link_path(source: OkfConcept, link: str) -> PurePosixPath | None:
    """Return the normalized bundle-relative path for an internal link, or None."""
    split = _safe_urlsplit(link)
    if split is None:
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
    return PurePosixPath(normalized)


def _index_markdown_paths(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("index.md") if path.is_file()))


def _file_section_slugs(path: Path) -> frozenset[str]:
    """Heading slugs for a non-concept markdown file (e.g. a generated index)."""
    from .document import split_sections

    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    sections = split_sections(text)[1]
    return _heading_slugs_from_titles(section.title for section in sections)


def validate_links(
    root: Path,
    concepts: Mapping[str, OkfConcept],
) -> tuple[OkfIssue, ...]:
    """Return warnings for internal links with absent targets or stale fragments.

    Same-document fragment links (``#heading``) resolve against the source
    concept's own section slugs; cross-document links resolve the file target
    first, then the fragment against the target's slugs. Fragments are checked
    against generated ``index.md`` files too (they are not parsed as concepts).
    External URLs are out of scope and fragments are URL-decoded before
    comparison.
    """
    resolved_root = root.resolve()
    slugs_by_path: dict[PurePosixPath, frozenset[str]] = {
        concept.relative_path: _section_slugs(concept)
        for concept in concepts.values()
    }
    for index_path in _index_markdown_paths(root):
        relative = PurePosixPath(index_path.relative_to(root).as_posix())
        slugs_by_path.setdefault(relative, _file_section_slugs(index_path))
    issues: list[OkfIssue] = []
    for concept in concepts.values():
        source = root / Path(concept.relative_path.as_posix())
        source_slugs = slugs_by_path.get(
            concept.relative_path, _section_slugs(concept)
        )
        for link in concept.links:
            split = _safe_urlsplit(link)
            if split is None:
                issues.append(
                    _link_issue(
                        concept,
                        _malformed_link_message(),
                        code="okf.link.malformed",
                    )
                )
                continue
            if split.scheme or split.netloc:
                continue
            path_text = unquote(split.path)
            fragment = unquote(split.fragment) if split.fragment else ""
            normalized = _normalized_link_path(concept, link)
            if not path_text:
                if fragment and fragment not in source_slugs:
                    issues.append(
                        _link_issue(
                            concept,
                            _fragment_link_message(),
                            code="okf.link.missing_fragment",
                        )
                    )
                continue
            target = (
                root / path_text.lstrip("/")
                if path_text.startswith("/")
                else source.parent / path_text
            ).resolve()
            if not target.is_relative_to(resolved_root) or not target.exists():
                issues.append(_link_issue(concept, _broken_link_message(normalized)))
                continue
            if not fragment:
                continue
            target_slugs = (
                slugs_by_path.get(normalized) if normalized is not None else None
            )
            if target_slugs is not None and fragment not in target_slugs:
                issues.append(
                    _link_issue(
                        concept,
                        _fragment_link_message(),
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
    expected_indexes: Mapping[PurePosixPath, str],
    severity: Severity,
) -> list[OkfIssue]:
    issues: list[OkfIssue] = []
    for relative_path, expected_text in expected_indexes.items():
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


def _controlled_frontmatter_signature(
    frontmatter: Mapping[str, object],
    controlled_keys: tuple[str, ...],
    *,
    compare_generated_descriptive: bool = True,
) -> dict[str, object]:
    """Return controlled frontmatter with generated.fingerprint and .at removed.

    The digest and the generation timestamp are validated or tolerated
    separately, so both are excluded when comparing the generator-owned
    frontmatter against the fresh catalog projection.

    ``generated.descriptive`` is a newer generation-metadata flag. Legacy
    bundles generated before the flag existed carry no ``descriptive`` key, so
    comparing it against a fresh projection (which always stamps the flag)
    would reject otherwise-valid legacy bundles. When
    ``compare_generated_descriptive`` is false the flag is dropped from the
    generated sub-mapping on both sides of the comparison, so legacy bundles
    are accepted while current bundles (whose loaded metadata declares the
    flag, so the default ``True`` applies) are still validated against it.
    """
    signature: dict[str, object] = {}
    for key in controlled_keys:
        if key not in frontmatter:
            continue
        value = frontmatter[key]
        if key == "generated" and isinstance(value, Mapping):
            signature[key] = {
                name: value[name]
                for name in value
                if name not in ("fingerprint", "at")
                and (compare_generated_descriptive or name != "descriptive")
            }
        else:
            signature[key] = value
    return signature


def _has_generated_directory_indexes(
    root: Path, expected_indexes: Mapping[PurePosixPath, str]
) -> bool:
    """Return True when the bundle carries the *coherent* generated index set.

    ``OkfBundle.generate`` always stamps a per-directory ``index.md`` for every
    semantic kind that has at least one concept, so a genuine (or
    metadata-stripped) generated bundle carries the whole set. That artifact is
    not removed by the generated-metadata-stripping attack, so the complete set
    is a reliable marker that still audits a bundle whose every ``generated``
    mapping was stripped. A single nested ``index.md`` that merely *happens* to
    byte-match the generator's deterministic output for one directory is not,
    on its own, generated evidence: an authored bundle could coincidentally
    carry one. Detection therefore requires the *complete* expected
    per-directory index set to be present and to match the generator output —
    the whole set is a coherent marker that a coincidental authored index is
    not accompanied by. The root ``index.md`` is excluded: an authored bundle
    may carry one (``okf_version`` is an allowed optional field), so it cannot
    distinguish generated from authored bundles on its own.
    """
    expected_dir_indexes = {
        relative_path: expected_text
        for relative_path, expected_text in expected_indexes.items()
        if len(relative_path.parts) >= 2  # per-directory index, not the root
    }
    if not expected_dir_indexes:
        return False
    for relative_path, expected_text in expected_dir_indexes.items():
        index_file = root / Path(relative_path.as_posix())
        if not index_file.is_file():
            return False
        try:
            actual = index_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if actual != expected_text:
            return False
    return True


def validate_generated_integrity(
    root: Path,
    concepts: Mapping[str, OkfConcept],
    layer: SemanticLayer,
    *,
    strict: bool = True,
) -> tuple[OkfIssue, ...]:
    """Compare a catalog-aware bundle against the fresh generated projection."""
    from .document import CONTROLLED_FRONTMATTER_KEYS, generated_fingerprint
    from .generation import concepts_from_layer, index_documents

    severity: Severity = "error" if strict else "warning"
    has_generated = any(
        isinstance(concept.frontmatter.get("generated"), Mapping)
        for concept in concepts.values()
    )
    # Prefer the generator-declared descriptive mode (stamped in the generated
    # metadata) over inferring it from the loaded documents, so a descriptive
    # bundle whose descriptions were stripped and re-stamped is still detected.
    # Legacy bundles that predate the declared flag fall back to inferring the
    # mode from the loaded documents.
    declared_modes = [
        gen["descriptive"]
        for concept in concepts.values()
        if isinstance((gen := concept.frontmatter.get("generated")), Mapping)
        and isinstance(gen.get("descriptive"), bool)
    ]
    if declared_modes:
        descriptive = any(declared_modes)
    else:
        descriptive = any(
            "description" in concept.frontmatter
            for concept in concepts.values()
            if isinstance(concept.frontmatter.get("generated"), Mapping)
        )
    expected = concepts_from_layer(layer, include_descriptive=descriptive)
    expected_by_selayer = {
        concept.frontmatter["selayer_id"]: concept
        for concept in expected.values()
    }
    controlled_keys = CONTROLLED_FRONTMATTER_KEYS
    # The expected per-directory index documents are deterministic for the
    # layer (their content depends only on concept titles, not on descriptive
    # mode or any curated curation), so compute them once for both
    # generated-bundle detection and the index-integrity check.
    expected_indexes = index_documents(layer, expected)
    # Detect generated bundles from reliable generated evidence (any surviving
    # per-concept generated metadata, or a per-directory index whose content
    # matches the generator output) rather than the mere presence of an index
    # file, so authored bundles that happen to carry a nested ``index.md`` are
    # not misclassified.
    is_generated_bundle = has_generated or _has_generated_directory_indexes(
        root, expected_indexes
    )
    issues: list[OkfIssue] = []

    if is_generated_bundle:
        for concept_id, expected_concept in expected.items():
            semantic_id = expected_concept.frontmatter["selayer_id"]
            relative = expected_concept.relative_path.as_posix()
            loaded = concepts.get(concept_id)
            if loaded is None:
                issues.append(
                    OkfIssue(
                        path=relative,
                        message=f"missing generated concept '{semantic_id}'",
                        severity=severity,
                        code="okf.generated.missing_concept",
                    )
                )
                continue
            # The document is present at the expected path; require it to
            # retain the generator-owned metadata and the selayer_id binding
            # rather than only checking that the file exists.
            generated_metadata = loaded.frontmatter.get("generated")
            binding = loaded.frontmatter.get("selayer_id")
            if not isinstance(generated_metadata, Mapping) or not _is_nonempty_string(
                binding
            ):
                issues.append(
                    OkfIssue(
                        path=relative,
                        message=(
                            f"generated concept '{semantic_id}' is missing "
                            f"required generated metadata or selayer_id"
                        ),
                        severity=severity,
                        code="okf.generated.missing_metadata",
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
        # Compare the controlled frontmatter against the fresh catalog
        # projection so a forged controlled field is detected even when the
        # stored fingerprint was recomputed to stay internally self-consistent.
        # ``generated.descriptive`` is a newer flag: only compare it when the
        # loaded metadata actually declares it, so legacy bundles generated
        # before the flag are not rejected merely for lacking it.
        loaded_generated = concept.frontmatter.get("generated")
        loaded_declares_descriptive = isinstance(
            loaded_generated, Mapping
        ) and isinstance(loaded_generated.get("descriptive"), bool)
        # Only a genuinely *absent* flag may use the legacy comparison path. A
        # present-but-non-boolean flag must not be silently treated as legacy
        # absence (which would drop it from the controlled comparison): it is a
        # controlled-frontmatter defect that survives a re-stamped fingerprint,
        # so it is reported with a safe, value-free message. The comparison
        # below still drops ``descriptive`` for this case (it is not a bool), so
        # the dedicated issue is the single, clear signal rather than a
        # redundant generic mismatch.
        if (
            isinstance(loaded_generated, Mapping)
            and "descriptive" in loaded_generated
            and not isinstance(loaded_generated["descriptive"], bool)
        ):
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' descriptive flag "
                        f"must be a boolean when present"
                    ),
                    severity=severity,
                    code="okf.generated.frontmatter_mismatch",
                )
            )
        if _controlled_frontmatter_signature(
            concept.frontmatter,
            controlled_keys,
            compare_generated_descriptive=loaded_declares_descriptive,
        ) != _controlled_frontmatter_signature(
            expected_concept.frontmatter,
            controlled_keys,
            compare_generated_descriptive=loaded_declares_descriptive,
        ):
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' controlled "
                        f"frontmatter does not match the catalog"
                    ),
                    severity=severity,
                    code="okf.generated.frontmatter_mismatch",
                )
            )
        generated = concept.frontmatter.get("generated")
        stored_fingerprint = (
            generated.get("fingerprint")
            if isinstance(generated, Mapping)
            else None
        )
        if (
            not isinstance(stored_fingerprint, str)
            or _SHA256_HEX.fullmatch(stored_fingerprint) is None
        ):
            issues.append(
                OkfIssue(
                    path=concept.relative_path.as_posix(),
                    message=(
                        f"generated concept '{semantic_id}' is missing a valid "
                        f"generated.fingerprint"
                    ),
                    severity=severity,
                    code="okf.generated.fingerprint_missing",
                )
            )
        else:
            # Malformed but YAML-valid controlled values (e.g. a date-typed
            # title) make the canonical digest uncomputable. Surface that as a
            # coded issue instead of letting the TypeError escape the validator.
            try:
                recomputed = generated_fingerprint(
                    concept.frontmatter, loaded_definition
                )
            except (TypeError, ValueError):
                issues.append(
                    OkfIssue(
                        path=concept.relative_path.as_posix(),
                        message=(
                            f"generated concept '{semantic_id}' has controlled "
                            f"frontmatter that prevents fingerprint computation"
                        ),
                        severity=severity,
                        code="okf.generated.fingerprint_invalid",
                    )
                )
            else:
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

    if is_generated_bundle:
        issues.extend(_validate_generated_indexes(root, expected_indexes, severity))

    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


__all__ = [
    "validate_concept",
    "validate_duplicate_bindings",
    "validate_generated_integrity",
    "validate_index",
    "validate_links",
    "validate_log",
]
