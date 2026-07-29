from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from types import MappingProxyType

from selayer.catalog import SemanticLayer, SemanticObject
from selayer.expressions import format_expression
from selayer.model import DataSource, Dimension, Fact, Measure, Metric, Relationship
from selayer.sources.config import connector_kind
from selayer.sources.schema import schema_fingerprint

from .document import generated_fingerprint
from .model import OkfConcept, OkfSection

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
_CURATED_SECTION_TITLES = (
    "Usage Guidance",
    "Examples",
    "Caveats",
    "Related Concepts",
)


def concept_path(semantic_id: str) -> PurePosixPath:
    kind, name = semantic_id.split(".", 1)
    return PurePosixPath(_KIND_DIRECTORIES[kind], f"{name}.md")


def display_title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def generated_directories() -> tuple[str, ...]:
    return tuple(_KIND_DIRECTORIES.values())


def _field_summary(schema: object) -> str:
    """Render an ordered, name/type summary of a declared table schema."""

    fields = getattr(schema, "fields", ())
    entries = []
    for field in fields:
        entries.append(f"`{field.name}`")
    return ", ".join(entries)


def catalog_definition(semantic_id: str, value: SemanticObject) -> str:
    """Render the complete executable catalog definition without reading data."""
    lines = [f"Semantic ID: `{semantic_id}`"]
    if isinstance(value, DataSource):
        lines.extend(
            (
                f"Connector: `{connector_kind(value.connector)}`",
                f"Schema fingerprint: `{schema_fingerprint(value.schema)}`",
                f"Fields: {_field_summary(value.schema)}",
                f"Grain: {', '.join(f'`{column}`' for column in value.grain)}",
            )
        )
    elif isinstance(value, Dimension):
        lines.extend(
            (
                f"Source: `{value.source}`",
                f"Column: `{value.column}`",
                f"Data type: `{value.data_type}`",
            )
        )
    elif isinstance(value, Fact):
        lines.extend(
            (
                f"Source: `{value.source}`",
                f"Data type: `{value.data_type}`",
                f"Expression: `{format_expression(value.expression)}`",
            )
        )
    elif isinstance(value, Measure):
        lines.extend(
            (
                f"Fact: `{value.fact}`",
                f"Aggregation: `{value.aggregation}`",
            )
        )
    elif isinstance(value, Metric):
        lines.extend(
            (
                "Declared measures: "
                + ", ".join(f"`{measure}`" for measure in value.measures),
                f"Expression: `{format_expression(value.expression)}`",
            )
        )
    elif isinstance(value, Relationship):
        lines.extend(
            (
                f"Source: `{value.source}.{value.source_column}`",
                f"Target: `{value.target}.{value.target_column}`",
                f"Cardinality: `{value.type}`",
            )
        )
    else:
        raise TypeError(f"unsupported semantic object: {type(value).__name__}")
    return "\n\n".join(lines)


def _generated_metadata(generated_at: datetime | None) -> dict[str, str]:
    generated = {"by": "process:selayer-okf"}
    if generated_at is not None:
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone(timedelta(0)))
        generated["at"] = (
            generated_at.astimezone(timezone(timedelta(0)))
            .isoformat()
            .replace("+00:00", "Z")
        )
    return generated


def concepts_from_layer(
    layer: SemanticLayer,
    *,
    generated_at: datetime | None = None,
    include_descriptive: bool = True,
) -> Mapping[str, OkfConcept]:
    concepts: dict[str, OkfConcept] = {}
    for semantic_id, value in layer.semantic_objects().items():
        kind, name = semantic_id.split(".", 1)
        path = concept_path(semantic_id)
        if path.name == "index.md":
            raise ValueError(
                f"semantic object '{semantic_id}' collides with reserved "
                f"index path '{path.as_posix()}'"
            )
        frontmatter: dict[str, object] = {
            "type": _KIND_TYPES[kind],
            "title": display_title(name),
        }
        description = getattr(value, "description", "")
        if include_descriptive and isinstance(description, str) and description:
            frontmatter["description"] = description
        definition = catalog_definition(semantic_id, value)
        generated = _generated_metadata(generated_at)
        frontmatter.update(
            {
                "selayer_id": semantic_id,
                "generated": generated,
            }
        )
        generated["fingerprint"] = generated_fingerprint(frontmatter, definition)
        frontmatter["status"] = "stable"
        sections = (
            OkfSection("Catalog Definition", definition),
            *(OkfSection(title, "") for title in _CURATED_SECTION_TITLES),
        )
        concept_id = path.with_suffix("").as_posix()
        concepts[concept_id] = OkfConcept.create(
            concept_id=concept_id,
            relative_path=path,
            frontmatter=frontmatter,
            sections=sections,
        )
    return MappingProxyType(dict(sorted(concepts.items())))


def index_documents(
    layer: SemanticLayer | None,
    concepts: Mapping[str, OkfConcept],
) -> Mapping[PurePosixPath, str]:
    """Render deterministic root and semantic-kind progressive indexes."""
    grouped: dict[str, list[OkfConcept]] = {
        directory: [] for directory in _KIND_DIRECTORIES.values()
    }
    for concept_id in sorted(concepts):
        concept = concepts[concept_id]
        top = concept.relative_path.parts[0]
        if top in grouped:
            grouped[top].append(concept)

    root_title = (
        (layer.label or display_title(layer.name)) if layer is not None else "Knowledge"
    )
    root_parts = [f'---\nokf_version: "0.2"\n---\n\n# {root_title}']
    if layer is not None and layer.description:
        root_parts.append(layer.description)
    documents: dict[PurePosixPath, str] = {}
    for directory in _KIND_DIRECTORIES.values():
        entries = grouped[directory]
        if not entries:
            continue
        heading = display_title(directory)
        root_links = "\n".join(
            f"- [{concept.frontmatter['title']}]({concept.relative_path.as_posix()})"
            for concept in entries
        )
        root_parts.append(f"# {heading}\n\n{root_links}")
        local_links = "\n".join(
            f"- [{concept.frontmatter['title']}]({concept.relative_path.name})"
            for concept in entries
        )
        documents[PurePosixPath(directory, "index.md")] = (
            f"# {heading}\n\n{local_links}\n"
        )
    documents[PurePosixPath("index.md")] = "\n\n".join(root_parts) + "\n"
    return MappingProxyType(dict(sorted(documents.items(), key=lambda item: item[0])))


__all__ = [
    "catalog_definition",
    "concept_path",
    "concepts_from_layer",
    "display_title",
    "generated_directories",
    "index_documents",
]
