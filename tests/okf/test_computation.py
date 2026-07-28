from pathlib import PurePosixPath

from selayer.okf.computation import attested_computation
from selayer.okf.model import AttestedComputation, OkfConcept, OkfSection


def _concept(frontmatter: dict, sections: tuple[OkfSection, ...] = ()) -> OkfConcept:
    return OkfConcept.create(
        concept_id="c",
        relative_path=PurePosixPath("c.md"),
        frontmatter=frontmatter,
        sections=sections,
    )


def test_non_attested_concept_returns_none() -> None:
    assert attested_computation(_concept({"type": "Metric"})) is None


def test_minimal_attested_computation_derives_empty_contract() -> None:
    contract = attested_computation(
        _concept({"type": "Attested Computation", "runtime": "bigquery"})
    )
    assert contract == AttestedComputation(
        runtime="bigquery",
        parameters=(),
        computation_path=None,
        computation_body="",
        executor_resource=None,
        executor_receipt=(),
        attester_resource=None,
    )


def test_inline_computation_body_is_extracted() -> None:
    concept = _concept(
        {"type": "Attested Computation", "runtime": "python"},
        sections=(
            OkfSection("Computation", "    def decode(mlfb): ..."),
            OkfSection("Meaning", "Interpret documented positions."),
        ),
    )
    contract = attested_computation(concept)
    assert contract is not None
    assert contract.computation_body == "    def decode(mlfb): ..."


def test_file_path_computation_and_contract_fields_are_derived() -> None:
    concept = _concept(
        {
            "type": "Attested Computation",
            "runtime": "dbt",
            "parameters": [
                {"name": "year", "type": "integer", "required": True},
                {"name": "segment", "type": "string"},
            ],
            "computation": "references/computations/profit.sql",
            "executor": {
                "resource": "references/skills/run-dbt.md",
                "receipt": ["run_id", "compiled_sql", "result"],
            },
            "attester": {"resource": "references/attesters/dbt-binding.py"},
        }
    )
    contract = attested_computation(concept)
    assert contract is not None
    assert contract.computation_path == "references/computations/profit.sql"
    assert contract.parameters[0].name == "year"
    assert contract.parameters[0].required is True
    assert contract.parameters[1].required is False
    assert contract.executor_resource == "references/skills/run-dbt.md"
    assert contract.executor_receipt == ("run_id", "compiled_sql", "result")
    assert contract.attester_resource == "references/attesters/dbt-binding.py"
