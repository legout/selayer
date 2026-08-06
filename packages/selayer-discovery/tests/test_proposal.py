"""Typed semantic proposal operation, reconstruction, and preview tests.

These tests pin the Task 16 typed-proposal contract:

* :class:`~selayer_discovery.proposal.Operation` carries a complete normalized
  before/after state, a fully-qualified target id, a before-state hash, claim
  ids, and dependency-group ids. Prohibited mutations (delete, rename, target
  kind change, edit outside the target object, arbitrary patch input, generated
  OKF target, path escape, generated frontmatter or ``Catalog Definition``
  overlay edit) are rejected.
* :func:`~selayer_discovery.proposal.reconstruct_candidate` round-trips the base
  catalog through ``ruamel.yaml`` so comments, key order outside changed
  objects, quoting, and newline style are preserved, and the reconstructed
  catalog loads to the exact expected :class:`~selayer.model.SemanticLayer`.
* Impact flags and changed-field sets are derived from the normalized
  before/after state and never read from an agent-supplied impact list.
* Atomic dependency groups reject cycles and carry rationale, current
  non-inferred claims, affecting gates, conflict ids, query cases, operations,
  and dependencies.
* :func:`~selayer_discovery.proposal.render_review_preview` renders a
  deterministic ``catalog.patch`` and knowledge diff. Previews are never verify
  or apply authority.
* The ``proposal import`` and ``proposal show`` CLI commands reconstruct a
  candidate, render previews, and emit deterministic JSON.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from ruamel.yaml import YAML
from selayer_discovery import canonical
from selayer_discovery.proposal import (
    CATALOG_COLLECTION_BY_KIND,
    GENESIS_HASH,
    KnowledgeSubject,
    Operation,
    OperationKind,
    ProposalError,
    QueryCase,
    ReviewPreview,
    build_proposal,
    reconstruct_candidate,
    render_review_preview,
    write_candidate,
)

from selayer.catalog import SemanticLayer

# --------------------------------------------------------------------------- #
# Catalog fixtures                                                            #
# --------------------------------------------------------------------------- #

_BASE_CATALOG_YAML = """\
# Canonical shopfloor catalog for proposal reconstruction tests.
version: 1
name: shopfloor
label: Shopfloor Analytics
description: Semantic model for the shopfloor example
data_sources:
  orders:
    type: parquet
    location: {location}
    grain: [id]
    schema:
      fields:
        - {{name: id, type: utf8, nullable: false}}
        - {{name: customer_id, type: utf8, nullable: true}}
        - {{name: status, type: utf8, nullable: true}}
        - {{name: amount, type: float64, nullable: true}}
  products:
    type: parquet
    location: {location_products}
    grain: [id]
    schema:
      fields:
        - {{name: id, type: utf8, nullable: false}}
        - {{name: category, type: utf8, nullable: true}}
        - {{name: cost, type: float64, nullable: true}}
dimensions:
  order_status:
    source: orders
    column: status
    data_type: string
    description: Order status
facts:
  order_amount:
    source: orders
    expression: orders.amount
    data_type: decimal
    description: Order amount
measures:
  total_order_amount:
    fact: order_amount
    aggregation: sum
    description: Total order amount
relationships:
  product_orders:
    source: products
    target: orders
    type: one_to_many
    source_column: id
    target_column: customer_id
"""


def _write_parquet(path: Path) -> None:
    table = pa.Table.from_pydict(
        {
            "id": ["O1", "O2"],
            "customer_id": ["P1", "P2"],
            "status": ["completed", "open"],
            "amount": [10.0, 20.0],
        },
        schema=pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("customer_id", pa.string()),
                pa.field("status", pa.string(), nullable=False),
                pa.field("amount", pa.float64()),
            ]
        ),
    )
    pq.write_table(table, path)


def _write_products_parquet(path: Path) -> None:
    table = pa.Table.from_pydict(
        {
            "id": ["P1", "P2"],
            "category": ["widgets", "gadgets"],
            "cost": [1.0, 2.0],
        },
        schema=pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("category", pa.string()),
                pa.field("cost", pa.float64()),
            ]
        ),
    )
    pq.write_table(table, path)


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    """A directory with parquet sources and a valid base catalog."""

    data = tmp_path / "data"
    data.mkdir()
    orders = data / "orders.parquet"
    products = data / "products.parquet"
    _write_parquet(orders)
    _write_products_parquet(products)
    text = _BASE_CATALOG_YAML.format(
        location=str(orders), location_products=str(products)
    )
    (tmp_path / "catalog.yaml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def base_catalog_path(catalog_dir: Path) -> Path:
    return catalog_dir / "catalog.yaml"


@pytest.fixture
def base_catalog_text(base_catalog_path: Path) -> str:
    return base_catalog_path.read_text(encoding="utf-8")


@pytest.fixture
def base_layer(base_catalog_path: Path) -> SemanticLayer:
    return SemanticLayer.load(base_catalog_path)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _hash(value: object) -> str:
    return canonical.fingerprint(value)


def _op(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid catalog.add operation mapping."""

    base: dict[str, Any] = {
        "operation_id": "operation-001",
        "kind": "catalog.add",
        "target_id": "dimension.product_category",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": {
            "source": "products",
            "column": "category",
            "data_type": "string",
            "description": "Product category",
        },
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _catalog_edit_op(**overrides: Any) -> dict[str, Any]:
    before = {
        "source": "orders",
        "column": "status",
        "data_type": "string",
        "description": "Order status",
    }
    after = {
        "source": "orders",
        "column": "status",
        "data_type": "string",
        "description": "Order status (revised)",
    }
    base: dict[str, Any] = {
        "operation_id": "operation-edit",
        "kind": "catalog.edit",
        "target_id": "dimension.order_status",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _group(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "group_id": "group-001",
        "title": "Add product category dimension",
        "rationale": "Products need a category dimension for filtering.",
        "dependencies": [],
        "supporting_claim_ids": ["claim-c1"],
        "inferred_claim_ids": [],
        "conflict_ids": [],
        "affecting_gates": ["gate-grains"],
        "query_cases": [],
        "operations": [_op()],
    }
    base.update(overrides)
    return base


def _proposal_mapping(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "proposal_id": "proposal-001",
        "title": "Add product category",
        "groups": [_group()],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Step 1: operation-schema acceptance                                         #
# --------------------------------------------------------------------------- #


class TestOperationSchema:
    def test_catalog_add_carries_complete_state(self) -> None:
        op = Operation.from_mapping(_op())
        assert op.kind is OperationKind.CATALOG_ADD
        assert op.target_id == "dimension.product_category"
        assert op.before is None
        assert op.before_hash == GENESIS_HASH
        assert op.after == {
            "source": "products",
            "column": "category",
            "data_type": "string",
            "description": "Product category",
        }
        assert op.claim_ids == ("claim-c1",)
        assert op.group_ids == ("group-001",)

    def test_catalog_edit_carries_before_after_and_hash(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = dict(before)
        after["description"] = "Revised"
        op = Operation.from_mapping(
            _catalog_edit_op(before=before, after=after, before_hash=_hash(before))
        )
        assert op.kind is OperationKind.CATALOG_EDIT
        assert op.before == before
        assert op.before_hash == _hash(before)
        assert op.after == after

    def test_catalog_deprecate_sets_status(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = dict(before)
        after["status"] = "deprecated"
        after["replaced_by"] = "dimension.order_state"
        op = Operation.from_mapping(
            _catalog_edit_op(
                kind="catalog.deprecate",
                target_id="dimension.order_status",
                before=before,
                after=after,
                before_hash=_hash(before),
            )
        )
        assert op.kind is OperationKind.CATALOG_DEPRECATE

    def test_source_add_rejects_grain_only_state(self) -> None:
        # A source after-state that only carries ``grain`` is incomplete: a
        # strict SemanticLayer load needs a connector, a schema, and a grain.
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="catalog.add",
                    target_id="source.shipping",
                    after={"grain": ["id"]},
                )
            )

    def test_source_add_rejects_grain_without_schema(self) -> None:
        # ``grain`` plus a connector type but no schema is still incomplete.
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="catalog.add",
                    target_id="source.shipping",
                    after={
                        "type": "parquet",
                        "location": "data/shipping.parquet",
                        "grain": ["id"],
                    },
                )
            )

    def test_source_add_accepts_complete_fields(self) -> None:
        after = {
            "type": "parquet",
            "location": "data/shipping.parquet",
            "grain": ["id"],
            "schema": {
                "fields": [
                    {"name": "id", "type": "utf8", "nullable": False}
                ]
            },
        }
        op = Operation.from_mapping(
            _op(kind="catalog.add", target_id="source.shipping", after=after)
        )
        assert op.kind is OperationKind.CATALOG_ADD
        assert op.target_id == "source.shipping"

    def test_reference_create_carries_text_after(self) -> None:
        op = Operation.from_mapping(
            {
                "operation_id": "operation-ref",
                "kind": "reference.create",
                "target_id": "dimensions/order_status.md",
                "before": None,
                "before_hash": GENESIS_HASH,
                "after": "---\nselayer_id: dimension.order_status\n---\n# Body",
                "claim_ids": ["claim-c1"],
                "group_ids": ["group-001"],
            }
        )
        assert op.kind is OperationKind.REFERENCE_CREATE
        assert op.before is None
        assert isinstance(op.after, str)

    def test_overlay_update_carries_text_before_after(self) -> None:
        before = "---\nselayer_id: dimension.order_status\n---\n## Usage Guidance\nOld.\n"
        after = "---\nselayer_id: dimension.order_status\n---\n## Usage Guidance\nNew.\n"
        op = Operation.from_mapping(
            {
                "operation_id": "operation-ovl",
                "kind": "overlay.update",
                "target_id": "dimensions/order_status.md",
                "before": before,
                "before_hash": _hash(before),
                "after": after,
                "claim_ids": ["claim-c1"],
                "group_ids": ["group-001"],
            }
        )
        assert op.kind is OperationKind.OVERLAY_UPDATE
        assert op.before == before

    def test_unknown_operation_key_rejected(self) -> None:
        data = _op(extra_field="nope")
        with pytest.raises(ProposalError):
            Operation.from_mapping(data)

    def test_missing_required_field_rejected(self) -> None:
        data = _op()
        data.pop("target_id")
        with pytest.raises(ProposalError):
            Operation.from_mapping(data)

    def test_oversized_prose_rejected(self) -> None:
        after = {
            "source": "products",
            "column": "category",
            "data_type": "string",
            "description": "x" * (16 * 1024 + 1),
        }
        with pytest.raises(ProposalError):
            Operation.from_mapping(_op(after=after))

    def test_operation_id_must_be_stable(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(_op(operation_id="Bad ID!"))

    def test_before_hash_must_match_normalized_before_for_edit(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _catalog_edit_op(before=before, before_hash="f" * 64)
            )

    def test_claim_ids_must_use_stable_prefix(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(_op(claim_ids=["not-a-claim-id"]))


# --------------------------------------------------------------------------- #
# Step 1: prohibited mutations                                                #
# --------------------------------------------------------------------------- #


class TestProhibitedMutations:
    def test_delete_kind_rejected(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _catalog_edit_op(
                    kind="catalog.delete",
                    before=before,
                    before_hash=_hash(before),
                )
            )

    def test_rename_rejected(self) -> None:
        # There is no ``catalog.rename`` kind: renaming is not a supported
        # mutation. An unknown kind is rejected at construction.
        with pytest.raises(ProposalError):
            Operation.from_mapping(_op(kind="catalog.rename"))

    def test_rename_via_nonexistent_edit_target_rejected_at_reconstruction(
        self, base_catalog_text: str
    ) -> None:
        # An edit targeting an id that does not exist in the base (a rename in
        # disguise) is rejected at reconstruction: the target object is missing.
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = dict(before)
        after["description"] = "Revised"
        op = Operation.from_mapping(
            _catalog_edit_op(
                target_id="dimension.order_state",
                before=before,
                after=after,
                before_hash=_hash(before),
            )
        )
        with pytest.raises(ProposalError):
            reconstruct_candidate(
                base_catalog_text=base_catalog_text,
                base_references={},
                base_overlays={},
                operations=(op,),
            )

    def test_target_kind_change_rejected(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = {
            "source": "orders",
            "expression": "orders.amount",
            "data_type": "decimal",
            "description": "Order status",
        }
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _catalog_edit_op(
                    kind="catalog.edit",
                    target_id="fact.order_status",
                    before=before,
                    after=after,
                    before_hash=_hash(before),
                )
            )

    def test_edit_outside_target_object_rejected(self) -> None:
        # before/after describe two objects in one operation.
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = {
            "source": "orders",
            "column": "amount",
            "data_type": "string",
            "description": "Order status",
        }
        # target_id points at one object but after is a different column shape
        # that cannot belong to the same identifier family.
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _catalog_edit_op(
                    before=before,
                    after=after,
                    before_hash=_hash(before),
                )
            )

    def test_arbitrary_patch_input_rejected(self) -> None:
        # A JSON-patch style op rather than complete normalized state.
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(after={"op": "replace", "path": "/description", "value": "x"})
            )

    def test_generated_okf_target_rejected(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="reference.create",
                    target_id="generated/dimensions/order_status.md",
                    after="text",
                )
            )

    def test_path_escape_rejected(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="reference.create",
                    target_id="../escape.md",
                    after="text",
                )
            )

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="reference.create",
                    target_id="/etc/passwd",
                    after="text",
                )
            )

    def test_overlay_generated_frontmatter_rejected(self) -> None:
        after = (
            "---\n"
            "selayer_id: dimension.order_status\n"
            "type: dimension\n"          # generated field
            "title: Order status\n"      # generated field
            "---\n"
            "## Usage Guidance\n"
            "Guidance.\n"
        )
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="overlay.create",
                    target_id="dimensions/order_status.md",
                    after=after,
                )
            )

    def test_overlay_catalog_definition_section_rejected(self) -> None:
        after = (
            "---\n"
            "selayer_id: dimension.order_status\n"
            "---\n"
            "## Catalog Definition\n"
            "type: dimension\n"
            "definition:\n"
            "  source: orders\n"
        )
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="overlay.create",
                    target_id="dimensions/order_status.md",
                    after=after,
                )
            )

    def test_overlay_disallowed_section_rejected(self) -> None:
        after = (
            "---\n"
            "selayer_id: dimension.order_status\n"
            "---\n"
            "## Random Section\n"
            "not allowed.\n"
        )
        with pytest.raises(ProposalError):
            Operation.from_mapping(
                _op(
                    kind="overlay.create",
                    target_id="dimensions/order_status.md",
                    after=after,
                )
            )

    def test_unknown_operation_kind_rejected(self) -> None:
        with pytest.raises(ProposalError):
            Operation.from_mapping(_op(kind="catalog.rename"))


