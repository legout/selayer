# Copilot Instructions for `selayer`

## Project Overview
- **Purpose:** This library implements a semantic layer for analytics, enabling users to define data sources, facts, measures, dimensions, metrics, and relationships in a structured, declarative way.
- **Core Components:**
  - `src/selayer/__init__.py`: Main logic. Defines all core dataclasses (DataSource, Fact, Measure, Dimension, Hierarchy, Metric, Relationship, SemanticLayer) and the `QueryEngine` for querying data.
  - `data/`: Example datasets in Parquet and CSV formats for local development and testing.
  - `examples/e_commerce/`: Example scripts for generating data and using the semantic layer.
  - `ecommerce_semantic_layer.yaml`/`.mermaid`: Example semantic model and diagram.

## Key Patterns & Conventions
- **Data Modeling:**
  - Use dataclasses for all semantic objects. Each object (Fact, Measure, etc.) is referenced by name, not by direct object reference.
  - Relationships between tables are defined explicitly in the `relationships` dict of `SemanticLayer`.
- **Querying:**
  - Use `QueryEngine` to run metric/dimension queries. It auto-generates SQL (DuckDB) and handles joins based on relationships.
  - Metrics reference measures by name; measures reference facts by name.
- **Serialization:**
  - Semantic models can be loaded/saved as YAML or JSON via `SemanticLayer.to_yaml()`, `to_json()`, `from_yaml()`, `from_json()`, and `save()`/`load()`.
  - Mermaid diagrams can be generated with `SemanticLayer.to_mermaid()`.

## Developer Workflow
- **Dependencies:**
  - Main: `polars`, `duckdb`, `pyyaml` (for YAML), `dataclasses` (Python 3.7+)
  - Install with: `pip install polars duckdb pyyaml`
- **Testing:**
  - No formal test suite is present. Use scripts in `examples/` for manual testing and prototyping.
- **Extending:**
  - Add new data types or sources by extending `DataSource.get_data()`.
  - Add new aggregation types by updating `Measure.to_sql()`.

## Integration Points
- **External Data:**
  - Supports Parquet, CSV, SQLite, and (prototype) Postgres via DuckDB connectors.
- **Visualization:**
  - Generates Mermaid ER diagrams for model visualization.

## Examples
- See `examples/e_commerce/selayer1.py` for a full example of model definition and querying.
- See `ecommerce_semantic_layer.yaml` for a declarative model definition.

## Project Structure Reference
- `src/selayer/__init__.py`: All core logic and data structures
- `data/`: Example datasets
- `examples/`: Usage and data generation scripts
- `ecommerce_semantic_layer.yaml`: Example model
- `ecommerce_semantic_layer.mermaid`: Example diagram

---

**For AI agents:**
- Always use the dataclass-based API for model construction.
- When adding new features, follow the naming and referencing conventions (by name, not object).
- Prefer extending existing dataclasses and methods over introducing new patterns.
- Reference this file for project-specific conventions before generating code or documentation.
