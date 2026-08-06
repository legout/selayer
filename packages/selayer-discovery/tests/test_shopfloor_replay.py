from __future__ import annotations

import ast
import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml
from selayer_discovery.canonical import fingerprint
from selayer_discovery.cli import _runtime_profile_resolver, main
from selayer_discovery.evidence import (
    ClaimStore,
    EvidenceClass,
    EvidenceStore,
    selector_from_mapping,
)
from selayer_discovery.interview import InterviewStore
from selayer_discovery.model import SCHEMA_VERSION
from selayer_discovery.session import SessionCharter, SessionStore

from selayer import SemanticLayer
from selayer.okf import OkfBundle
from selayer.verification import verify_static

_REPLAY = Path(__file__).parents[3] / "examples" / "shopfloor" / "discovery" / "replay"


def _generate_shopfloor_data(path: Path) -> Any:
    namespace = runpy.run_path(str(_REPLAY.parents[1] / "generate_data.py"))
    generator = cast(Callable[[Path], Any], namespace["generate_shopfloor_data"])
    return generator(path)


def _snapshot_tree(path: Path) -> tuple[tuple[str, bytes], ...]:
    if not path.exists():
        return ()
    ignored = {".git", ".pytest_cache", "__pycache__", "build", "dist"}
    return tuple(
        sorted(
            (str(child.relative_to(path)), child.read_bytes())
            for child in path.rglob("*")
            if child.is_file() and not any(part in ignored for part in child.parts)
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
    tmp_path: Path, capsys: Any
) -> None:
    """Exercise the replay's deterministic core without a model or network."""

    root = _REPLAY.parents[1]
    repository_root = root.parent.parent
    repository_before = _snapshot_tree(repository_root)
    catalog_source = root / "shopfloor_semantic_layer.yaml"
    catalog = cast(
        dict[str, Any], yaml.safe_load(catalog_source.read_text(encoding="utf-8"))
    )
    defect = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "defect.yaml").read_text(encoding="utf-8")),
    )
    data_paths = _generate_shopfloor_data(tmp_path / "data")
    generated_locations = {
        "customer_orders": data_paths.customer_orders,
        "production_orders": data_paths.production_orders_db,
        "serialized_drives": data_paths.shopfloor_db,
        "component_consumption": data_paths.component_consumption,
        "component_lot_inspections": data_paths.component_lot_inspections,
        "operation_executions": data_paths.operation_executions,
        "machine_telemetry": data_paths.machine_telemetry,
        "eol_test_runs": data_paths.eol_test_runs,
    }
    for source_id, source in catalog["data_sources"].items():
        schema_ref = source.pop("schema_ref")
        source["schema"] = yaml.safe_load(
            (root / "schemas" / Path(schema_ref).name).read_text(encoding="utf-8")
        )
        source["location"] = str(generated_locations[source_id])
    target = catalog["dimensions"]["drive_serial_number"]
    before = dict(target)
    target.update(defect["replace"])
    defective_state = dict(target)
    defective_catalog = tmp_path / "shopfloor.yaml"
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
    corrected_catalog = tmp_path / "expected-corrected.yaml"
    corrected_catalog.write_text(
        cast(str, yaml.safe_dump(catalog, sort_keys=False)), encoding="utf-8"
    )
    corrected_layer = SemanticLayer.load(corrected_catalog)
    corrected_dimension = corrected_layer.dimension("drive_serial_number")
    assert corrected_dimension.source == correction["source"]
    assert corrected_dimension.column == correction["column"]
    assert corrected_dimension.data_type == correction["data_type"]
    assert verify_static(corrected_layer).complete
    bundle = OkfBundle.build(
        corrected_layer,
        tmp_path / "okf",
        references_dir=root / "business_context",
        overlays_dir=root / "okf_overlays",
    )
    assert bundle.concepts

    runtime_profile_path = tmp_path / "runtime-profiles.yaml"
    runtime_profile_path.write_text(
        "version: 1\nprofiles:\n  shopfloor_readonly:\n"
        "    allow_extension_install:\n      literal: false\n",
        encoding="utf-8",
    )
    charter = SessionCharter(
        schema_version=SCHEMA_VERSION,
        session_id="shopfloor-replay",
        business_question="Safe drive-level component and EOL analysis",
        catalog_fingerprint=fingerprint(defective_catalog.read_text(encoding="utf-8")),
        catalog_path="shopfloor.yaml",
        runtime_profile="runtime-profiles.yaml",
        approver="Dr Alice Okonkwo",
        inclusions=("dimension.drive_serial_number",),
        exclusions=("operation-to-telemetry event joins",),
        acceptance_questions=("Does conformed drive identity hold?",),
    )
    session_dir = (
        tmp_path
        / ".selayer"
        / "discovery"
        / "sessions"
        / charter.session_id
    )
    resolver = _runtime_profile_resolver(tmp_path, charter)
    assert (
        resolver.resolve("shopfloor_readonly", source_id="shopfloor").name
        == "shopfloor_readonly"
    )
    session = SessionStore.create(
        session_dir, charter=charter, actor=charter.approver
    )
    evidence = EvidenceStore.create(session_dir / "evidence")
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
    interview = InterviewStore.create(session)
    question = interview.ask(
        gate="gate-grains",
        text="Does the drive identity preserve the declared grain?",
        evidence_ids=(records[0].record_id,),
        subjects=("dimension.drive_serial_number",),
        actor=charter.approver,
    )
    answer = interview.answer(
        gate="gate-grains",
        text="The drive identity remains one row per serialized drive.",
        actor=charter.approver,
    )
    interview.correct(
        answer_id=answer.answer.answer_id,
        reason="Clarify the source audit scope.",
        replacement="The drive identity remains one row per serialized drive after source audit.",
        actor=charter.approver,
    )
    assert question.question_id

    claims = ClaimStore.create(session, evidence)
    creator = "charter"
    claim_specs = (
        (
            "claim-drive-identity",
            okf_record,
            {
                "kind": "source_field",
                "field": "serial_number",
            },
            EvidenceClass.OBSERVED,
        ),
        (
            "claim-replay-2",
            records[1],
            {"kind": "document_line_range", "start_line": 1, "end_line": 1},
            EvidenceClass.ASSERTED,
        ),
        (
            "claim-replay-3",
            records[2],
            {"kind": "document_line_range", "start_line": 1, "end_line": 1},
            EvidenceClass.INFERRED,
        ),
    )
    for claim_id, record, selector_fields, evidence_class in claim_specs:
        selector = selector_from_mapping(
            {
                **selector_fields,
                "record_id": record.record_id,
                "content_hash": record.content_hash,
                "revision": record.revision,
            }
        )
        claims.add_claim(
            claim_id=claim_id,
            subject="dimension.drive_serial_number",
            statement=f"Replay claim {claim_id} is bounded to one evidence snapshot.",
            evidence_class=evidence_class,
            selectors=(selector,),
            creator_event=creator,
            actor=charter.approver,
        )
    assert okf_record.content_hash
    assert len(claims.claims()) == 3

    proposal_data = cast(
        dict[str, Any],
        yaml.safe_load((_REPLAY / "proposal.yaml").read_text(encoding="utf-8")),
    )
    proposal_operation = proposal_data["groups"][0]["operations"][0]
    proposal_operation["before"] = defective_state
    proposal_operation["before_hash"] = fingerprint(defective_state)
    proposal_operation["after"] = dict(target)
    proposal_operation["claim_ids"] = ["claim-drive-identity"]
    proposal_path = tmp_path / "proposal.yaml"
    proposal_path.write_text(
        cast(str, yaml.safe_dump(proposal_data, sort_keys=False)), encoding="utf-8"
    )
    common = ["--project", str(tmp_path), "--session-id", charter.session_id]
    import_result = main(
        ["proposal", "import", *common, "--proposal", str(proposal_path)]
    )
    assert import_result == 0, capsys.readouterr().err
    verify_result = main(
        ["proposal", "verify", *common, "--proposal", proposal_data["proposal_id"]]
    )
    assert verify_result == 0, capsys.readouterr().err
    attest_result = main(
        [
            "proposal",
            "attest",
            *common,
            "--proposal",
            proposal_data["proposal_id"],
            "--group",
            "group-conformed-drive",
            "--approver",
            charter.approver,
        ]
    )
    assert attest_result == 0, capsys.readouterr().err
    prepare_result = main(
        [
            "proposal",
            "prepare-apply",
            *common,
            "--proposal",
            proposal_data["proposal_id"],
            "--group",
            "group-conformed-drive",
        ]
    )
    assert prepare_result == 0, capsys.readouterr().err
    proposal_dir = (
        tmp_path
        / ".selayer"
        / "discovery"
        / "sessions"
        / charter.session_id
        / "proposals"
        / proposal_data["proposal_id"]
    )
    batch = cast(
        dict[str, Any],
        json.loads((proposal_dir / "prepared-batch.json").read_text(encoding="utf-8")),
    )
    assert (
        main(
            [
                "proposal",
                "attest-apply",
                *common,
                "--proposal",
                proposal_data["proposal_id"],
                "--batch",
                batch["fingerprint"],
                "--approver",
                charter.approver,
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "proposal",
                "export-preview",
                *common,
                "--proposal",
                proposal_data["proposal_id"],
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "proposal",
                "apply",
                *common,
                "--proposal",
                proposal_data["proposal_id"],
                "--approver",
                charter.approver,
            ]
        )
        == 0
    )
    applied_layer = SemanticLayer.load(defective_catalog)
    assert applied_layer.dimension("drive_serial_number").source == "serialized_drives"
    assert "operation_telemetry_join" not in applied_layer.dimensions

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
    assert _snapshot_tree(repository_root) == repository_before

    source_root = repository_root / "packages" / "selayer-discovery" / "src"
    forbidden_modules = {"subprocess", "requests", "httpx", "socket", "openai", "anthropic"}
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert not imports & forbidden_modules, source_path
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"os", "subprocess"}
            and node.func.attr in {"system", "popen", "run", "Popen", "check_output"}
            for node in ast.walk(tree)
        ), source_path