# --------------------------------------------------------------------------- #
# Step 3: derived impacts and changed fields                                  #
# --------------------------------------------------------------------------- #


class TestDerivedImpacts:
    def test_changed_fields_derived_from_before_after(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Revised",
        }
        op = Operation.from_mapping(
            _catalog_edit_op(before=before, after=after, before_hash=_hash(before))
        )
        assert op.changed_fields == ("description",)

    def test_impact_flags_derived_not_from_agent(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = {
            "source": "orders",
            "column": "status",
            "data_type": "integer",
            "description": "Order status",
        }
        op = Operation.from_mapping(
            _catalog_edit_op(before=before, after=after, before_hash=_hash(before))
        )
        assert "type_changed" in op.impacts
        # Agent never supplies impacts; they are read-only.
        assert not hasattr(op, "agent_impacts")

    def test_deprecate_derives_id_deprecated_impact(self) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = dict(before)
        after["status"] = "deprecated"
        after["replaced_by"] = "dimension.order_state"
        op = Operation.from_mapping(
            _catalog_edit_op(
                kind="catalog.deprecate",
                before=before,
                after=after,
                before_hash=_hash(before),
            )
        )
        assert "id_deprecated" in op.impacts

    def test_add_derives_object_added_impact(self) -> None:
        op = Operation.from_mapping(_op())
        assert "object_added" in op.impacts

    def test_ignored_agent_impact_list(self) -> None:
        # An agent-supplied impacts field is ignored entirely.
        data = _op()
        data["impacts"] = ["something_made_up"]
        op = Operation.from_mapping(data)
        assert "something_made_up" not in op.impacts


# --------------------------------------------------------------------------- #
# Step 2: candidate reconstruction                                            #
# --------------------------------------------------------------------------- #


class TestCandidateReconstruction:
    def test_catalog_add_reconstructs_and_loads(
        self, base_catalog_text: str, base_layer: SemanticLayer, catalog_dir: Path
    ) -> None:
        op = Operation.from_mapping(_op())
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        path = write_candidate(candidate, catalog_dir / "candidate")
        layer = SemanticLayer.load(path)
        assert "product_category" in layer.dimensions
        # Untouched objects remain identical.
        assert layer.facts.keys() == base_layer.facts.keys()

    def test_round_trip_preserves_comments_outside_changed_objects(
        self, base_catalog_text: str
    ) -> None:
        op = Operation.from_mapping(_op())
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        text = candidate.catalog_text
        # The leading comment is preserved verbatim.
        assert text.startswith("# Canonical shopfloor catalog")
        # The label and description lines are untouched.
        assert "label: Shopfloor Analytics" in text

    def test_round_trip_preserves_quoting_and_order(
        self, base_catalog_text: str
    ) -> None:
        op = Operation.from_mapping(_op())
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        after_text = candidate.catalog_text
        # The unchanged data-source collection keys keep their original order
        # (orders before products).
        orders_idx = after_text.index("  orders:")
        products_idx = after_text.index("  products:")
        assert orders_idx < products_idx
        # Flow-style field lines (quoting/brace style) are preserved on the
        # untouched orders source.
        assert "{name: id" in after_text
        # Leading top-of-file comment is preserved verbatim.
        assert after_text.startswith("# Canonical shopfloor catalog")

    def test_catalog_edit_reconstructs_description_change(
        self, base_catalog_text: str, catalog_dir: Path
    ) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status (revised)",
        }
        op = Operation.from_mapping(
            _catalog_edit_op(before=before, after=after, before_hash=_hash(before))
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        path = write_candidate(candidate, catalog_dir / "candidate")
        layer = SemanticLayer.load(path)
        assert layer.dimension("order_status").description == "Order status (revised)"

    def test_catalog_deprecate_reconstructs_status(
        self, base_catalog_text: str, catalog_dir: Path
    ) -> None:
        before = {
            "source": "orders",
            "column": "status",
            "data_type": "string",
            "description": "Order status",
        }
        after = dict(before)
        after["status"] = "deprecated"
        after["replaced_by"] = "dimension.order_state"
        op = Operation.from_mapping(
            _catalog_edit_op(
                kind="catalog.deprecate",
                before=before,
                after=after,
                before_hash=_hash(before),
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        path = write_candidate(candidate, catalog_dir / "candidate")
        layer = SemanticLayer.load(path)
        dim = layer.dimension("order_status")
        assert dim.status.value == "deprecated"
        assert dim.replaced_by == "dimension.order_state"

    def test_reference_create_reconstructs_text(
        self, base_catalog_text: str
    ) -> None:
        after = "---\nselayer_id: dimension.order_status\n---\n# Order status\n"
        op = Operation.from_mapping(
            _op(
                kind="reference.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        ref = dict(candidate.references)
        assert ref["dimensions/order_status.md"] == after

    def test_overlay_create_reconstructs_text(
        self, base_catalog_text: str
    ) -> None:
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nUse this for status.\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="overlay.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        ovl = dict(candidate.overlays)
        assert ovl["dimensions/order_status.md"] == after

    def test_reference_operation_never_appears_in_overlays(
        self, base_catalog_text: str
    ) -> None:
        # A reference operation must only affect the references subject; it
        # must never leak into the overlay base.
        after = "---\nselayer_id: dimension.order_status\n---\n# Order status\n"
        op = Operation.from_mapping(
            _op(
                kind="reference.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        assert "dimensions/order_status.md" in dict(candidate.references)
        assert "dimensions/order_status.md" not in dict(candidate.overlays)

    def test_overlay_operation_never_appears_in_references(
        self, base_catalog_text: str
    ) -> None:
        # An overlay operation must only affect the overlay subject; it must
        # never leak into the reference base.
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nUse this for status.\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="overlay.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        assert "dimensions/order_status.md" in dict(candidate.overlays)
        assert "dimensions/order_status.md" not in dict(candidate.references)

    def test_reference_update_against_nonempty_authored_base(
        self, base_catalog_text: str
    ) -> None:
        before = "---\nselayer_id: dimension.order_status\n---\n# Old\n"
        after = "---\nselayer_id: dimension.order_status\n---\n# New\n"
        op = Operation.from_mapping(
            _op(
                kind="reference.update",
                target_id="dimensions/order_status.md",
                before=before,
                before_hash=_hash(before),
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={"dimensions/order_status.md": before},
            base_overlays={},
            operations=(op,),
        )
        refs = dict(candidate.references)
        assert refs["dimensions/order_status.md"] == after
        # The overlay subject is untouched.
        assert dict(candidate.overlays) == {}

    def test_overlay_update_against_nonempty_authored_base(
        self, base_catalog_text: str
    ) -> None:
        before = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nOld.\n"
        )
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nNew.\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="overlay.update",
                target_id="dimensions/order_status.md",
                before=before,
                before_hash=_hash(before),
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={"dimensions/order_status.md": before},
            operations=(op,),
        )
        ovls = dict(candidate.overlays)
        assert ovls["dimensions/order_status.md"] == after
        # The reference subject is untouched.
        assert dict(candidate.references) == {}

    def test_candidate_has_stable_fingerprint(
        self, base_catalog_text: str
    ) -> None:
        op = Operation.from_mapping(_op())
        first = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        second = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        assert first.fingerprint == second.fingerprint

    def test_reconstruction_preserves_crlf_newlines(
        self, base_catalog_text: str
    ) -> None:
        # A base catalog authored with CRLF line endings must be preserved on
        # round-trip so a candidate never silently normalizes authored style.
        crlf_text = base_catalog_text.replace("\n", "\r\n")
        op = Operation.from_mapping(_op())
        candidate = reconstruct_candidate(
            base_catalog_text=crlf_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        assert "\r\n" in candidate.catalog_text
        # No lone LF introduced where CRLF was authored (no silent normalization).
        assert "\n" not in candidate.catalog_text.replace("\r\n", "")


# --------------------------------------------------------------------------- #
# Step 4: atomic dependency groups                                            #
# --------------------------------------------------------------------------- #


class TestDependencyGroups:
    def test_group_carries_required_fields(self) -> None:
        proposal = build_proposal(_proposal_mapping())
        group = proposal.groups[0]
        assert group.group_id == "group-001"
        assert group.rationale
        assert group.supporting_claim_ids == ("claim-c1",)
        assert group.affecting_gates == ("gate-grains",)
        assert group.conflict_ids == ()
        assert len(group.operations) == 1

    def test_dependency_cycle_rejected(self) -> None:
        g1 = _group(
            group_id="group-001",
            dependencies=["group-002"],
        )
        g2 = _group(
            group_id="group-002",
            dependencies=["group-001"],
            operations=[
                _op(
                    operation_id="operation-002",
                    target_id="dimension.product_category",
                )
            ],
        )
        with pytest.raises(ProposalError):
            build_proposal(_proposal_mapping(groups=[g1, g2]))

    def test_self_dependency_rejected(self) -> None:
        g1 = _group(dependencies=["group-001"])
        with pytest.raises(ProposalError):
            build_proposal(_proposal_mapping(groups=[g1]))

    def test_unknown_dependency_rejected(self) -> None:
        g1 = _group(dependencies=["group-missing"])
        with pytest.raises(ProposalError):
            build_proposal(_proposal_mapping(groups=[g1]))

    def test_operations_sorted_stably_within_proposal(self) -> None:
        g1 = _group(
            operations=[
                _op(operation_id="operation-zeta"),
                _op(operation_id="operation-alpha", target_id="dimension.alpha"),
            ]
        )
        proposal = build_proposal(_proposal_mapping(groups=[g1]))
        ids = [op.operation_id for op in proposal.operations]
        assert ids == sorted(ids)

    def test_query_cases_carried(self) -> None:
        g1 = _group(
            query_cases=[
                {
                    "case_id": "case-001",
                    "kind": "compatible_plan",
                    "description": "Plan accepts the new dimension.",
                }
            ]
        )
        proposal = build_proposal(_proposal_mapping(groups=[g1]))
        assert len(proposal.groups[0].query_cases) == 1
        assert isinstance(proposal.groups[0].query_cases[0], QueryCase)


# --------------------------------------------------------------------------- #
# Step 5: deterministic review previews                                       #
# --------------------------------------------------------------------------- #


class TestReviewPreview:
    def test_catalog_patch_is_deterministic(self, base_catalog_text: str) -> None:
        op = Operation.from_mapping(_op())
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        preview = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            candidate=candidate,
        )
        assert isinstance(preview, ReviewPreview)
        assert "product_category" in preview.catalog_patch
        first = preview
        second = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            candidate=candidate,
        )
        assert first.fingerprint == second.fingerprint

    def test_reference_diff_rendered(self, base_catalog_text: str) -> None:
        after = "---\nselayer_id: dimension.order_status\n---\n# Order status\n"
        op = Operation.from_mapping(
            _op(
                kind="reference.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        preview = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            candidate=candidate,
        )
        assert any(
            "order_status" in path and diff.strip() for path, diff in preview.reference_diffs
        )


class TestReviewSummary:
    def test_summary_has_no_raw_patch_or_diff_text(
        self, base_catalog_text: str
    ) -> None:
        from selayer_discovery.proposal import render_review_summary

        secret = "LEAKME-body-canary-424242"
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            f"## Usage Guidance\n{secret}\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="overlay.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        preview = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            candidate=candidate,
        )
        summary = render_review_summary(
            base_references={},
            base_overlays={},
            candidate=candidate,
            preview=preview,
        )
        # The raw body, patch, and diff text must never reach the summary.
        import dataclasses

        rendered = json.dumps(
            dataclasses.asdict(summary), sort_keys=True
        )
        assert secret not in rendered
        assert preview.catalog_patch not in rendered
        assert "product_category" not in rendered

    def test_summary_carries_counts_status_and_fingerprints(
        self, base_catalog_text: str
    ) -> None:
        from selayer_discovery.proposal import render_review_summary

        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nUse this for status.\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="overlay.create",
                target_id="dimensions/order_status.md",
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            operations=(op,),
        )
        preview = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={},
            base_overlays={},
            candidate=candidate,
        )
        summary = render_review_summary(
            base_references={},
            base_overlays={},
            candidate=candidate,
            preview=preview,
        )
        assert summary.catalog_fingerprint == candidate.catalog_fingerprint
        assert summary.preview_fingerprint == preview.fingerprint
        # A catalog.add produces added lines but no removed lines.
        assert summary.catalog_added_lines > 0
        assert summary.catalog_removed_lines >= 0
        assert len(summary.overlays) == 1
        entry = summary.overlays[0]
        assert entry.path == "dimensions/order_status.md"
        assert entry.status == "added"
        assert entry.fingerprint
        assert entry.added_lines > 0
        assert summary.references == ()

    def test_summary_marks_updated_vs_added(
        self, base_catalog_text: str
    ) -> None:
        from selayer_discovery.proposal import render_review_summary

        before = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nOld.\n"
        )
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nNew.\n"
        )
        op = Operation.from_mapping(
            _op(
                kind="reference.update",
                target_id="dimensions/order_status.md",
                before=before,
                before_hash=_hash(before),
                after=after,
            )
        )
        candidate = reconstruct_candidate(
            base_catalog_text=base_catalog_text,
            base_references={"dimensions/order_status.md": before},
            base_overlays={},
            operations=(op,),
        )
        preview = render_review_preview(
            base_catalog_text=base_catalog_text,
            base_references={"dimensions/order_status.md": before},
            base_overlays={},
            candidate=candidate,
        )
        summary = render_review_summary(
            base_references={"dimensions/order_status.md": before},
            base_overlays={},
            candidate=candidate,
            preview=preview,
        )
        entry = summary.references[0]
        assert entry.status == "updated"
        assert entry.removed_lines >= 1


# --------------------------------------------------------------------------- #
# Step 6: proposal import / show CLI                                          #
# --------------------------------------------------------------------------- #


def _write_proposal_yaml(path: Path, mapping: dict[str, Any]) -> None:
    buf = StringIO()
    YAML().dump(mapping, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _init_session(
    project: Path,
    catalog_rel: str = "catalog.yaml",
    session_id: str = "session-001",
) -> Path:
    from selayer_discovery.cli import main

    charter = project / "charter.yaml"
    charter_data = {
        "business_question": "Is the order grain one row per order?",
        "approver": "Dr. Alice Okonkwo",
        "catalog_fingerprint": "a" * 64,
        "inclusions": ["source.shopfloor.orders"],
        "exclusions": ["domain.finance"],
        "acceptance_questions": ["Does the grain pass the audit?"],
    }
    buf = StringIO()
    YAML().dump(charter_data, buf)
    charter.write_text(buf.getvalue(), encoding="utf-8")
    rc = main(
        [
            "session",
            "init",
            "--charter",
            str(charter),
            "--project",
            str(project),
            "--catalog-path",
            catalog_rel,
            "--session-id",
            session_id,
        ]
    )
    assert rc == 0
    return project / ".selayer" / "discovery" / "sessions" / session_id


class TestProposalImportCli:
    def test_proposal_import_reconstructs_and_stores(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, _proposal_mapping())
        rc = main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["proposal_id"] == "proposal-001"
        assert out["candidate_fingerprint"]
        assert out["operations"] >= 1

    def test_proposal_show_emits_previews(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, _proposal_mapping())
        assert (
            main(
                [
                    "proposal",
                    "import",
                    "--session-id",
                    "session-001",
                    "--project",
                    str(catalog_dir),
                    "--proposal",
                    str(proposal_path),
                ]
            )
            == 0
        )
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        # ``show`` emits a safe summary (no raw patch/diff body text).
        assert "catalog_patch" not in out
        assert "reference_diffs" not in out
        assert "overlay_diffs" not in out
        assert "preview_fingerprint" in out
        assert out["catalog"]["added_lines"] >= 1

    def test_proposal_show_summary_hides_document_body_canary(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        secret = "LEAKME-show-body-canary-778899"
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            f"## Usage Guidance\n{secret}\n"
        )
        op = _op(
            kind="overlay.create",
            target_id="dimensions/order_status.md",
            after=after,
        )
        mapping = _proposal_mapping(
            groups=[_group(operations=[op])],
        )
        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, mapping)
        assert (
            main(
                [
                    "proposal",
                    "import",
                    "--session-id",
                    "session-001",
                    "--project",
                    str(catalog_dir),
                    "--proposal",
                    str(proposal_path),
                ]
            )
            == 0
        )
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

    def test_proposal_show_rejects_traversal_proposal_id(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                "../../../etc/passwd",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"]
        # The traversal id must never reach the filesystem as a raw path.
        assert "passwd" not in captured.out
        assert "passwd" not in captured.err

    def test_proposal_show_rejects_missing_session(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-missing",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 1
        err = json.loads(capsys.readouterr().err)
        assert err["code"]

    def test_proposal_overlay_update_loads_authored_okf_overlays_root(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        before = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nOld.\n"
        )
        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nNew.\n"
        )
        # An authored overlay root consistent with the repo fixtures.
        overlay_root = catalog_dir / "okf_overlays" / "dimensions"
        overlay_root.mkdir(parents=True)
        (overlay_root / "order_status.md").write_text(before, encoding="utf-8")
        op = _op(
            kind="overlay.update",
            target_id="dimensions/order_status.md",
            before=before,
            before_hash=_hash(before),
            after=after,
        )
        mapping = _proposal_mapping(groups=[_group(operations=[op])])
        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, mapping)
        rc = main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        assert rc == 0
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert any(
            e["path"] == "dimensions/order_status.md" and e["status"] == "updated"
            for e in out["overlays"]
        )

    def test_proposal_reference_update_loads_authored_references_root(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        before = "---\nselayer_id: dimension.order_status\n---\n# Old\n"
        after = "---\nselayer_id: dimension.order_status\n---\n# New\n"
        reference_root = catalog_dir / "references" / "dimensions"
        reference_root.mkdir(parents=True)
        (reference_root / "order_status.md").write_text(before, encoding="utf-8")
        op = _op(
            kind="reference.update",
            target_id="dimensions/order_status.md",
            before=before,
            before_hash=_hash(before),
            after=after,
        )
        mapping = _proposal_mapping(groups=[_group(operations=[op])])
        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, mapping)
        rc = main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        assert rc == 0
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "show",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert any(
            e["path"] == "dimensions/order_status.md" and e["status"] == "updated"
            for e in out["references"]
        )

    def test_proposal_knowledge_create_valid_with_absent_roots(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # When no authored roots exist, empty bases remain valid for create ops.
        from selayer_discovery.cli import main

        after = (
            "---\nselayer_id: dimension.order_status\n---\n"
            "## Usage Guidance\nNew.\n"
        )
        op = _op(
            kind="overlay.create",
            target_id="dimensions/order_status.md",
            after=after,
        )
        mapping = _proposal_mapping(groups=[_group(operations=[op])])
        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, mapping)
        rc = main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        assert rc == 0


# --------------------------------------------------------------------------- #
# Stable operation ordering                                                   #
# --------------------------------------------------------------------------- #


def test_catalog_collection_map_is_complete() -> None:
    assert CATALOG_COLLECTION_BY_KIND[OperationKind.CATALOG_ADD]
    assert CATALOG_COLLECTION_BY_KIND[OperationKind.CATALOG_EDIT]
    assert CATALOG_COLLECTION_BY_KIND[OperationKind.CATALOG_DEPRECATE]


def test_knowledge_subjects_known() -> None:
    assert KnowledgeSubject.REFERENCE
    assert KnowledgeSubject.OVERLAY


# --------------------------------------------------------------------------- #
# Task 17: impact-derived verification readiness                              #
# --------------------------------------------------------------------------- #
#
# These tests pin the Task 17 verification-readiness contract:
#
# * the mandatory-check matrix is derived solely from normalized before/after
#   impacts (never agent-supplied) and maps every impact to its required
#   evidence exactly;
# * typed safe semantic query cases carry expected compatible plans, stable
#   planner rejection codes, and optional bounded execution assertions, and
#   reject SQL, callable assertions, unrestricted row capture, and unknown
#   result operators;
# * readiness gates over affecting gates, current non-inferred claims,
#   conflicts, dependency groups, reopenable evidence, and mandatory-check
#   outcomes;
# * ``proposal verify`` reconstructs a fresh candidate and writes an immutable
#   report bound to all input hashes whose semantic fingerprint is stable on
#   repeated unchanged inputs.

from selayer_discovery.proposal import (
    MandatoryCheckKind,
    mandatory_check_kinds,
    verify_proposal,
)

# Impact flag vocabulary (mirrors proposal._IMPACT_* constants).
_IMPACT_OBJECT_ADDED = "object_added"
_IMPACT_OBJECT_EDITED = "object_edited"
_IMPACT_ID_DEPRECATED = "id_deprecated"
_IMPACT_SOURCE_CHANGED = "source_changed"
_IMPACT_SCHEMA_CHANGED = "schema_changed"
_IMPACT_GRAIN_CHANGED = "grain_changed"
_IMPACT_RELATIONSHIP_CHANGED = "relationship_changed"
_IMPACT_TYPE_CHANGED = "type_changed"
_IMPACT_EXPRESSION_CHANGED = "expression_changed"
_IMPACT_AGGREGATION_CHANGED = "aggregation_changed"
_IMPACT_FORMULA_CHANGED = "formula_changed"
_IMPACT_REFERENCE_CHANGED = "reference_changed"
_IMPACT_OVERLAY_CHANGED = "overlay_changed"


# -- operation factories producing each impact family ---------------------- #


def _metric_add_op(**overrides: Any) -> dict[str, Any]:
    after = {
        "expression": "total_order_amount",
        "measures": ["total_order_amount"],
        "description": "Total order revenue",
    }
    base: dict[str, Any] = {
        "operation_id": "op-metric-add",
        "kind": "catalog.add",
        "target_id": "metric.total_revenue",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _measure_aggregation_edit_op(**overrides: Any) -> dict[str, Any]:
    before = {
        "fact": "order_amount",
        "aggregation": "sum",
        "description": "Total order amount",
    }
    after = {
        "fact": "order_amount",
        "aggregation": "max",
        "description": "Total order amount",
    }
    base: dict[str, Any] = {
        "operation_id": "op-measure-edit",
        "kind": "catalog.edit",
        "target_id": "measure.total_order_amount",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


#: Full orders source schema matching the fixture catalog so a candidate
#: ``source.orders`` edit always loads through ``SemanticLayer.load`` and the
#: grain ``[id, status]`` resolves to declared columns.
_ORDERS_SCHEMA_FIELDS: list[dict[str, Any]] = [
    {"name": "id", "type": "utf8", "nullable": False},
    {"name": "customer_id", "type": "utf8", "nullable": True},
    # ``status`` is non-nullable so it can serve as a grain column: the
    # production catalog loader rejects nullable grain columns, and the core
    # physical audit requires the declared shape to match the parquet file.
    {"name": "status", "type": "utf8", "nullable": False},
    {"name": "amount", "type": "float64", "nullable": True},
]


def _orders_source(location: str, grain: Sequence[str]) -> dict[str, Any]:
    """Return a complete orders data-source state for a given location."""

    return {
        "type": "parquet",
        "location": location,
        "grain": list(grain),
        "schema": {"fields": [dict(field) for field in _ORDERS_SCHEMA_FIELDS]},
    }


def _orders_grain_after(location: str) -> dict[str, Any]:
    """Return the after state for a source-grain edit.

    The fixture widens the grain from ``[id]`` to ``[id, status]`` only: the
    schema fields come unchanged from ``_ORDERS_SCHEMA_FIELDS`` (where
    ``status`` is non-nullable so it can serve as a grain column). Production
    source-shape validation is preserved unchanged and the derived
    ``grain_changed`` impact triggers the physical audit. The ``location``
    threads the authored source-location semantics through to the after state
    so a real parquet path makes the physical audit pass (the candidate shape
    matches the file) and a missing path makes it unavailable.
    """

    fields = [dict(field) for field in _ORDERS_SCHEMA_FIELDS]
    return {
        "type": "parquet",
        "location": location,
        "grain": ["id", "status"],
        "schema": {"fields": fields},
    }


def _source_grain_edit_op(**overrides: Any) -> dict[str, Any]:
    # ``before`` is popped so the derived ``after`` and ``before_hash`` always
    # stay consistent with whatever state a test supplies (a missing-file
    # location, a real parquet location, etc.). The default carries the full
    # orders schema so the candidate is a valid, loadable source. The after
    # state widens the grain to ``[id, status]`` keeping the fixture's schema
    # fields (``status`` is non-nullable so the grain loads and matches the
    # parquet file).
    before = dict(
        overrides.pop("before", _orders_source("data/orders.parquet", ["id"]))
    )
    after_override = overrides.pop("after", None)
    after = (
        after_override
        if after_override is not None
        else _orders_grain_after(str(before["location"]))
    )
    base: dict[str, Any] = {
        "operation_id": "op-source-grain",
        "kind": "catalog.edit",
        "target_id": "source.orders",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _relationship_add_op(**overrides: Any) -> dict[str, Any]:
    after = {
        "source": "products",
        "target": "orders",
        "type": "one_to_many",
        "source_column": "id",
        "target_column": "customer_id",
    }
    base: dict[str, Any] = {
        "operation_id": "op-rel-add",
        "kind": "catalog.add",
        "target_id": "relationship.product_orders_v2",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _dimension_type_edit_op(**overrides: Any) -> dict[str, Any]:
    before = {
        "source": "orders",
        "column": "status",
        "data_type": "string",
        "description": "Order status",
    }
    after = dict(before)
    after["data_type"] = "integer"
    base: dict[str, Any] = {
        "operation_id": "op-dim-type",
        "kind": "catalog.edit",
        "target_id": "dimension.order_status",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _fact_type_edit_op(**overrides: Any) -> dict[str, Any]:
    # Edit ``fact.order_amount`` data_type ``decimal`` -> ``double``: both are
    # compatible with the float64 ``amount`` column, so the candidate always
    # loads and the static check passes. The ``data_type`` field change drives
    # the derived ``type_changed`` impact (the conditional data-evidence gate).
    before = {
        "source": "orders",
        "expression": "orders.amount",
        "data_type": "decimal",
        "description": "Order amount",
    }
    after = dict(before)
    after["data_type"] = "double"
    base: dict[str, Any] = {
        "operation_id": "op-fact-type",
        "kind": "catalog.edit",
        "target_id": "fact.order_amount",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _deprecate_op(**overrides: Any) -> dict[str, Any]:
    before = {
        "source": "orders",
        "column": "status",
        "data_type": "string",
        "description": "Order status",
    }
    after = dict(before)
    after["status"] = "deprecated"
    after["replaced_by"] = "dimension.order_state"
    base: dict[str, Any] = {
        "operation_id": "op-deprecate",
        "kind": "catalog.deprecate",
        "target_id": "dimension.order_status",
        "before": before,
        "before_hash": _hash(before),
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _overlay_create_op(**overrides: Any) -> dict[str, Any]:
    # OKF overlays use level-1 (``#``) section headings; the curated
    # ``Usage Guidance`` section is one of the allowed overlay sections and
    # its ``selayer_id`` resolves to a catalog object.
    after = (
        "---\nselayer_id: dimension.order_status\n---\n"
        "# Usage Guidance\nUse for status filtering.\n"
    )
    base: dict[str, Any] = {
        "operation_id": "op-overlay",
        "kind": "overlay.create",
        "target_id": "dimensions/order_status.md",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _reference_create_op(**overrides: Any) -> dict[str, Any]:
    after = "---\nselayer_id: dimension.order_status\n---\n# Order status\n"
    base: dict[str, Any] = {
        "operation_id": "op-reference",
        "kind": "reference.create",
        "target_id": "dimensions/order_status.md",
        "before": None,
        "before_hash": GENESIS_HASH,
        "after": after,
        "claim_ids": ["claim-c1"],
        "group_ids": ["group-001"],
    }
    base.update(overrides)
    return base


def _query_case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "case-001",
        "kind": "compatible_plan",
        "description": "The new metric plans with the status dimension.",
        "metrics": ["total_revenue"],
        "dimensions": ["order_status"],
    }
    base.update(overrides)
    return base


# -- reconstruction + verify helper ---------------------------------------- #


def _reconstruct_and_load(
    proposal_mapping: dict[str, Any],
    base_catalog_text: str,
    tmp_path: Path,
) -> tuple[Any, Any, Any, Path]:
    """Build, reconstruct, write, and load a candidate layer."""

    from selayer_discovery.proposal import (
        build_proposal,
        reconstruct_candidate,
        write_candidate,
    )

    proposal = build_proposal(proposal_mapping)
    candidate = reconstruct_candidate(
        base_catalog_text=base_catalog_text,
        base_references={},
        base_overlays={},
        operations=proposal.operations,
    )
    candidate_dir = tmp_path / "candidate"
    catalog_path = write_candidate(candidate, candidate_dir)
    layer = SemanticLayer.load(catalog_path)
    return proposal, candidate, layer, candidate_dir


# --------------------------------------------------------------------------- #
# Step 1: exact impact -> mandatory-check matrix                              #
# --------------------------------------------------------------------------- #


class TestMandatoryMatrix:
    """The mandatory-check matrix is derived solely from derived impacts."""

    def test_dimension_add_requires_static(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_OBJECT_ADDED,))
        assert MandatoryCheckKind.STATIC.value in kinds

    def test_dimension_type_edit_requires_static(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_OBJECT_EDITED, _IMPACT_TYPE_CHANGED))
        assert MandatoryCheckKind.STATIC.value in kinds
        assert MandatoryCheckKind.PHYSICAL.value not in kinds

    def test_source_add_requires_static_and_physical(self) -> None:
        kinds = mandatory_check_kinds(
            (_IMPACT_OBJECT_ADDED, _IMPACT_SOURCE_CHANGED, _IMPACT_SCHEMA_CHANGED)
        )
        assert MandatoryCheckKind.STATIC.value in kinds
        assert MandatoryCheckKind.PHYSICAL.value in kinds

    def test_source_grain_edit_requires_physical(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_OBJECT_EDITED, _IMPACT_GRAIN_CHANGED))
        assert MandatoryCheckKind.PHYSICAL.value in kinds

    def test_relationship_add_requires_physical(self) -> None:
        kinds = mandatory_check_kinds(
            (_IMPACT_OBJECT_ADDED, _IMPACT_RELATIONSHIP_CHANGED)
        )
        assert MandatoryCheckKind.PHYSICAL.value in kinds

    def test_measure_aggregation_edit_requires_compatibility_and_acceptance(
        self,
    ) -> None:
        kinds = mandatory_check_kinds(
            (_IMPACT_OBJECT_EDITED, _IMPACT_AGGREGATION_CHANGED)
        )
        assert MandatoryCheckKind.COMPATIBILITY.value in kinds
        assert MandatoryCheckKind.ACCEPTANCE.value in kinds

    def test_metric_formula_requires_compatibility_and_acceptance(self) -> None:
        kinds = mandatory_check_kinds(
            (_IMPACT_OBJECT_ADDED, _IMPACT_EXPRESSION_CHANGED, _IMPACT_FORMULA_CHANGED)
        )
        assert MandatoryCheckKind.COMPATIBILITY.value in kinds
        assert MandatoryCheckKind.ACCEPTANCE.value in kinds

    def test_deprecation_requires_static_and_compatibility(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_ID_DEPRECATED, _IMPACT_OBJECT_EDITED))
        assert MandatoryCheckKind.STATIC.value in kinds
        assert MandatoryCheckKind.COMPATIBILITY.value in kinds

    def test_reference_change_requires_okf(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_REFERENCE_CHANGED,))
        assert kinds == frozenset({MandatoryCheckKind.OKF.value})

    def test_overlay_change_requires_okf(self) -> None:
        kinds = mandatory_check_kinds((_IMPACT_OVERLAY_CHANGED,))
        assert kinds == frozenset({MandatoryCheckKind.OKF.value})

    def test_matrix_derived_from_operation_impacts_not_agent(self) -> None:
        # The mandatory kinds for a group are derived from the operation's
        # derived impacts, never from an agent-supplied list.
        op = Operation.from_mapping(_source_grain_edit_op())
        kinds = mandatory_check_kinds(op.impacts)
        assert MandatoryCheckKind.PHYSICAL.value in kinds
        # A hostile agent-supplied impact is never honoured.
        assert "agent_only_impact" not in kinds


# --------------------------------------------------------------------------- #
# Step 2: typed safe semantic query cases                                    #
# --------------------------------------------------------------------------- #


class TestQueryCases:
    """Query cases carry typed safe payloads and reject unsafe inputs."""

    def test_compatible_plan_case_carries_selectors(self) -> None:
        case = QueryCase.from_mapping(_query_case())
        assert case.metrics == ("total_revenue",)
        assert case.dimensions == ("order_status",)

    def test_planner_rejection_case_carries_expected_code(self) -> None:
        case = QueryCase.from_mapping(
            _query_case(
                case_id="case-rej",
                kind="planner_rejection",
                description="Unknown metric is rejected.",
                metrics=["nonexistent"],
                expected_rejection_code="unknown_metric",
            )
        )
        assert case.expected_rejection_code == "unknown_metric"

    def test_execution_assertion_case_carries_bounded_assertion(self) -> None:
        case = QueryCase.from_mapping(
            _query_case(
                case_id="case-exec",
                kind="execution_assertion",
                description="Bounded row count.",
                assertions=[{"operator": "row_count_max", "value": 100}],
            )
        )
        assert len(case.assertions) == 1
        assert case.assertions[0].operator == "row_count_max"

    def test_filter_carries_safe_value(self) -> None:
        case = QueryCase.from_mapping(
            _query_case(
                filters=[
                    {"dimension_id": "order_status", "operator": "equals", "value": "open"}
                ],
            )
        )
        assert case.filters[0].dimension_id == "order_status"
        assert case.filters[0].value == "open"

    def test_rejects_sql_assertion(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    assertions=[{"operator": "row_count_max", "value": 1, "sql": "SELECT 1"}],
                )
            )

    def test_rejects_callable_assertion(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    assertions=[
                        {"operator": "row_count_max", "value": 1, "check": "len"}
                    ],
                )
            )

    def test_rejects_unrestricted_row_capture(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    assertions=[
                        {"operator": "row_count_max", "value": 1, "capture_rows": True}
                    ],
                )
            )

    def test_rejects_unknown_result_operator(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    assertions=[{"operator": "sum_equals", "value": 42}],
                )
            )

    def test_rejects_unknown_filter_operator(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    filters=[
                        {"dimension_id": "order_status", "operator": "like", "value": "%"}
                    ],
                )
            )

    def test_rejects_sql_key_in_case(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(sql="SELECT * FROM orders")
            )

    def test_invalid_filter_type_is_accepted_rejection_code(self) -> None:
        # ``invalid_filter_type`` is a stable core ``QueryPlanningError`` code
        # (raised when a filter value's type mismatches a dimension's declared
        # data type). A focused planner-rejection case may cite it.
        case = QueryCase.from_mapping(
            _query_case(
                case_id="case-invalid-filter",
                kind="planner_rejection",
                description="A typed-mismatch filter is rejected.",
                metrics=["total_revenue"],
                dimensions=["order_status"],
                filters=[
                    {"dimension_id": "order_status", "operator": "equals", "value": 5}
                ],
                expected_rejection_code="invalid_filter_type",
            )
        )
        assert case.expected_rejection_code == "invalid_filter_type"

    def test_rejects_unknown_rejection_code(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    kind="planner_rejection",
                    expected_rejection_code="made_up_code",
                )
            )

    def test_rejects_callable_value_in_filter(self) -> None:
        with pytest.raises(ProposalError):
            QueryCase.from_mapping(
                _query_case(
                    filters=[
                        {"dimension_id": "order_status", "operator": "equals", "check": print}
                    ],
                )
            )


