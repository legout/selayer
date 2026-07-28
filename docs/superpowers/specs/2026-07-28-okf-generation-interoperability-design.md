# OKF Generation Interoperability Design

## Purpose

Make selayer-generated OKF bundles easier for generic OKF v0.2 consumers to understand while retaining read and synchronization compatibility with existing selayer bundles.

This design covers generated type names, index descriptions and links, fingerprint relocation, and canonical `resource` values for catalog-backed sources and dimensions.

## Goals

- Generate recognizable generic concept type names.
- Continue accepting legacy `Selayer …` type names.
- Add concept descriptions to generated indexes when available.
- Generate stable bundle-absolute links.
- Move selayer's synchronization fingerprint out of the standard `generated` family.
- Upgrade legacy fingerprints without losing edit-conflict protection.
- Generate unique physical resource identifiers for sources and dimensions.
- Preserve deterministic output and synchronization behavior.

## Non-goals

- A central type registry.
- Rewriting arbitrary authored concept types.
- Generating resources for facts, measures, metrics, or relationships that describe abstract semantics.
- Resolving or dereferencing physical resources.
- Changing catalog authority, planning, or compilation behavior.
- Adding dependencies.

## Canonical and legacy type names

Newly generated catalog concepts use:

```python
_KIND_TYPES = {
    "source": "Data Source",
    "dimension": "Dimension",
    "fact": "Fact",
    "measure": "Measure",
    "metric": "Metric",
    "relationship": "Relationship",
}
```

Existing bundles may contain the legacy names:

```python
_LEGACY_KIND_TYPES = {
    "source": "Selayer Data Source",
    "dimension": "Selayer Dimension",
    "fact": "Selayer Fact",
    "measure": "Selayer Measure",
    "metric": "Selayer Metric",
    "relationship": "Selayer Relationship",
}
```

`selayer_id` binding validation accepts either the canonical or matching legacy type. It still rejects a mismatched semantic kind, such as `type: Metric` with `selayer_id: dimension.region`.

Unknown producer-defined types remain accepted when they do not claim a `selayer_id` binding. Generated output writes only canonical names; successful sync naturally upgrades legacy generated documents.

## Index entries

`index_documents()` renders entries as:

```markdown
* [Title](/metrics/gross_margin.md) - One-line description
```

Rules:

- Every generated link begins with `/` and is relative to the bundle root.
- Root and per-kind indexes both use the same absolute target path.
- Append `- {description}` only when frontmatter contains a non-empty string `description`.
- Omit the separator when no description exists.
- Preserve existing deterministic kind and concept ordering.
- Do not change `include_descriptive=False`; indexes use descriptions only when generation already produced them.

Consumers continue accepting both absolute and relative authored links.

## Fingerprint relocation and migration

New generated documents store:

```yaml
generated:
  by: process:selayer-okf
  at: 2026-07-28T00:00:00Z
selayer_fingerprint: <sha256>
```

They no longer write `generated.fingerprint`.

Fingerprint canonicalization excludes both locations so legacy and new metadata do not alter the content hash:

- top-level `selayer_fingerprint`
- legacy `generated.fingerprint`

Synchronization uses this precedence:

1. a valid top-level `selayer_fingerprint`;
2. otherwise a valid legacy `generated.fingerprint`;
3. otherwise no trusted baseline fingerprint.

If both fields exist, the top-level field is authoritative. A malformed authoritative new field is a conflict; sync must not silently fall back to the legacy field and mask corruption.

Generation writes only the new field. A successful sync of an untouched legacy generated document removes `generated.fingerprint` and writes `selayer_fingerprint`, providing automatic migration. Curated edits continue producing conflicts exactly as before.

Validation rules:

- `selayer_fingerprint`, when present, must be a lowercase 64-character hexadecimal SHA-256 string;
- legacy `generated.fingerprint` remains accepted and validated during the compatibility window;
- both may coexist for reading, with the new field authoritative;
- unknown extensions remain preserved.

## Catalog-backed resources

### Sources

A generated data-source concept uses the catalog's physical path verbatim:

```yaml
resource: data/products.parquet
```

The generator does not require the path to exist and does not convert it to an absolute filesystem path.

### Dimensions

A generated dimension identifies its physical column with a URI fragment:

```yaml
resource: data/products.parquet#column=mlfb
```

Construction rules:

1. resolve the dimension's catalog `source` to its `DataSource.path`;
2. append `#column=`;
3. URL-encode the column value with UTF-8 percent encoding and no unescaped `/`, `#`, `?`, `&`, or `=` characters.

Use `urllib.parse.quote(column, safe="")`; this is part of Python's standard library.

If the catalog source or column is invalid, normal catalog validation remains responsible for rejecting it before OKF generation. No resource is generated for facts, measures, metrics, or relationships.

## Synchronization and round-tripping

- Generated canonical type, resource, description, links, and new fingerprint participate in deterministic generated output.
- Authored unknown fields remain preserved during parse/render.
- Sync compares canonical content with both fingerprint locations excluded.
- Legacy generated documents that are unchanged are upgraded.
- Legacy documents with curated edits remain conflicts and are not overwritten.
- Dry-run and real sync report the same create/update/conflict decisions.

## Error handling

- A semantic ID/type mismatch remains an error for both canonical and legacy recognized types.
- Invalid new or legacy fingerprints remain errors.
- Missing descriptions simply omit index description text.
- Existing consumer support for relative links is retained.
- Resource construction performs no I/O and cannot fail for a catalog that has already passed validation.

## Testing strategy

- Golden generation tests for all six canonical type names.
- Compatibility validation tests for all six legacy type names and mismatches.
- Index tests covering absolute root/local links, descriptions, missing descriptions, and deterministic order.
- Fingerprint unit tests for canonicalization, precedence, malformed fields, and coexistence.
- Sync tests for untouched legacy auto-upgrade, curated legacy conflicts, new-field conflicts, and dry-run parity.
- Resource tests for source paths, ordinary columns, and percent-encoded columns.
- Round-trip tests preserving authored unknown types and fields.
- CLI/documentation snapshot updates where generated output changes.
- Full test suite, Ruff, and Pyright verification.

## Delivery order

1. Add dual type recognition and switch generated names.
2. Relocate fingerprints with dual-read/new-write synchronization.
3. Add physical resources.
4. Add index descriptions and absolute links.
5. Update snapshots/documentation and run full regression verification.
