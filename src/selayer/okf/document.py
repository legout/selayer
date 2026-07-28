from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

from .model import OkfConcept, OkfMetadataError, OkfSection

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
_RAW_FRONTMATTER = re.compile(
    r"\A---(?P<newline>\r\n|\n)(?P<content>.*?)"
    r"(?P<before_close>\r\n|\n)---(?P<after_close>\r\n|\n|\Z)",
    re.DOTALL,
)
_HEADING = re.compile(r"^# ([^#].*)$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,}).*$")
_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")
_YAML_SET_TAG = "tag:yaml.org,2002:set"
CONTROLLED_FRONTMATTER_KEYS = (
    "type",
    "title",
    "description",
    "selayer_id",
    "generated",
)


class _OkfSafeDumper(yaml.SafeDumper):
    pass


class OkfDocumentError(ValueError):
    pass


class OkfControlledMergeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _FrontmatterLayout:
    offset: int
    content_end: int
    newline: str
    body_offset: int
    spans: Mapping[str, tuple[int, int]]


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
    try:
        return OkfConcept.create(
            concept_id=relative_path.with_suffix("").as_posix(),
            relative_path=relative_path,
            frontmatter=loaded,
            preamble=preamble,
            sections=sections,
            links=tuple(_LINK.findall(body)),
        )
    except OkfMetadataError as error:
        raise OkfDocumentError("cyclic YAML frontmatter is not supported") from error


def _fence_marker(line: str) -> str | None:
    match = _FENCE_OPEN.match(line)
    return match.group(1) if match is not None else None


def _closes_fence(line: str, marker: str) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith(marker[0]):
        return False
    run_length = len(stripped) - len(stripped.lstrip(marker[0]))
    return run_length >= len(marker) and not stripped[run_length:].strip()


def split_sections(body: str) -> tuple[str, tuple[OkfSection, ...]]:
    preamble: list[str] = []
    sections: list[OkfSection] = []
    title: str | None = None
    content: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        if fence is None:
            fence = _fence_marker(line)
            heading = None if fence is not None else _HEADING.match(line)
        else:
            if _closes_fence(line, fence):
                fence = None
            heading = None
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


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_set_member_key(value: Any) -> str:
    dumped = yaml.dump(
        _thaw(value),
        Dumper=cast(type[yaml.Dumper], _OkfSafeDumper),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        canonical=True,
    )
    if dumped is None:
        raise AssertionError("YAML string serialization returned no content")
    return cast(str, dumped)


def _represent_frozenset(
    dumper: yaml.SafeDumper, value: frozenset[Any]
) -> yaml.nodes.MappingNode:
    members = sorted(value, key=_canonical_set_member_key)
    return dumper.represent_mapping(
        _YAML_SET_TAG, [(_thaw(member), None) for member in members]
    )


_OkfSafeDumper.add_representer(frozenset, _represent_frozenset)


def generated_fingerprint(
    frontmatter: Mapping[str, Any], catalog_definition: str
) -> str:
    """Hash the canonical generated projection, excluding the digest itself."""
    controlled = {
        key: _thaw(frontmatter[key])
        for key in CONTROLLED_FRONTMATTER_KEYS
        if key in frontmatter
    }
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


def _semantic_node_end(node: yaml.nodes.Node) -> int:
    if isinstance(node, yaml.nodes.ScalarNode) or (
        isinstance(node, yaml.nodes.CollectionNode) and node.flow_style
    ):
        if node.end_mark is None:
            raise OkfControlledMergeError("YAML node has no end position")
        return node.end_mark.index
    children: list[yaml.nodes.Node] = []
    if isinstance(node, yaml.nodes.MappingNode):
        children = [child for pair in node.value for child in pair]
    elif isinstance(node, yaml.nodes.SequenceNode):
        children = list(node.value)
    if not children:
        if node.end_mark is None:
            raise OkfControlledMergeError("YAML node has no end position")
        return node.end_mark.index
    return max(_semantic_node_end(child) for child in children)


def _value_span_end(text: str, semantic_end: int) -> int:
    if semantic_end > 0 and text[semantic_end - 1] == "\n":
        return semantic_end
    newline = text.find("\n", semantic_end)
    return len(text) if newline < 0 else newline + 1


def _frontmatter_layout(text: str) -> _FrontmatterLayout:
    match = _RAW_FRONTMATTER.match(text)
    if match is None:
        raise OkfControlledMergeError("missing YAML frontmatter")
    source = match.group("content")
    try:
        node = yaml.compose(source)
    except yaml.YAMLError as error:
        raise OkfControlledMergeError("invalid YAML frontmatter") from error
    if not isinstance(node, yaml.nodes.MappingNode):
        raise OkfControlledMergeError("frontmatter must be a mapping")
    offset = match.start("content")
    spans: dict[str, tuple[int, int]] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.nodes.ScalarNode):
            continue
        key = key_node.value
        if key in spans:
            if key in {*CONTROLLED_FRONTMATTER_KEYS, "verified"}:
                raise OkfControlledMergeError(f"duplicate frontmatter key '{key}'")
            continue
        if key_node.start_mark is None:
            raise OkfControlledMergeError("YAML key has no start position")
        start = offset + key_node.start_mark.index
        semantic_end = _semantic_node_end(value_node)
        end = offset + _value_span_end(source, semantic_end)
        spans[key] = (start, end)
    return _FrontmatterLayout(
        offset=offset,
        content_end=match.end("content"),
        newline=match.group("newline"),
        body_offset=match.end(),
        spans=spans,
    )


