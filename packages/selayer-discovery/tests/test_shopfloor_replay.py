from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

_REPLAY = Path(__file__).parents[3] / "examples" / "shopfloor" / "discovery" / "replay"


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
    defect = cast(dict[str, Any], yaml.safe_load((_REPLAY / "defect.yaml").read_text(encoding="utf-8")))
    assert defect == {
        "version": 1,
        "target": "dimension.drive_serial_number",
        "replace": {"source": "operation_executions", "column": "serial_number"},
    }


def test_replay_interview_has_one_correction_and_no_raw_transcript() -> None:
    events = [
        json.loads(line)
        for line in (_REPLAY / "interview.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in events] == ["question", "answer", "correction"]
    assert events[-1]["supersedes"] == events[1]["event_id"]
    assert all("tool" not in event.get("answer", "").lower() for event in events)


def test_replay_policy_is_default_omit_and_bounded() -> None:
    policy = cast(dict[str, Any], yaml.safe_load((_REPLAY / "policy.yaml").read_text(encoding="utf-8")))
    assert policy["salt_ref"] == "SHOPFLOOR_DISCOVERY_SALT"
    assert policy["fields"]["customer_name"]["transform"] == "omit"
    assert policy["fields"]["serial_number"]["transform"] == "hash"
    assert policy["limits"]["rows_per_source"] == 20
    assert policy["reveals"] == []


def test_replay_proposal_keeps_telemetry_group_blocked() -> None:
    proposal = cast(dict[str, Any], yaml.safe_load((_REPLAY / "proposal.yaml").read_text(encoding="utf-8")))
    groups = {group["group_id"]: group for group in proposal["groups"]}
    assert groups["group-conformed-drive"]["conflict_ids"] == []
    assert groups["group-telemetry-join"]["conflict_ids"] == [
        "conflict-operation-telemetry"
    ]
    rendered = cast(str, yaml.safe_dump(proposal))
    assert "operation-to-telemetry event joins" not in rendered


def test_replay_wiki_queries_are_bounded_and_read_only() -> None:
    query = cast(dict[str, Any], json.loads((_REPLAY / "wiki-query.json").read_text(encoding="utf-8")))
    assert query["provider"] == "okf-filesystem"
    assert all(item["max_results"] == 5 for item in query["queries"])
    assert all("write" not in item["text"].lower() for item in query["queries"])
