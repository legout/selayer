# Shopfloor example hardening design

## Purpose

Turn `examples/shopfloor` into a small, deterministic reference for grain-aware semantic modeling, physical source verification, safe planning, OKF generation, and authored business context.

The example will drive acceptance requirements for later selayer verification and agent-assisted authoring work. It will not implement the general verification framework or agent workflow itself.

## Context

The current example already demonstrates eight physical sources, six declared relationships, twelve metrics, safe child-to-parent traversal, mixed-grain rejection, and Delta reloads. The catalog and current physical data are internally consistent. The main weaknesses are in how the example communicates and preserves those semantics:

- `run_example.py` appends an EOL retest to the repository Delta table, leaving the local fixture in a post-reload state.
- `dimension.drive_serial_number` is anchored on component consumption even though serialized drives are the registry for drive identity.
- Operation and telemetry machine and line identifiers look conformed, but no safe relationship or temporal rule connects the event domains.
- The catalog exposes little time context.
- Grain, relationship, and business invariants are not verified by the example tests.
- The generated OKF bundle contains empty guidance sections and is not a versioned source artifact.
- No small body of reviewed business documentation explains why the modeled facts and metrics mean what they do.

## Goals

- Keep the example small enough to inspect by hand.
- Preserve the multi-connector demonstration.
- Make the baseline fixture reproducible and leave it unchanged after the walkthrough.
- Clarify conformed identities and deliberately isolated domains.
- Add only time dimensions supported by real source columns.
- Test grains, relationship cardinalities, referential integrity, and selected business rules.
- Keep the twelve headline metrics and their baseline meanings.
- Generate OKF on demand rather than committing generated pages.
- Version reviewed business-context documents and sparse OKF overlays.
- Make the generated versus authored ownership rule enforceable.
- Supply acceptance cases for later reusable library features.

## Non-goals

- A realistic manufacturing simulator.
- More connectors or a second execution engine.
- New many-to-many planning, re-graining, or allocation behavior.
- Joining telemetry events directly to operation events.
- Inventing event timestamps absent from the source data.
- A general source profiler or validation framework.
- Agent-driven document ingestion or autonomous semantic correction.
- Treating OKF prose as executable authority.
- Committing the generated OKF bundle.

## Ownership model

The example has four versioned inputs:

1. `generate_data.py` and the baseline fixture define the physical data.
2. `schemas/` and `shopfloor_semantic_layer.yaml` define executable semantics.
3. `business_context/` defines the reviewed example story and business rules.
4. `okf_overlays/` defines authored guidance for catalog-backed OKF concepts.

An on-demand build generates a fresh OKF bundle from the catalog, adds the reviewed reference documents, applies the overlays, validates the complete bundle, and publishes it only after every check succeeds.

Generated output is disposable. Authors never edit it as source.

## Proposed source tree

```text
examples/shopfloor/
├── business_context/
│   ├── glossary.md
│   ├── kpi_definitions.md
│   ├── process_overview.md
│   └── quality_policy.md
├── data/
├── okf_overlays/
│   ├── metrics/
│   ├── relationships/
│   ├── sources/
│   └── selected_concepts/
├── schemas/
├── build_knowledge.py
├── generate_data.py
├── run_example.py
├── shopfloor_semantic_layer.yaml
└── README.md
```

Generated knowledge goes to `examples/shopfloor/.generated/knowledge/` by default. `.generated/` is ignored by Git. The build command also accepts an explicit output directory for tests and external consumers.

The current untracked `examples/shopfloor/knowledge/` tree is replaced by the versioned overlays and on-demand output.

## Baseline data and reload walkthrough

`generate_shopfloor_data()` continues to produce the same logical baseline on every run. The baseline contains three EOL attempts:

- DRV-001 passes on its first attempt.
- DRV-002 passes on its first attempt.
- DRV-003 fails its first attempt.

The baseline EOL attempt pass rate and first-pass yield are both `2/3`.

The reload walkthrough must not append to the repository Delta table. It copies the mutable EOL source and a rebased catalog into a temporary directory, appends the deterministic DRV-003 retest there, reloads that temporary source, and demonstrates:

