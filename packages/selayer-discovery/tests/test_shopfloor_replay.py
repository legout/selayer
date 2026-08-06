from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

import yaml
from selayer_discovery.canonical import fingerprint
from selayer_discovery.evidence import (
    ClaimStore,
    EvidenceClass,
    EvidenceStore,
    selector_from_mapping,
)
from selayer_discovery.model import SCHEMA_VERSION
from selayer_discovery.session import SessionCharter, SessionStore

from selayer import SemanticLayer
from selayer.okf import OkfBundle
from selayer.verification import verify_static

_REPLAY = Path(__file__).parents[3] / "examples" / "shopfloor" / "discovery" / "replay"


def _snapshot_tree(path: Path) -> tuple[tuple[str, bytes], ...]:
    if not path.exists():
        return ()
    return tuple(
        sorted(
            (str(child.relative_to(path)), child.read_bytes())
            for child in path.rglob("*")
            if child.is_file()
        )
    )


def test_replay_artifacts_are_present_and_safe() -> None:
    expected = {
        "defect.yaml",
        "interview.jsonl",
        "policy.yaml",
        "proposal.yaml",
        "wiki-query.json",
    }
    assert {path.name for path in _REPLAY.iterdir()} == expected
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in _REPLAY.iterdir())
    assert "subprocess" not in rendered
    assert "secret" not in rendered.lower()
    assert "apply now" not in rendered.lower()


def test_replay_declares_exact_reversible_defect() -> None:
    defect = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "defect.yaml").read_text(encoding="utf-8")),
    )
    assert defect == {
        "version": 1,
        "target": "dimension.drive_serial_number",
        "replace": {"source": "operation_executions", "column": "serial_number"},
    }


def test_replay_interview_has_one_correction_and_no_raw_transcript() -> None:
    events = [
        json.loads(line)
        for line in (_REPLAY / "interview.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["type"] for event in events] == ["question", "answer", "correction"]
    assert events[-1]["supersedes"] == events[1]["event_id"]
    assert all("tool" not in event.get("answer", "").lower() for event in events)


def test_replay_policy_is_default_omit_and_bounded() -> None:
    policy = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "policy.yaml").read_text(encoding="utf-8")),
    )
    assert policy["salt_ref"] == "SHOPFLOOR_DISCOVERY_SALT"
    assert policy["fields"]["customer_name"]["transform"] == "omit"
    assert policy["fields"]["serial_number"]["transform"] == "hash"
    assert policy["limits"]["rows_per_source"] == 20
    assert policy["reveals"] == []


def test_replay_proposal_keeps_telemetry_group_blocked() -> None:
    proposal = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "proposal.yaml").read_text(encoding="utf-8")),
    )
    groups = {group["group_id"]: group for group in proposal["groups"]}
    assert groups["group-conformed-drive"]["conflict_ids"] == []
    assert groups["group-telemetry-join"]["conflict_ids"] == [
        "conflict-operation-telemetry"
    ]
    rendered = cast(str, yaml.safe_dump(proposal))
    assert "operation-to-telemetry event joins" not in rendered


def test_replay_wiki_queries_are_bounded_and_read_only() -> None:
    query = cast(
        dict[str, Any],
        json.loads((_REPLAY / "wiki-query.json").read_text(encoding="utf-8")),
    )
    assert query["provider"] == "okf-filesystem"
    assert all(item["max_results"] == 5 for item in query["queries"])
    assert all("write" not in item["text"].lower() for item in query["queries"])


