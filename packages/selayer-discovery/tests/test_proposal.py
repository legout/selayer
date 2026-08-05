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
                pa.field("status", pa.string()),
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
        "affecting_gates": ["gate-grain"],
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
        assert group.affecting_gates == ("gate-grain",)
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
        assert "catalog_patch" in out
        assert "preview_fingerprint" in out

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