- EOL attempt pass rate changes from `2/3` to `3/4`.
- First-pass yield remains `2/3`.
- The repository baseline remains at three attempts.

Tests compare logical table content and Delta version or row count where relevant. They do not require byte-identical database or Delta metadata.

`generate_data.py` gains a small command-line entry point with an explicit output directory. The generation function remains importable for tests.

## Semantic-model changes

### Conformed drive identity

Move `dimension.drive_serial_number` to `serialized_drives.serial_number`.

Serialized drives are the registry for drive identity. Component consumption, operation executions, and EOL runs reach the dimension through existing grain-preserving child-to-parent paths. Production-order metrics still cannot group by a child drive without row expansion, which is the correct planner behavior.

Tests cover drive grouping and filtering from the component, operation, and EOL grains.

### Operation and telemetry identity

Rename operation-local dimensions:

- `line_id` becomes `operation_line_id`.
- `machine_id` becomes `operation_machine_id`.

Keep or add telemetry-local dimensions:

- `telemetry_line_id`
- `telemetry_machine_id`

No relationship connects operation executions to telemetry. Matching identifier values do not establish a safe event-to-event join. A future model would need a real machine registry and explicit temporal semantics before these dimensions could be conformed.

Rename telemetry row-marker facts where needed so they read as event markers rather than conformed entity definitions. The telemetry and alarm metric meanings do not change.

### Supported time dimensions

Add:

- `requested_ship_date` from customer orders, with date type.
- `telemetry_recorded_at` from machine telemetry, with timestamp type.

Do not add operation, inspection, EOL, completion, or shipment time dimensions until the data generator supplies trustworthy event columns.

### Metrics

Keep the twelve existing headline metrics and their baseline formulas. The overlays document each metric's native grain, unit, count behavior, denominator behavior, supported grouping examples, and known restrictions.

`component_count` continues to mean fitted component rows. It is not a distinct physical-component count. The source grain and required component serial field make `count` correct for this fixture, and the caveat states that meaning explicitly.

## Business-context corpus

The example includes four short Markdown documents.

### Process overview

Describes the order, production, serialization, component fitting, operation, EOL, shipment, and telemetry flows. It distinguishes registry records from events and explains why telemetry is isolated.

### KPI definitions

Defines all twelve metrics. Each definition names the unit, numerator, denominator, inclusion rule, entity or event grain, and zero-denominator behavior.

### Quality policy

Defines accepted incoming inspection, rework classification, first-attempt pass behavior, EOL attempts, and release rules. It states the cross-field rules enforced by tests.

### Glossary

Defines the canonical terms used by the catalog and OKF bundle. Important distinctions include attempt versus unit, event versus entity, production completion versus shipment, and operation machine versus telemetry machine.

Each document is valid authored OKF `Reference` content with stable title, type, status, document ID, and ownership metadata. The document body remains ordinary Markdown. During the on-demand build, the files are copied into `references/` in the bundle.

The corpus is human-authored fixture content. This design does not claim that an agent extracted or verified it.

## OKF overlay contract

Overlays are sparse Markdown files keyed by typed `selayer_id`. Their directory structure mirrors the generated concept paths.

An overlay may provide:

- `selayer_id`
- approved source or provenance metadata
- `Usage Guidance`
- `Examples`
- `Caveats`
- `Related Concepts`

An overlay may not provide or replace:

- `type`
- `title`
- `description`
- `generated`
- `Catalog Definition`
- indexes
- the root log
- human verification claims

The build rejects unknown IDs, duplicate IDs, duplicate curated headings, unsupported headings, controlled frontmatter, orphan overlays, missing references, self-links, duplicate related links, and broken internal links.

### Coverage policy

All twelve metrics require non-empty Usage Guidance, Examples, Caveats, and Related Concepts sections.

All eight sources require Usage Guidance and Caveats.

All six relationships require Usage Guidance, Caveats, and Related Concepts.

Dimensions, facts, and measures need overlays only when their semantics are not obvious from the generated definition. The first version includes overlays for drive identity, telemetry event markers, first-attempt and first-pass concepts, component counting, and other concepts referenced by metric caveats.

