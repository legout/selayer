# semantic_layer.py
from __future__ import annotations
from typing import Dict, List, Optional, Any, Union, Callable, Literal
from dataclasses import dataclass, field, asdict
import yaml
import json
import os
import polars as pl
import duckdb

@dataclass
class DataSource:
    name: str
    type: str  # "parquet", "csv", "delta", "iceberg", "postgres", "sqlite"
    path: str
    schema: Optional[Dict[str, str]] = None
    connection_params: Optional[Dict[str, Any]] = None
    
    def get_data(self) -> pl.DataFrame:
        """Load data from the source into a Polars DataFrame"""
        if self.type == "parquet":
            return pl.read_parquet(self.path)
        elif self.type == "csv":
            return pl.read_csv(self.path)
        elif self.type in ["postgres", "sqlite"]:
            # This would use connectorx or similar to load from DB
            # For prototype, we'll use DuckDB's SQL capabilities
            conn = duckdb.connect(":memory:")
            if self.type == "postgres":
                # This would require postgres_scanner extension
                conn.execute(f"INSTALL postgres_scanner; LOAD postgres_scanner;")
                conn.execute(f"CREATE TABLE source AS SELECT * FROM postgres_scan('{self.connection_params['connection_string']}', '{self.connection_params['table']}');")
            else:
                conn.execute(f"CREATE TABLE source AS SELECT * FROM sqlite_scan('{self.path}', '{self.connection_params['table']}');")
            
            df = conn.execute("SELECT * FROM source").pl()
            conn.close()
            return df
        else:
            raise ValueError(f"Unsupported data source type: {self.type}")

@dataclass
class Fact:
    """
    A fact represents an atomic data point in a fact table.
    Facts are typically numeric values that can be aggregated.
    """
    name: str
    description: str
    data_type: str  # numeric, integer, decimal, etc.
    source: str  # Reference to a DataSource
    column: str  # Column in the source that contains this fact
    is_additive: bool = True  # Whether this fact can be summed across all dimensions

@dataclass
class Measure:
    """
    A measure is an aggregation of facts (SUM, AVG, COUNT, etc.)
    """
    name: str
    description: str
    fact: str  # Reference to a Fact
    aggregation: Literal["sum", "avg", "min", "max", "count", "count_distinct"] = "sum"
    filter_expression: Optional[str] = None  # Optional filter to apply before aggregation
    
    def to_sql(self) -> str:
        """Convert the measure to a SQL expression"""
        agg_functions = {
            "sum": "SUM",
            "avg": "AVG",
            "min": "MIN",
            "max": "MAX",
            "count": "COUNT",
            "count_distinct": "COUNT(DISTINCT {expr})"
        }
        
        # The column reference will be replaced at query time
        fact_ref = "{fact_source}.{fact_column}"
        
        # Apply filter if specified
        if self.filter_expression:
            filter_clause = f"CASE WHEN {self.filter_expression} THEN {fact_ref} ELSE NULL END"
        else:
            filter_clause = fact_ref
        
        # Apply aggregation
        if self.aggregation == "count_distinct":
            return agg_functions[self.aggregation].format(expr=filter_clause)
        else:
            return f"{agg_functions[self.aggregation]}({filter_clause})"

@dataclass
class Dimension:
    name: str
    description: str
    data_type: str
    source: str  # Reference to a DataSource
    column: str
    hierarchies: List[str] = field(default_factory=list)  # List of hierarchies this dimension belongs to
    
@dataclass
class Hierarchy:
    """
    A hierarchy represents a drill-down path in dimensions
    Example: Year > Quarter > Month > Day
    """
    name: str
    description: str
    levels: List[str]  # List of dimension names in hierarchical order

@dataclass
class Metric:
    name: str
    description: str
    expression: str  # SQL or Python expression
    measures: List[str] = field(default_factory=list)  # List of measures this metric uses
    dependencies: List[str] = field(default_factory=list)  # List of other metrics this metric depends on
    
    def evaluate(self, context: Dict[str, Any], engine: QueryEngine) -> Any:
        """Evaluate the metric using the specified engine"""
        # This is a simplified implementation
        return engine.evaluate_expression(self.expression, context)

@dataclass
class Relationship:
    name: str
    source: str  # Source dimension or data source
    target: str  # Target dimension or data source
    type: str = "one_to_many"  # one_to_one, one_to_many, many_to_one, many_to_many
    source_column: str = ""
    target_column: str = ""

