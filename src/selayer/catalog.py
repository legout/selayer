from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from selayer.model import (
    DataSource,
    Dimension,
    Fact,
    Hierarchy,
    Measure,
    Metric,
    Relationship,
)


@dataclass
class SemanticLayer:
    """A serializable collection of semantic model definitions."""

    name: str
    description: str
    data_sources: dict[str, DataSource] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    hierarchies: dict[str, Hierarchy] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)

    def add_data_source(self, data_source: DataSource) -> None:
        self.data_sources[data_source.name] = data_source

    def add_fact(self, fact: Fact) -> None:
        self.facts[fact.name] = fact

    def add_measure(self, measure: Measure) -> None:
        self.measures[measure.name] = measure

    def add_dimension(self, dimension: Dimension) -> None:
        self.dimensions[dimension.name] = dimension

    def add_hierarchy(self, hierarchy: Hierarchy) -> None:
        self.hierarchies[hierarchy.name] = hierarchy

    def add_metric(self, metric: Metric) -> None:
        self.metrics[metric.name] = metric

    def add_relationship(self, relationship: Relationship) -> None:
        self.relationships[relationship.name] = relationship

    def to_dict(self) -> dict[str, Any]:
        """Convert this catalog to primitive Python values."""
        return {
            "name": self.name,
            "description": self.description,
            "data_sources": {
                key: asdict(value) for key, value in self.data_sources.items()
            },
            "facts": {key: asdict(value) for key, value in self.facts.items()},
            "measures": {key: asdict(value) for key, value in self.measures.items()},
            "dimensions": {
                key: asdict(value) for key, value in self.dimensions.items()
            },
            "hierarchies": {
                key: asdict(value) for key, value in self.hierarchies.items()
            },
            "metrics": {key: asdict(value) for key, value in self.metrics.items()},
            "relationships": {
                key: asdict(value) for key, value in self.relationships.items()
            },
        }

    def to_yaml(self) -> str:
        """Serialize this catalog as YAML."""
        content = yaml.safe_dump(self.to_dict(), sort_keys=False)
        if not isinstance(content, str):
            raise TypeError("YAML serializer did not return text")
        return content

    def to_json(self) -> str:
        """Serialize this catalog as formatted JSON."""
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: str, format: str = "yaml") -> None:
        """Save this catalog as YAML or JSON."""
        serializers = {"yaml": self.to_yaml, "json": self.to_json}
        try:
            content = serializers[format]()
        except KeyError as exc:
            raise ValueError(f"Unsupported format: {format}") from exc
        Path(path).write_text(content, encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticLayer:
        """Construct a catalog from primitive Python values."""
        layer = cls(name=data["name"], description=data["description"])
        for item in data.get("data_sources", {}).values():
            layer.add_data_source(DataSource(**item))
        for item in data.get("facts", {}).values():
            layer.add_fact(Fact(**item))
        for item in data.get("measures", {}).values():
            layer.add_measure(Measure(**item))
        for item in data.get("dimensions", {}).values():
            layer.add_dimension(Dimension(**item))
        for item in data.get("hierarchies", {}).values():
            layer.add_hierarchy(Hierarchy(**item))
        for item in data.get("metrics", {}).values():
            layer.add_metric(Metric(**item))
        for item in data.get("relationships", {}).values():
            layer.add_relationship(Relationship(**item))
        return layer

    @classmethod
    def from_yaml(cls, yaml_string: str) -> SemanticLayer:
        """Construct a catalog from YAML."""
        data = yaml.safe_load(yaml_string)
        if not isinstance(data, dict):
            raise TypeError("semantic layer YAML must contain a mapping")
        return cls.from_dict(data)

    @classmethod
    def from_json(cls, json_string: str) -> SemanticLayer:
        """Construct a catalog from JSON."""
        try:
            data = json.loads(json_string)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid semantic layer JSON") from exc
        if not isinstance(data, dict):
            raise TypeError("semantic layer JSON must contain an object")
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: str) -> SemanticLayer:
        """Load a catalog from a YAML or JSON file."""
        catalog_path = Path(path)
        content = catalog_path.read_text(encoding="utf-8")
        suffix = catalog_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return cls.from_yaml(content)
        if suffix == ".json":
            return cls.from_json(content)
        raise ValueError(f"Unsupported file format: {path}")

    def to_mermaid(self) -> str:
        """Generate a Mermaid entity-relationship diagram."""
        mermaid = ["erDiagram"]
        self._append_sources(mermaid)
        self._append_facts(mermaid)
        self._append_measures(mermaid)
        self._append_dimensions(mermaid)
        self._append_metrics(mermaid)
        self._append_relationships(mermaid)
        return "\n".join(mermaid)

    def _append_sources(self, mermaid: list[str]) -> None:
        for name, data_source in self.data_sources.items():
            mermaid.extend(
                [
                    f"    {name} {{",
                    f'        string type "{data_source.type}"',
                    f'        string path "{data_source.path}"',
                    "    }",
                ]
            )

    def _append_facts(self, mermaid: list[str]) -> None:
        for name, fact in self.facts.items():
            mermaid.extend(
                [
                    f"    {name} {{",
                    f'        string description "{fact.description}"',
                    f'        string dataType "{fact.data_type}"',
                    f'        string source "{fact.source}"',
                    f'        bool isAdditive "{fact.is_additive}"',
                    "    }",
                    f"    {fact.source} ||--o{{ {name} : contains",
                ]
            )

    def _append_measures(self, mermaid: list[str]) -> None:
        for name, measure in self.measures.items():
            mermaid.extend(
                [
                    f"    {name} {{",
                    f'        string description "{measure.description}"',
                    f'        string fact "{measure.fact}"',
                    f'        string aggregation "{measure.aggregation}"',
                    "    }",
                    f"    {measure.fact} ||--o{{ {name} : aggregates",
                ]
            )

    def _append_dimensions(self, mermaid: list[str]) -> None:
        for name, dimension in self.dimensions.items():
            mermaid.extend(
                [
                    f"    {name} {{",
                    f'        string description "{dimension.description}"',
                    f'        string dataType "{dimension.data_type}"',
                    f'        string source "{dimension.source}"',
                    "    }",
                    f"    {dimension.source} ||--o{{ {name} : contains",
                ]
            )

    def _append_metrics(self, mermaid: list[str]) -> None:
        for name, metric in self.metrics.items():
            mermaid.extend(
                [
                    f"    {name} {{",
                    f'        string description "{metric.description}"',
                    "    }",
                ]
            )
            for measure in metric.measures:
                mermaid.append(f"    {measure} ||--o{{ {name} : used_in")
            for dependency in metric.dependencies:
                mermaid.append(f"    {dependency} ||--o{{ {name} : depends_on")

    def _append_relationships(self, mermaid: list[str]) -> None:
        relation_types = {
            "one_to_one": "||--||",
            "one_to_many": "||--o{",
            "many_to_one": "}o--||",
            "many_to_many": "}o--o{",
        }
        for name, relationship in self.relationships.items():
            relation_type = relation_types.get(relationship.type, "||--o{")
            mermaid.append(
                f"    {relationship.source} {relation_type} "
                f'{relationship.target} : "{name}"'
            )