Examples use public `QueryEngine` calls or clearly labeled natural-language questions. Executable examples must plan against the current catalog. Numeric expected values require support from the deterministic fixture tests.

## Knowledge build

The intended command is:

```bash
uv run python examples/shopfloor/build_knowledge.py \
  --output-dir examples/shopfloor/.generated/knowledge
```

The build runs in a temporary staging directory:

1. Load the catalog.
2. Generate a fresh OKF bundle.
3. Add the authored Reference documents.
4. Apply overlays through a restricted merge operation.
5. Load and validate the complete bundle against the catalog.
6. Check coverage and executable examples.
7. Move the staging tree to the requested empty destination.

The command refuses an existing non-empty destination. It never treats edits in generated output as authored input.

The restricted overlay merge is a required library interface for the later selayer verification design. The shopfloor implementation should consume that interface rather than add a second Markdown merge engine under `examples/`.

## Failure behavior

The data generator fails before returning a usable path set when a connector dependency or write fails.

The reload walkthrough uses a temporary directory and removes it on normal completion or failure. A failed reload preserves the previous registered source, as required by the existing library contract.

The knowledge build publishes no destination if generation, overlay application, validation, coverage checks, or example planning fails. Diagnostics identify the semantic ID, source overlay, and failing field or heading. Diagnostic order is stable.

No build or validation command reads unrestricted source rows into OKF, logs credentials, follows external links, or executes code found in business documents.

## Test strategy

### Data-generation tests

- Generate every connector input in a temporary directory.
- Assert the logical baseline rows and schema.
- Assert three baseline EOL attempts.
- Assert repeated generation resets an appended retest.
- Assert the walkthrough leaves repository fixtures unchanged.

### Physical semantic checks

- Every grain tuple is non-null and unique.
- Every one-side relationship key is non-null and unique.
- Every non-null target key resolves to the declared source side.
- Zero-child source keys are reported but accepted.
- Join-column logical types match.

### Business invariant tests

- Completed units do not exceed planned units.
- Attempts are positive and `(serial_number, attempt)` is unique.
- `is_first_pass` is true exactly for attempt one with a passing result.
- Passing inspections are released and failing inspections are quarantined.
- Result, shipment, state, and schedule values stay within the documented domains.

### Catalog and planner tests

- The catalog loads without diagnostics.
- All twelve baseline metrics match independent expected values.
- Drive filtering and grouping work from component, operation, and EOL grains.
- Production metrics cannot group through a row-expanding drive path.
- Operation and telemetry dimensions remain isolated.
- The intended mixed-grain metric combinations fail with stable planner codes.
- The two supported time dimensions plan only where their source paths are safe.

### OKF build tests

- A clean on-demand build succeeds.
- A non-empty destination is rejected.
- The generated semantic concept count matches the catalog.
- Four Reference concepts are present.
- Required overlay coverage is complete.
- Internal links and references resolve.
- Executable examples plan.
- Controlled generated regions match the catalog.
- Forbidden overlay edits fail without partial output.
- The resulting bundle contains no source values beyond reviewed fixture examples.

## Documentation

Update `examples/shopfloor/README.md` to explain:

- the baseline and temporary retest states;
- the corrected semantic identifiers;
- why telemetry remains disconnected;
- the business-context files;
- how to generate the OKF bundle;
- which files are generated and which are authored;
- the verification commands and expected headline values.

The README links to the later library and agent-assistance designs once those exist.

## Relationship to later work

This design establishes concrete acceptance cases for two later designs.

The selayer verification design will generalize catalog diagnostics, physical grain and relationship checks, planner compatibility reporting, catalog-aware OKF integrity, and restricted overlay application.

The agent-assisted semantic discovery design will cover interviews, document ingestion, evidence tracking, semantic proposals, correction workflows, and agent-authored overlay drafts. It will treat unstructured documents as evidence, not executable authority.

The specification order is:

1. Complete and approve this shopfloor design.
2. Write the selayer verification design using these acceptance requirements.
3. Write the agent-assisted semantic discovery design after the deterministic interfaces are settled.

Implementation plans follow the same dependency order, but library verification work may need to land before the shopfloor knowledge builder can consume the restricted overlay interface.