def _dump_frontmatter_entry(key: str, value: Any, newline: str) -> str:
    dumped = yaml.dump(
        {key: _thaw(value)},
        Dumper=cast(type[yaml.Dumper], _OkfSafeDumper),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if dumped is None:
        raise AssertionError("YAML string serialization returned no content")
    return cast(str, dumped).replace("\n", newline)


def _section_spans(
    text: str, body_offset: int
) -> Mapping[str, tuple[tuple[int, int], ...]]:
    sections: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    headings: list[tuple[str, int, int]] = []
    fence: str | None = None
    offset = body_offset
    for line in text[body_offset:].splitlines(keepends=True):
        semantic_line = line.rstrip("\r\n")
        if fence is None:
            fence = _fence_marker(semantic_line)
            heading = None if fence is not None else _HEADING.match(semantic_line)
        else:
            if _closes_fence(semantic_line, fence):
                fence = None
            heading = None
        if heading is not None:
            headings.append((heading.group(1).strip(), offset, offset + len(line)))
        offset += len(line)
    for index, (title, _heading_start, content_start) in enumerate(headings):
        content_end = headings[index + 1][1] if index + 1 < len(headings) else len(text)
        sections[title].append((content_start, content_end))
    return {title: tuple(spans) for title, spans in sections.items()}


def merge_generated_concept_text(
    existing_text: str,
    generated: OkfConcept,
    *,
    definition_changed: bool,
) -> str:
    """Patch generator-owned spans without rendering curated document bytes."""
    layout = _frontmatter_layout(existing_text)
    edits: list[tuple[int, int, str]] = []
    generated_keys = set(generated.frontmatter) & set(CONTROLLED_FRONTMATTER_KEYS)
    for key in CONTROLLED_FRONTMATTER_KEYS:
        span = layout.spans.get(key)
        if span is not None:
            replacement = (
                _dump_frontmatter_entry(key, generated.frontmatter[key], layout.newline)
                if key in generated_keys
                else ""
            )
            edits.append((*span, replacement))

    missing = [
        key
        for key in CONTROLLED_FRONTMATTER_KEYS
        if key in generated_keys and key not in layout.spans
    ]
    insertions: defaultdict[int, list[str]] = defaultdict(list)
    for key in missing:
        key_index = CONTROLLED_FRONTMATTER_KEYS.index(key)
        following = next(
            (
                layout.spans[candidate][0]
                for candidate in CONTROLLED_FRONTMATTER_KEYS[key_index + 1 :]
                if candidate in layout.spans
            ),
            layout.content_end,
        )
        insertions[following].append(
            _dump_frontmatter_entry(key, generated.frontmatter[key], layout.newline)
        )
    edits.extend(
        (offset, offset, "".join(entries)) for offset, entries in insertions.items()
    )

    sections = _section_spans(existing_text, layout.body_offset)
    definitions = sections.get("Catalog Definition", ())
    if len(definitions) != 1:
        raise OkfControlledMergeError("expected exactly one Catalog Definition section")
    if definition_changed:
        generated_definitions = tuple(
            section
            for section in generated.sections
            if section.title == "Catalog Definition"
        )
        if len(generated_definitions) != 1:
            raise OkfControlledMergeError(
                "generated concept must have one Catalog Definition section"
            )
        start, end = definitions[0]
        has_following_heading = end != len(existing_text)
        content = generated_definitions[0].content.replace("\n", layout.newline)
        replacement = layout.newline + content + layout.newline
        if has_following_heading:
            replacement += layout.newline
        edits.append((start, end, replacement))
        verified = layout.spans.get("verified")
        if verified is not None:
            edits.append((*verified, ""))

    merged = existing_text
    for start, end, replacement in sorted(
        edits, key=lambda edit: (edit[0], edit[1]), reverse=True
    ):
        merged = merged[:start] + replacement + merged[end:]
    return merged


def render_concept(concept: OkfConcept) -> str:
    dumped = yaml.dump(
        _thaw(concept.frontmatter),
        Dumper=cast(type[yaml.Dumper], _OkfSafeDumper),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if dumped is None:
        raise AssertionError("YAML string serialization returned no content")
    frontmatter = dumped.rstrip()
    parts = [f"---\n{frontmatter}\n---"]
    if concept.preamble:
        parts.append(concept.preamble.rstrip())
    parts.extend(
        f"# {section.title}\n\n{section.content.rstrip()}".rstrip()
        for section in concept.sections
    )
    return "\n\n".join(parts).rstrip() + "\n"


__all__ = [
    "CONTROLLED_FRONTMATTER_KEYS",
    "OkfControlledMergeError",
    "OkfDocumentError",
    "generated_fingerprint",
    "merge_generated_concept_text",
    "parse_concept",
    "render_concept",
    "split_sections",
]