# --------------------------------------------------------------------------- #
# Step 3 + 4: verification delegation and readiness                          #
# --------------------------------------------------------------------------- #


class TestVerificationChecks:
    """verify_proposal delegates to core public APIs per the matrix."""

    def test_static_check_runs_for_catalog_add(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        checks = bundle.checks_for("group-001")
        kinds = {c.kind for c in checks}
        assert MandatoryCheckKind.STATIC.value in kinds
        static = bundle.check("group-001", MandatoryCheckKind.STATIC.value)
        assert static.status == "passed"

    def test_physical_check_runs_for_source_impact(
        self, catalog_dir: Path, base_catalog_text: str, tmp_path: Path
    ) -> None:
        orders_path = catalog_dir / "data" / "orders.parquet"
        op = _source_grain_edit_op(
            before=_orders_source(str(orders_path), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit order grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.PHYSICAL.value in kinds
        physical = bundle.check("group-001", MandatoryCheckKind.PHYSICAL.value)
        assert physical.status == "passed"

    def test_physical_check_unavailable_when_source_missing(
        self, tmp_path: Path
    ) -> None:
        # A candidate source pointing at a nonexistent file makes the physical
        # audit unavailable: readiness must refuse it, never bypass.
        data = tmp_path / "data"
        data.mkdir()
        missing = data / "missing.parquet"
        catalog_text = f"""\
version: 1
name: empty
label: Empty
description: empty
data_sources:
  orders:
    type: parquet
    location: {missing!s}
    grain: [id]
    schema:
      fields:
        - {{name: id, type: utf8, nullable: false}}
"""
        op = _source_grain_edit_op(
            before=_orders_source(str(missing), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        physical = bundle.check("group-001", MandatoryCheckKind.PHYSICAL.value)
        assert physical.status == "unavailable"
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "check_failed" in readiness.blockers

    def test_compatibility_and_acceptance_for_formula_impact(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        op = _metric_add_op()
        case = _query_case(
            case_id="case-compatible",
            kind="compatible_plan",
            description="The new metric plans.",
            metrics=["total_revenue"],
            dimensions=["order_status"],
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add revenue metric",
                    rationale="Revenue rollup.",
                    operations=[op],
                    query_cases=[case],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.COMPATIBILITY.value in kinds
        assert MandatoryCheckKind.ACCEPTANCE.value in kinds
        acceptance = bundle.check("group-001", MandatoryCheckKind.ACCEPTANCE.value)
        assert acceptance.status == "passed"

    def test_acceptance_rejection_case_passes_with_matching_code(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        op = _metric_add_op()
        case = _query_case(
            case_id="case-rej",
            kind="planner_rejection",
            description="Unknown metric is rejected.",
            metrics=["nonexistent"],
            expected_rejection_code="unknown_metric",
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add revenue metric",
                    rationale="Revenue rollup.",
                    operations=[op],
                    query_cases=[case],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        acceptance = bundle.check("group-001", MandatoryCheckKind.ACCEPTANCE.value)
        assert acceptance.status == "passed"

    def test_acceptance_execution_assertion_bounded(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        op = _metric_add_op()
        case = _query_case(
            case_id="case-exec",
            kind="execution_assertion",
            description="Bounded row count.",
            metrics=["total_revenue"],
            dimensions=["order_status"],
            assertions=[{"operator": "row_count_max", "value": 100}],
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add revenue metric",
                    rationale="Revenue rollup.",
                    operations=[op],
                    query_cases=[case],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        acceptance = bundle.check("group-001", MandatoryCheckKind.ACCEPTANCE.value)
        assert acceptance.status == "passed"

    def test_acceptance_invalid_filter_type_rejection_case(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        # A focused planner-rejection case citing ``invalid_filter_type``: the
        # typed filter value (int 5) mismatches the string ``order_status``
        # dimension, so the core planner rejects with exactly that code.
        op = _metric_add_op()
        case = _query_case(
            case_id="case-invalid-filter",
            kind="planner_rejection",
            description="A typed-mismatch filter is rejected.",
            metrics=["total_revenue"],
            dimensions=["order_status"],
            filters=[
                {"dimension_id": "order_status", "operator": "equals", "value": 5}
            ],
            expected_rejection_code="invalid_filter_type",
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add revenue metric",
                    rationale="Revenue rollup.",
                    operations=[op],
                    query_cases=[case],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        acceptance = bundle.check("group-001", MandatoryCheckKind.ACCEPTANCE.value)
        assert acceptance.status == "passed"

    def test_okf_check_runs_for_overlay(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        op = _overlay_create_op()
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add overlay",
                    rationale="Curated guidance.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.OKF.value in kinds
        okf = bundle.check("group-001", MandatoryCheckKind.OKF.value)
        assert okf.status == "passed"

    def test_okf_check_fails_for_invalid_overlay(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        # An overlay whose selayer_id does not match a catalog object fails
        # strict OKF integrity on load.
        after = (
            "---\nselayer_id: dimension.nonexistent\n---\n"
            "# Usage Guidance\nGuidance.\n"
        )
        op = _overlay_create_op(after=after)
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Add overlay",
                    rationale="Curated guidance.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        okf = bundle.check("group-001", MandatoryCheckKind.OKF.value)
        assert okf.status == "failed"

    def test_bundle_fingerprint_stable_on_repeat(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        first = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf-1",
        )
        second = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf-2",
        )
        assert first.fingerprint == second.fingerprint
        assert first.input_hashes == second.input_hashes

    def test_bundle_has_no_raw_values(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        rendered = json.dumps(bundle.to_dict(), sort_keys=True)
        # No SQL, no raw evidence bodies, no document prose leak into the report.
        assert "SELECT" not in rendered
        assert "product_category" not in rendered
        assert "Order status" not in rendered


# --------------------------------------------------------------------------- #
# Step 3b: conditional type/expression data-evidence gating (Task 17 fix)     #
# --------------------------------------------------------------------------- #


class TestTypeExpressionDataCitation:
    """A type/expression group that cites observed data needs a physical check.

    The pure impacts-only matrix keeps type/expression static-only. When a
    type/expression group's normalized typed data cites data -- an observed
    supporting claim and/or a bounded execution-assertion query case -- a
    physical (reopenable) requirement is added at the group-check level. The
    conditional is never derived from an agent-supplied impact list.
    """

    def test_type_expression_cited_by_execution_assertion_adds_physical(
        self, base_catalog_text: str, tmp_path: Path
    ) -> None:
        op = _fact_type_edit_op()
        case = _query_case(
            case_id="case-exec-type",
            kind="execution_assertion",
            description="Bounded row count over the typed fact.",
            metrics=[],
            dimensions=["order_status"],
            assertions=[{"operator": "row_count_max", "value": 100}],
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit fact type",
                    rationale="Type revision.",
                    operations=[op],
                    query_cases=[case],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.STATIC.value in kinds
        assert MandatoryCheckKind.PHYSICAL.value in kinds

    def test_type_expression_cited_by_observed_claim_adds_physical(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        op = _fact_type_edit_op()
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit fact type",
                    rationale="Type revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.PHYSICAL.value in kinds

    def test_type_expression_without_citation_has_no_physical(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        # A type/expression group whose only supporting claim is ASSERTED (not
        # observed) and which has no execution-assertion case stays static-only:
        # the conditional physical requirement must not fire.
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        from selayer_discovery.model import EvidenceClass

        TestReadiness._add_observed_claim(
            claims,
            evidence,
            session_store,
            tmp_path,
            actor,
            evidence_class=EvidenceClass.ASSERTED,
        )
        op = _fact_type_edit_op()
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit fact type",
                    rationale="Type revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        kinds = {c.kind for c in bundle.checks_for("group-001")}
        assert MandatoryCheckKind.STATIC.value in kinds
        assert MandatoryCheckKind.PHYSICAL.value not in kinds


# --------------------------------------------------------------------------- #
# Step 4: readiness gating                                                    #
# --------------------------------------------------------------------------- #


class TestReadiness:
    """Readiness gates over gates, claims, conflicts, deps, evidence, checks."""

    @staticmethod
    def _stores(catalog_dir: Path) -> Any:
        """Open session, evidence, claim, and interview stores."""

        from selayer_discovery.evidence import ClaimStore, EvidenceStore
        from selayer_discovery.interview import InterviewStore
        from selayer_discovery.session import SessionStore

        session_dir = (
            catalog_dir / ".selayer" / "discovery" / "sessions" / "session-001"
        )
        session_store = SessionStore.open(session_dir)
        evidence = EvidenceStore.create(session_dir / "evidence")
        claims = ClaimStore.create(session_store, evidence)
        interview = InterviewStore.create(session_store)
        return session_store, evidence, claims, interview

    @staticmethod
    def _add_observed_claim(
        claims: Any,
        evidence: Any,
        session_store: Any,
        project: Path,
        actor: str,
        *,
        claim_id: str = "claim-c1",
        evidence_class: Any = None,
    ) -> Any:
        from selayer_discovery.evidence import (
            DocumentLineSelector,
        )
        from selayer_discovery.model import EvidenceClass

        doc = project / "spec.md"
        doc.write_text("grain evidence line one\nline two\n", encoding="utf-8")
        record = evidence.add_document(doc, allowed_roots=(project,))
        selector = DocumentLineSelector(
            record_id=record.record_id,
            content_hash=record.content_hash,
            revision=record.revision,
            start_line=1,
            end_line=1,
        )
        session_store.record_artifact(
            "answer-gate-grain", content_hash="a" * 64, actor=actor
        )
        return claims.add_claim(
            claim_id=claim_id,
            subject="source.shopfloor.orders",
            statement="The grain is one row per order.",
            evidence_class=evidence_class or EvidenceClass.OBSERVED,
            selectors=(selector,),
            creator_event="answer-gate-grain",
            actor=actor,
        )

    def test_ready_when_all_satisfied(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = self._stores(catalog_dir)
        actor = session_store.charter.approver
        self._add_observed_claim(claims, evidence, session_store, tmp_path, actor)
        # Dispose the affecting gate.
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert readiness.ready
        assert readiness.blockers == ()

    def test_blocked_by_open_gate(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = self._stores(catalog_dir)
        actor = session_store.charter.approver
        self._add_observed_claim(claims, evidence, session_store, tmp_path, actor)
        # Gate left undisposed.
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "gate_open" in readiness.blockers

    def test_blocked_by_inferred_only_claim(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        from selayer_discovery.model import EvidenceClass

        _init_session(catalog_dir)
        session_store, evidence, claims, interview = self._stores(catalog_dir)
        actor = session_store.charter.approver
        self._add_observed_claim(
            claims,
            evidence,
            session_store,
            tmp_path,
            actor,
            evidence_class=EvidenceClass.INFERRED,
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "claim_inferred_only" in readiness.blockers

    def test_blocked_by_unresolved_conflict(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        from selayer_discovery.evidence import ConflictKind

        _init_session(catalog_dir)
        session_store, evidence, claims, interview = self._stores(catalog_dir)
        actor = session_store.charter.approver
        self._add_observed_claim(claims, evidence, session_store, tmp_path, actor)
        self._add_observed_claim(
            claims,
            evidence,
            session_store,
            tmp_path,
            actor,
            claim_id="claim-c2",
        )
        claims.add_conflict(
            conflict_id="conflict-grain",
            kind=ConflictKind.SEMANTIC,
            subject="source.shopfloor.orders",
            involved_claim_ids=("claim-c1", "claim-c2"),
            affected_group_ids=("group-001",),
            reason="Sources disagree.",
            actor=actor,
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "conflict_unresolved" in readiness.blockers

    def test_blocked_by_dependency_not_ready(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = self._stores(catalog_dir)
        actor = session_store.charter.approver
        self._add_observed_claim(claims, evidence, session_store, tmp_path, actor)
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        # group-001 depends on group-002; group-002 has an open gate
        # (``gate-other``) so it is not ready, which must block group-001 via
        # the dependency gate. g2 targets a distinct semantic target
        # (``dimension.product_segment``) so the test exercises dependency
        # readiness, not the cross-group overlap guard.
        g1 = _group(
            group_id="group-001",
            dependencies=["group-002"],
            affecting_gates=["gate-grains"],
        )
        g2 = _group(
            group_id="group-002",
            title="Dependent",
            rationale="Depends.",
            dependencies=[],
            affecting_gates=["gate-other"],
            supporting_claim_ids=["claim-c1"],
            operations=[
                _op(
                    operation_id="op-002",
                    target_id="dimension.product_segment",
                    after={
                        "source": "products",
                        "column": "category",
                        "data_type": "string",
                        "description": "Product segment",
                    },
                )
            ],
        )
        mapping = _proposal_mapping(groups=[g1, g2])
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "dependency_not_ready" in readiness.blockers

    def test_blocked_by_failed_mandatory_check(
        self, tmp_path: Path
    ) -> None:
        # A candidate whose source is missing fails the physical check; even
        # with a disposed gate and a current claim, readiness is blocked.
        _init_session_with_catalog(tmp_path)
        session_store, evidence, claims, interview = self._stores(tmp_path)
        actor = session_store.charter.approver
        self._add_observed_claim(claims, evidence, session_store, tmp_path, actor)
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        op = _source_grain_edit_op(
            before=_orders_source(str(tmp_path / "nope.parquet"), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        catalog_text = _missing_source_catalog(tmp_path)
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "check_failed" in readiness.blockers


# --------------------------------------------------------------------------- #
# Step 4b: evidence-readiness gates (Task 17 fix)                           #
# --------------------------------------------------------------------------- #


class TestEvidenceReadinessGates:
    """Readiness validates supporting-claim selectors and reopenable evidence.

    For every supporting claim, each retained selector is revalidated against
    the current evidence revision: a stale or missing selector blocks. When a
    group requires physical (reopenable) evidence, the referenced snapshot
    content must exist and reopen safely; live/non-reopenable evidence blocks.
    """

    def test_stale_evidence_selector_blocks_readiness(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        claim = TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        # Revise the cited document after the claim was recorded so the
        # retained selector now binds to a superseded revision.
        doc = tmp_path / "spec.md"
        doc.write_text("revised grain evidence\nline two\n", encoding="utf-8")
        evidence.add_document(doc, allowed_roots=(tmp_path,))
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "evidence_selector_stale" in readiness.blockers
        # The retained selectors are available for revalidation.
        assert len(claim.selectors) == 1

    def test_missing_evidence_selector_blocks_readiness(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping()
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        # An evidence store that does not contain the cited record: the
        # retained selector's record is absent, so revalidation blocks.
        from selayer_discovery.evidence import EvidenceStore

        empty_evidence = EvidenceStore.create(tmp_path / "empty-evidence")
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=empty_evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "evidence_selector_stale" in readiness.blockers

    def test_non_reopenable_evidence_blocks_readiness(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        claim = TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        orders_path = catalog_dir / "data" / "orders.parquet"
        op = _source_grain_edit_op(
            before=_orders_source(str(orders_path), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit order grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        # Delete the content-addressed snapshot backing the selector: the
        # selector stays current (the manifest still records it) but the
        # snapshot can no longer be reopened.
        snapshot = evidence.snapshot_path(claim.selectors[0].content_hash)
        snapshot.unlink()
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "evidence_not_reopenable" in readiness.blockers

    def test_reopenable_evidence_passes_for_physical_group(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        orders_path = catalog_dir / "data" / "orders.parquet"
        op = _source_grain_edit_op(
            before=_orders_source(str(orders_path), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit order grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert readiness.ready
        assert readiness.blockers == ()

    def test_tampered_evidence_snapshot_blocks_readiness(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        # Overwriting a content-addressed snapshot with different bytes must
        # block readiness: the snapshot "exists" but no longer reopens to the
        # recorded content hash, so the reopenability gate fails closed.
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        claim = TestReadiness._add_observed_claim(
            claims, evidence, session_store, tmp_path, actor
        )
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        orders_path = catalog_dir / "data" / "orders.parquet"
        op = _source_grain_edit_op(
            before=_orders_source(str(orders_path), ["id"]),
        )
        mapping = _proposal_mapping(
            groups=[
                _group(
                    title="Edit order grain",
                    rationale="Grain revision.",
                    operations=[op],
                )
            ]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        # Tamper: overwrite the snapshot bytes so the content hash no longer
        # matches its content-addressed filename.
        evidence.snapshot_path(claim.selectors[0].content_hash).write_bytes(
            b"tampered snapshot bytes\n"
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "evidence_not_reopenable" in readiness.blockers

    def test_legacy_claim_without_selectors_blocks_readiness(
        self,
        catalog_dir: Path,
        base_catalog_text: str,
        tmp_path: Path,
    ) -> None:
        # A legacy journal payload may declare selector_kinds but persist no
        # typed selectors (pre-typed-selector format). Such a claim cannot be
        # revalidated against the evidence revision: readiness fails closed
        # with evidence_selector_stale rather than vacuously passing the
        # selector gates over an empty tuple.
        _init_session(catalog_dir)
        session_store, evidence, claims, interview = TestReadiness._stores(
            catalog_dir
        )
        actor = session_store.charter.approver
        # Inject a legacy claim journal line: selector_kinds nonempty but no
        # persisted ``selectors`` field, then reopen the store to reconstruct.
        legacy_payload = {
            "claim_id": "claim-legacy",
            "subject": "source.shopfloor.orders",
            "statement": "A legacy claim with no persisted selectors.",
            "evidence_class": "observed",
            "creator_event": "answer-gate-grain",
            "contradicts": [],
            "selector_kinds": ["document_line_range"],
            "actor": actor,
            "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
            "state": "current",
        }
        journal = claims.root / "claims.jsonl"
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(legacy_payload, sort_keys=True) + "\n")
        from selayer_discovery.evidence import ClaimStore

        claims = ClaimStore.open(session_store, evidence)
        legacy = claims.get_claim("claim-legacy")
        assert legacy.selector_kinds == ("document_line_range",)
        assert legacy.selectors == ()
        interview.answer(gate="gate-grains", text="Yes.", actor=actor)
        mapping = _proposal_mapping(
            groups=[_group(supporting_claim_ids=["claim-legacy"])]
        )
        proposal, candidate, layer, _ = _reconstruct_and_load(
            mapping, base_catalog_text, tmp_path
        )
        bundle = verify_proposal(
            proposal=proposal,
            candidate=candidate,
            candidate_layer=layer,
            okf_output_dir=tmp_path / "okf",
            interview_store=interview,
            claim_store=claims,
            evidence_store=evidence,
        )
        readiness = bundle.readiness_for("group-001")
        assert not readiness.ready
        assert "evidence_selector_stale" in readiness.blockers


# --------------------------------------------------------------------------- #
# Step 5: CLI proposal verify                                                 #
# --------------------------------------------------------------------------- #


class TestProposalVerifyCli:
    """``proposal verify`` writes an immutable hash-bound report."""

    def test_verify_emits_hash_bound_report(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, _proposal_mapping())
        assert (
            main(
                [
                    "proposal",
                    "import",
                    "--session-id",
                    "session-001",
                    "--project",
                    str(catalog_dir),
                    "--proposal",
                    str(proposal_path),
                ]
            )
            == 0
        )
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "verify",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["proposal_id"] == "proposal-001"
        assert out["fingerprint"]
        assert out["input_hashes"]
        assert isinstance(out["groups"], list)
        group = out["groups"][0]
        assert group["group_id"] == "group-001"
        assert isinstance(group["checks"], list)
        assert isinstance(group["ready"], bool)

    def test_verify_fingerprint_stable_on_repeat(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, _proposal_mapping())
        main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        capsys.readouterr()
        main(
            [
                "proposal",
                "verify",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        first = json.loads(capsys.readouterr().out)
        main(
            [
                "proposal",
                "verify",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        second = json.loads(capsys.readouterr().out)
        assert first["fingerprint"] == second["fingerprint"]
        assert first["input_hashes"] == second["input_hashes"]

    def test_verify_output_has_no_raw_values(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        proposal_path = catalog_dir / "proposal.yaml"
        _write_proposal_yaml(proposal_path, _proposal_mapping())
        main(
            [
                "proposal",
                "import",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                str(proposal_path),
            ]
        )
        capsys.readouterr()
        main(
            [
                "proposal",
                "verify",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
            ]
        )
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        rendered = json.dumps(out, sort_keys=True)
        assert "SELECT" not in rendered
        assert "product_category" not in rendered
        # No raw evidence/document body text leaks.
        assert "Order status" not in rendered

    def test_verify_rejects_missing_proposal(
        self,
        catalog_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from selayer_discovery.cli import main

        _init_session(catalog_dir)
        capsys.readouterr()
        rc = main(
            [
                "proposal",
                "verify",
                "--session-id",
                "session-001",
                "--project",
                str(catalog_dir),
                "--proposal",
                "proposal-missing",
            ]
        )
        assert rc == 1
        err = json.loads(capsys.readouterr().err)
        assert err["code"]


# -- catalog helpers for missing-source readiness test --------------------- #


def _missing_source_catalog(tmp_path: Path) -> str:
    return (
        "version: 1\n"
        "name: empty\n"
        "label: Empty\n"
        "description: empty\n"
        "data_sources:\n"
        "  orders:\n"
        "    type: parquet\n"
        f"    location: {tmp_path / 'nope.parquet'}\n"
        "    grain: [id]\n"
        "    schema:\n"
        "      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
    )


def _init_session_with_catalog(tmp_path: Path) -> Path:
    from ruamel.yaml import YAML as _YAML
    from selayer_discovery.cli import main

    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(_missing_source_catalog(tmp_path), encoding="utf-8")
    charter = tmp_path / "charter.yaml"
    charter_data = {
        "business_question": "Is the grain one row per order?",
        "approver": "Dr. Alice Okonkwo",
        "catalog_fingerprint": "a" * 64,
        "inclusions": ["source.shopfloor.orders"],
        "exclusions": ["domain.finance"],
        "acceptance_questions": ["Does the grain pass the audit?"],
    }
    buf = StringIO()
    _YAML().dump(charter_data, buf)
    charter.write_text(buf.getvalue(), encoding="utf-8")
    main(
        [
            "session",
            "init",
            "--charter",
            str(charter),
            "--project",
            str(tmp_path),
            "--catalog-path",
            "catalog.yaml",
            "--session-id",
            "session-001",
        ]
    )
    return tmp_path