def test_replay_runs_temporary_catalog_okf_and_typed_evidence_flow(
    tmp_path: Path,
) -> None:
    """Exercise the replay's deterministic core without a model or network."""

    root = _REPLAY.parents[1]
    repository_root = root.parent.parent
    repository_changes = repository_root / "semantic_changes"
    repository_before = _snapshot_tree(repository_changes)
    catalog_source = root / "shopfloor_semantic_layer.yaml"
    catalog = cast(
        dict[str, Any], yaml.safe_load(catalog_source.read_text(encoding="utf-8"))
    )
    defect = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "defect.yaml").read_text(encoding="utf-8")),
    )
    for source in catalog["data_sources"].values():
        schema_ref = source.pop("schema_ref")
        source["schema"] = yaml.safe_load(
            (root / "schemas" / Path(schema_ref).name).read_text(encoding="utf-8")
        )
    target = catalog["dimensions"]["drive_serial_number"]
    before = dict(target)
    target.update(defect["replace"])
    defective_catalog = tmp_path / "defective.yaml"
    defective_catalog.write_text(
        cast(str, yaml.safe_dump(catalog, sort_keys=False)), encoding="utf-8"
    )
    defective_layer = SemanticLayer.load(defective_catalog)
    assert before != target
    assert verify_static(defective_layer).complete
    defective_bundle = OkfBundle.build(
        defective_layer,
        tmp_path / "okf-defective",
        references_dir=root / "business_context",
        overlays_dir=root / "okf_overlays",
    )
    assert defective_bundle.concepts

    replay_proposal = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "proposal.yaml").read_text(encoding="utf-8")),
    )
    correction = replay_proposal["groups"][0]["operations"][0]["after"]
    target.clear()
    target.update(correction)
    corrected_catalog = tmp_path / "shopfloor.yaml"
    corrected_catalog.write_text(
        cast(str, yaml.safe_dump(catalog, sort_keys=False)), encoding="utf-8"
    )
    corrected_layer = SemanticLayer.load(corrected_catalog)
    assert (
        corrected_layer.dimension("drive_serial_number").source == "serialized_drives"
    )
    assert verify_static(corrected_layer).complete
    bundle = OkfBundle.build(
        corrected_layer,
        tmp_path / "okf",
        references_dir=root / "business_context",
        overlays_dir=root / "okf_overlays",
    )
    assert bundle.concepts

    charter = SessionCharter(
        schema_version=SCHEMA_VERSION,
        session_id="shopfloor-replay",
        business_question="Safe drive-level component and EOL analysis",
        catalog_fingerprint=fingerprint(corrected_catalog.read_text(encoding="utf-8")),
        catalog_path="shopfloor.yaml",
        approver="Dr Alice Okonkwo",
        inclusions=("dimension.drive_serial_number",),
        exclusions=("operation-to-telemetry event joins",),
        acceptance_questions=("Does conformed drive identity hold?",),
    )
    session = SessionStore.create(
        tmp_path / "session", charter=charter, actor=charter.approver
    )
    evidence = EvidenceStore.create(tmp_path / "evidence")
    records = []
    for index in range(4):
        document = tmp_path / f"business-{index}.txt"
        document.write_text(f"business document {index}\n", encoding="utf-8")
        records.append(evidence.add_document(document, allowed_roots=(tmp_path,)))
    okf_record = evidence.add_snapshot(
        b"filesystem OKF evidence\n",
        media_type="text/plain",
        source="okf-filesystem",
    )
    claims = ClaimStore.create(session, evidence)
    creator = "charter"
    for index, (record, evidence_class) in enumerate(
        zip(
            records[:3],
            (EvidenceClass.OBSERVED, EvidenceClass.ASSERTED, EvidenceClass.INFERRED),
            strict=True,
        ),
        start=1,
    ):
        selector = selector_from_mapping(
            {
                "kind": "document_line_range",
                "record_id": record.record_id,
                "content_hash": record.content_hash,
                "revision": record.revision,
                "start_line": 1,
                "end_line": 1,
            }
        )
        claims.add_claim(
            claim_id=f"claim-replay-{index}",
            subject="dimension.drive_serial_number",
            statement=f"Replay claim {index} is bounded to one evidence snapshot.",
            evidence_class=evidence_class,
            selectors=(selector,),
            creator_event=creator,
            actor=charter.approver,
        )
    assert okf_record.content_hash
    assert len(claims.claims()) == 3
    final_bundle = OkfBundle.build(
        corrected_layer,
        tmp_path / "okf-final",
        references_dir=root / "business_context",
        overlays_dir=root / "okf_overlays",
    )
    assert final_bundle.concepts
    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "okf-final").rglob("*.md")
    )
    assert "filesystem OKF evidence" not in generated
    assert all(f"business document {index}" not in generated for index in range(4))
    assert _snapshot_tree(repository_changes) == repository_before

    source_root = repository_root / "packages" / "selayer-discovery" / "src"
    forbidden_modules = {"subprocess", "requests", "httpx", "socket", "openai", "anthropic"}
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not imports & forbidden_modules, source_path
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"os", "subprocess"}
            and node.func.attr in {"system", "popen", "run", "Popen", "check_output"}
            for node in ast.walk(tree)
        ), source_path
