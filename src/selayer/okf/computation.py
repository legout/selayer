from __future__ import annotations

from collections.abc import Mapping

from .model import AttestedComputation, OkfConcept, OkfParameter

_COMPUTATION_SECTION = "Computation"


def _computation_body(concept: OkfConcept) -> str:
    for section in concept.sections:
        if section.title == _COMPUTATION_SECTION:
            return section.content
    return ""


def _parameters(frontmatter: Mapping[str, object]) -> tuple[OkfParameter, ...]:
    raw = frontmatter.get("parameters")
    if not isinstance(raw, (list, tuple)):
        return ()
    derived: list[OkfParameter] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        param_type = entry.get("type")
        if not isinstance(name, str) or not isinstance(param_type, str):
            continue
        required = entry.get("required", False)
        derived.append(OkfParameter(name=name, type=param_type, required=bool(required)))
    return tuple(derived)


def _executor(
    frontmatter: Mapping[str, object],
) -> tuple[str | None, tuple[str, ...]]:
    executor = frontmatter.get("executor")
    if not isinstance(executor, Mapping):
        return None, ()
    resource = executor.get("resource")
    receipt = executor.get("receipt")
    resource_value = resource if isinstance(resource, str) else None
    receipt_value = (
        tuple(item for item in receipt if isinstance(item, str))
        if isinstance(receipt, (list, tuple))
        else ()
    )
    return resource_value, receipt_value


def _attester(frontmatter: Mapping[str, object]) -> str | None:
    attester = frontmatter.get("attester")
    if not isinstance(attester, Mapping):
        return None
    resource = attester.get("resource")
    return resource if isinstance(resource, str) else None


def attested_computation(concept: OkfConcept) -> AttestedComputation | None:
    """Derive the typed Attested Computation contract, or None for other types."""
    if concept.frontmatter.get("type") != "Attested Computation":
        return None
    frontmatter = concept.frontmatter
    runtime = frontmatter.get("runtime")
    computation = frontmatter.get("computation")
    executor_resource, executor_receipt = _executor(frontmatter)
    return AttestedComputation(
        runtime=runtime if isinstance(runtime, str) else "",
        parameters=_parameters(frontmatter),
        computation_path=computation if isinstance(computation, str) else None,
        computation_body=_computation_body(concept),
        executor_resource=executor_resource,
        executor_receipt=executor_receipt,
        attester_resource=_attester(frontmatter),
    )


__all__ = ["attested_computation"]
