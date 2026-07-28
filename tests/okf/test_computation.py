from pathlib import Path, PurePosixPath

from selayer.okf import OkfBundle
from selayer.okf.computation import attested_computation
from selayer.okf.model import AttestedComputation, OkfConcept, OkfParameter, OkfSection


def _concept(
    frontmatter: dict, sections: tuple[OkfSection, ...] = ()
) -> OkfConcept:
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


def test_complete_attested_computation_survives_load_write_load_round_trip(
    tmp_path: Path,
) -> None:
    """An authored complete Attested Computation contract with an inline
    ``# Computation`` body must survive load -> write -> load, preserving
    every frontmatter contract value and the derived typed contract/body."""
    source = tmp_path / "in" / "decoder.md"
    source.parent.mkdir()
    source.write_text(
        "---\n"
        "type: Attested Computation\n"
        "title: Revenue Attestation\n"
        "runtime: bigquery\n"
        "status: stable\n"
        "parameters:\n"
        "  - {name: year, type: integer, required: true}\n"
        "  - {name: segment, type: string, required: false}\n"
        "executor:\n"
        "  resource: references/skills/run-bq.md\n"
        "  receipt: [job_id, executed_sql, result]\n"
        "attester:\n"
        "  resource: references/attesters/revenue.py\n"
        "---\n\n"
        "# Computation\n\n"
        "SELECT revenue FROM orders WHERE year = @year\n\n"
        "# Meaning\n\n"
        "Revenue computed from the orders table.\n",
        encoding="utf-8",
    )

    loaded = OkfBundle.load(tmp_path / "in")
    original = loaded.concepts["decoder"]

    out = tmp_path / "out"
    loaded.write(out)

    reloaded = OkfBundle.load(out)
    roundtripped = reloaded.concepts["decoder"]

    # Every authored frontmatter contract value is preserved verbatim.
    frontmatter = roundtripped.frontmatter
    assert frontmatter["type"] == "Attested Computation"
    assert frontmatter["runtime"] == "bigquery"
    assert frontmatter["title"] == "Revenue Attestation"
    assert frontmatter["status"] == "stable"
    assert frontmatter["parameters"] == (
        {"name": "year", "type": "integer", "required": True},
        {"name": "segment", "type": "string", "required": False},
    )
    assert frontmatter["executor"] == {
        "resource": "references/skills/run-bq.md",
        "receipt": ("job_id", "executed_sql", "result"),
    }
    assert frontmatter["attester"] == {"resource": "references/attesters/revenue.py"}
    # Unknown/extension frontmatter is preserved too.
    assert "computation" not in frontmatter

    # The derived typed contract and body survive the round-trip identically.
    assert attested_computation(roundtripped) == attested_computation(original)
    contract = attested_computation(roundtripped)
    assert contract is not None
    assert contract.runtime == "bigquery"
    assert contract.parameters == (
        OkfParameter(name="year", type="integer", required=True),
        OkfParameter(name="segment", type="string", required=False),
    )
    assert contract.computation_path is None
    assert contract.computation_body == "SELECT revenue FROM orders WHERE year = @year"
    assert contract.executor_resource == "references/skills/run-bq.md"
    assert contract.executor_receipt == ("job_id", "executed_sql", "result")
    assert contract.attester_resource == "references/attesters/revenue.py"