@dataclass
class SemanticLayer:
    name: str
    description: str
    data_sources: Dict[str, DataSource] = field(default_factory=dict)
    facts: Dict[str, Fact] = field(default_factory=dict)
    measures: Dict[str, Measure] = field(default_factory=dict)
    dimensions: Dict[str, Dimension] = field(default_factory=dict)
    hierarchies: Dict[str, Hierarchy] = field(default_factory=dict)
    metrics: Dict[str, Metric] = field(default_factory=dict)
    relationships: Dict[str, Relationship] = field(default_factory=dict)
    
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
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert semantic layer to dictionary"""
        return {
            "name": self.name,
            "description": self.description,
            "data_sources": {k: asdict(v) for k, v in self.data_sources.items()},
            "facts": {k: asdict(v) for k, v in self.facts.items()},
            "measures": {k: asdict(v) for k, v in self.measures.items()},
            "dimensions": {k: asdict(v) for k, v in self.dimensions.items()},
            "hierarchies": {k: asdict(v) for k, v in self.hierarchies.items()},
            "metrics": {k: asdict(v) for k, v in self.metrics.items()},
            "relationships": {k: asdict(v) for k, v in self.relationships.items()},
        }
    
    def to_yaml(self) -> str:
        """Export semantic layer to YAML"""
        return yaml.dump(self.to_dict(), sort_keys=False)
    
    def to_json(self) -> str:
        """Export semantic layer to JSON"""
        return json.dumps(self.to_dict(), indent=2)
    
    def save(self, path: str, format: str = "yaml") -> None:
        """Save semantic layer to file"""
        if format == "yaml":
            with open(path, "w") as f:
                f.write(self.to_yaml())
        elif format == "json":
            with open(path, "w") as f:
                f.write(self.to_json())
        else:
            raise ValueError(f"Unsupported format: {format}")
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SemanticLayer:
        """Create semantic layer from dictionary"""
        semantic_layer = cls(
            name=data["name"],
            description=data["description"]
        )
        
        # Load data sources
        for name, ds_data in data.get("data_sources", {}).items():
            semantic_layer.add_data_source(DataSource(**ds_data))
        
        # Load facts
        for name, fact_data in data.get("facts", {}).items():
            semantic_layer.add_fact(Fact(**fact_data))
            
        # Load measures
        for name, measure_data in data.get("measures", {}).items():
            semantic_layer.add_measure(Measure(**measure_data))
            
        # Load dimensions
        for name, dim_data in data.get("dimensions", {}).items():
            semantic_layer.add_dimension(Dimension(**dim_data))
        
        # Load hierarchies
        for name, hierarchy_data in data.get("hierarchies", {}).items():
            semantic_layer.add_hierarchy(Hierarchy(**hierarchy_data))
            
        # Load metrics
        for name, metric_data in data.get("metrics", {}).items():
            semantic_layer.add_metric(Metric(**metric_data))
            
        # Load relationships
        for name, rel_data in data.get("relationships", {}).items():
            semantic_layer.add_relationship(Relationship(**rel_data))
            
        return semantic_layer
    
    @classmethod
    def from_yaml(cls, yaml_str: str) -> SemanticLayer:
        """Create semantic layer from YAML string"""
        data = yaml.safe_load(yaml_str)
        return cls.from_dict(data)
    
    @classmethod
    def from_json(cls, json_str: str) -> SemanticLayer:
        """Create semantic layer from JSON string"""
        data = json.loads(json_str)
        return cls.from_dict(data)
    
    @classmethod
    def load(cls, path: str) -> SemanticLayer:
        """Load semantic layer from file"""
        with open(path, "r") as f:
            content = f.read()
            
        if path.endswith(".yaml") or path.endswith(".yml"):
            return cls.from_yaml(content)
        elif path.endswith(".json"):
            return cls.from_json(content)
        else:
            raise ValueError(f"Unsupported file format: {path}")
    
    def to_mermaid(self) -> str:
        """Generate Mermaid diagram representation"""
        mermaid = ["erDiagram"]
        
        # Add data sources as entities
        for name, ds in self.data_sources.items():
            mermaid.append(f'    {name} {{')
            mermaid.append(f'        string type "{ds.type}"')
            mermaid.append(f'        string path "{ds.path}"')
            mermaid.append('    }')
        
        # Add facts
        for name, fact in self.facts.items():
            mermaid.append(f'    {name} {{')
            mermaid.append(f'        string description "{fact.description}"')
            mermaid.append(f'        string dataType "{fact.data_type}"')
            mermaid.append(f'        string source "{fact.source}"')
            mermaid.append(f'        bool isAdditive "{fact.is_additive}"')
            mermaid.append('    }')
            
            # Connect facts to their sources
            mermaid.append(f'    {fact.source} ||--o{{ {name} : contains')
        
        # Add measures
        for name, measure in self.measures.items():
            mermaid.append(f'    {name} {{')
            mermaid.append(f'        string description "{measure.description}"')
            mermaid.append(f'        string fact "{measure.fact}"')
            mermaid.append(f'        string aggregation "{measure.aggregation}"')
            mermaid.append('    }')
            
            # Connect measures to their facts
            mermaid.append(f'    {measure.fact} ||--o{{ {name} : aggregates')
        
        # Add dimensions
        for name, dim in self.dimensions.items():
            mermaid.append(f'    {name} {{')
            mermaid.append(f'        string description "{dim.description}"')
            mermaid.append(f'        string dataType "{dim.data_type}"')
            mermaid.append(f'        string source "{dim.source}"')
            mermaid.append('    }')
            
            # Connect dimensions to their sources
            mermaid.append(f'    {dim.source} ||--o{{ {name} : contains')
        
        # Add metrics
        for name, metric in self.metrics.items():
            mermaid.append(f'    {name} {{')
            mermaid.append(f'        string description "{metric.description}"')
            mermaid.append('    }')
            
            # Connect metrics to their measures
            for measure in metric.measures:
                mermaid.append(f'    {measure} ||--o{{ {name} : used_in')
            
            # Connect metrics to their dependencies
            for dep in metric.dependencies:
                mermaid.append(f'    {dep} ||--o{{ {name} : depends_on')
        
        # Add relationships
        for name, rel in self.relationships.items():
            relation_type = {
                "one_to_one": "||--||",
                "one_to_many": "||--o{",
                "many_to_one": "}o--||",
                "many_to_many": "}o--o{"
            }.get(rel.type, "||--o{")
            
            mermaid.append(f'    {rel.source} {relation_type} {rel.target} : "{name}"')
        
        return "\n".join(mermaid)

class QueryEngine:
    def __init__(self, semantic_layer: SemanticLayer, engine_type: str = "duckdb"):
        self.semantic_layer = semantic_layer
        self.engine_type = engine_type
        self.conn = None
        
        if engine_type == "duckdb":
            self.conn = duckdb.connect(":memory:")
            # Register data sources as views
            for name, ds in semantic_layer.data_sources.items():
                df = ds.get_data()
                self.conn.register(name, df)
        elif engine_type == "polars":
            # For Polars, we'll load data when needed
            pass
        else:
            raise ValueError(f"Unsupported engine type: {engine_type}")
    
    def evaluate_expression(self, expression: str, context: Dict[str, Any]) -> Any:
        """Evaluate an expression using the query engine"""
        # Replace placeholders in the expression with context values
        for key, value in context.items():
            if isinstance(value, str):
                expression = expression.replace(f"{{{{{key}}}}}", f"'{value}'")
            else:
                expression = expression.replace(f"{{{{{key}}}}}", str(value))
        
        if self.engine_type == "duckdb":
            result = self.conn.execute(expression).fetchall()
            return result
        elif self.engine_type == "polars":
            # This is simplified - in reality you'd parse the expression
            # and use Polars' API to execute it
            return pl.DataFrame()
    
    def _find_join_path(self, source_tables, target_table):
        """Find a path to join from source_tables to target_table"""
        if target_table in source_tables:
            return []  # Already joined
            
        # Simple BFS to find a join path
        from collections import deque
        visited = set(source_tables)
        queue = deque((table, []) for table in source_tables)
        
        while queue:
            current_table, path = queue.popleft()
            
            # Check all relationships for this table
            for rel_name, rel in self.semantic_layer.relationships.items():
                next_table = None
                join_condition = None
                
                # Check if this relationship connects current_table to an unvisited table
                if rel.source == current_table and rel.target not in visited:
                    next_table = rel.target
                    join_condition = f"{rel.source}.{rel.source_column} = {rel.target}.{rel.target_column}"
                elif rel.target == current_table and rel.source not in visited:
                    next_table = rel.source
                    join_condition = f"{rel.target}.{rel.target_column} = {rel.source}.{rel.source_column}"
                
                if next_table:
                    if next_table == target_table:
                        # Found a path to the target
                        return path + [(current_table, next_table, join_condition)]
                    
                    visited.add(next_table)
                    queue.append((next_table, path + [(current_table, next_table, join_condition)]))
        
        # No path found
        return None
    
    def query(self, metrics: List[str], dimensions: List[str] = None, 
              filters: Dict[str, Any] = None) -> pl.DataFrame:
        """
        Execute a query against the semantic layer
        
        Args:
            metrics: List of metric names to include
            dimensions: List of dimension names to group by
            filters: Dictionary of filters to apply
            
        Returns:
            DataFrame with query results
        """
        dimensions = dimensions or []
        filters = filters or {}
        
        # Build SQL query
        select_clauses = []
        
        # Determine required tables
        required_tables = set()
        fact_tables = set()
        dimension_tables = set()
        
        # Add tables needed for dimensions
        for dim_name in dimensions:
            if dim_name in self.semantic_layer.dimensions:
                dim = self.semantic_layer.dimensions[dim_name]
                dimension_tables.add(dim.source)
                required_tables.add(dim.source)
        
        # Add tables needed for metrics
        for metric_name in metrics:
            if metric_name in self.semantic_layer.metrics:
                metric = self.semantic_layer.metrics[metric_name]
                
                # Add tables from measures
                for measure_name in metric.measures:
                    if measure_name in self.semantic_layer.measures:
                        measure = self.semantic_layer.measures[measure_name]
                        fact = self.semantic_layer.facts[measure.fact]
                        fact_tables.add(fact.source)
                        required_tables.add(fact.source)
        
        # Add dimensions to select
        for dim_name in dimensions:
            if dim_name in self.semantic_layer.dimensions:
                dim = self.semantic_layer.dimensions[dim_name]
                select_clauses.append(f"{dim.source}.{dim.column} as {dim_name}")
        
        # Process metrics
        for metric_name in metrics:
            if metric_name in self.semantic_layer.metrics:
                metric = self.semantic_layer.metrics[metric_name]
                expression = metric.expression
                
                # Replace measure placeholders in the metric expression
                for measure_name in metric.measures:
                    if measure_name in self.semantic_layer.measures:
                        measure = self.semantic_layer.measures[measure_name]
                        fact = self.semantic_layer.facts[measure.fact]
                        
                        # Replace placeholders in the measure SQL
                        measure_sql = measure.to_sql().replace("{fact_source}", fact.source).replace("{fact_column}", fact.column)
                        expression = expression.replace(f"{{{{{measure_name}}}}}", f"({measure_sql})")
                
                select_clauses.append(f"({expression}) as {metric_name}")
        
        # Determine FROM clause and JOINs
        if not required_tables:
            raise ValueError("No tables required for this query")
        
        # Start with the first table
        from_table = next(iter(required_tables))
        joined_tables = {from_table}
        join_clauses = []
        
        # Add necessary joins
        for table in required_tables - {from_table}:
            if table not in joined_tables:
                # Find a path to join this table
                path = self._find_join_path(joined_tables, table)
                if path:
                    for source, target, condition in path:
                        join_clauses.append(f"JOIN {target} ON {condition}")
                        joined_tables.add(target)
                else:
                    # No path found, use a cross join as fallback
                    join_clauses.append(f"CROSS JOIN {table}")
                    joined_tables.add(table)
        
        # Build WHERE clause from filters
        where_clauses = []
        for filter_name, filter_value in filters.items():
            # Find the table for this filter
            table = None
            column = filter_name
            
            # Check if this is a dimension
            for dim_name, dim in self.semantic_layer.dimensions.items():
                if dim_name == filter_name:
                    table = dim.source
                    column = dim.column
                    break
            
            if not table:
                # Assume it's a column in one of the required tables
                # This is simplified - in reality you'd need more robust logic
                table = from_table
            
            # Build the filter clause
            if isinstance(filter_value, tuple) and len(filter_value) == 2:
                # Range filter
                where_clauses.append(f"{table}.{column} BETWEEN '{filter_value[0]}' AND '{filter_value[1]}'")
            elif isinstance(filter_value, list):
                # IN filter
                values = ", ".join([f"'{v}'" if isinstance(v, str) else str(v) for v in filter_value])
                where_clauses.append(f"{table}.{column} IN ({values})")
            else:
                # Equality filter
                if isinstance(filter_value, str):
                    where_clauses.append(f"{table}.{column} = '{filter_value}'")
                else:
                    where_clauses.append(f"{table}.{column} = {filter_value}")
        
        # Build GROUP BY clause
        group_by_clause = ", ".join([f"{dim_name}" for dim_name in dimensions])
        
        # Assemble query
        query = f"SELECT {', '.join(select_clauses)} FROM {from_table}"
        if join_clauses:
            query += f" {' '.join(join_clauses)}"
        if where_clauses:
            query += f" WHERE {' AND '.join(where_clauses)}"
        if dimensions:
            query += f" GROUP BY {group_by_clause}"
        
        print(f"Generated SQL: {query}")
        
        # Execute query
        if self.engine_type == "duckdb":
            result = self.conn.execute(query).pl()
            return result
        elif self.engine_type == "polars":
            # In reality, you'd implement this using Polars' API
            return pl.DataFrame()