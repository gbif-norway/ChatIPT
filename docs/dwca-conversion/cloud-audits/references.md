# Alternative Identifiers and Literature References: audit and helpers

Scope: GBIF `http://rs.gbif.org/terms/1.0/Identifier` and `http://rs.gbif.org/terms/1.0/Reference` on
Event and Occurrence cores only, against DwC-DP schema revision
`76898192fd298c2aa170a7059e1bdadf3ee2a828` (`back-end/api/templates/dwc-dp`, 79 table schemas).
Taxon cores and other subjects are unsupported. Implementation: `back-end/api/dwca_references.py`;
tests: `back-end/api/test_dwca_references.py`. `dwca_conversion.py`, `dwca_import.py` and existing tests are unchanged.

## Sources audited

- `sources/extension/e4e4b0b7aaaa-identifier.xml` — Alternative Identifiers, issued 2015-02-13. Only registry version of this row type.
- `sources/extension/a6d313919dbb-references.xml` — Literature References, issued 2015-02-13. Only registry version of this row type.
- `sources/extension/650d0e7712e0-reference_extension.xml` is EOL References (`http://eol.org/schema/reference/Reference`), a different row type; not handled here.
- `registry.json` `row_types` for both IRIs list exactly the XML term IRIs (checked by a test).
- Pinned tables: `occurrence-identifier`, `event-identifier`, `bibliographic-resource`, `occurrence-reference`, `event-reference`.

## Mappings

"Exact" means the pinned field's `dcterms:isVersionOf` equals the source term IRI. Non-exact mappings are
definition-based, listed in `NON_EXACT_TARGETS`, and must be reviewed; they are not aliases of the same term.

| Row type | Source term IRI | Target (neutral) | Match |
|---|---|---|---|
| Identifier | `http://purl.org/dc/terms/identifier` (required) | `occurrence-identifier.identifier` | non-exact: target IRI `skos:notation` |
| Identifier | `dcterms:title`, `dcterms:subject`, `dcterms:format`, `dwc:datasetID` | preserved | no target; `dcterms:format` is the MIME type of the resolved resource, never `identifierType` |
| Reference | `http://purl.org/dc/terms/identifier` | `bibliographic-resource.referenceID` | non-exact: target IRI `dwc:referenceID` |
| Reference | `http://purl.org/dc/terms/bibliographicCitation` | `bibliographic-resource.bibliographicCitation` | exact |
| Reference | `http://purl.org/dc/terms/title` | `bibliographic-resource.title` | exact |
| Reference | `http://purl.org/dc/terms/creator` | `bibliographic-resource.author` | non-exact: target IRI `http://purl.org/dc/elements/1.1/creator` |
| Reference | `http://purl.org/dc/terms/date` | `bibliographic-resource.issued` | non-exact: target IRI `dcterms:issued`; examples include `6/1/2009` |
| Reference | `dcterms:source`, `description`, `subject`, `language`, `rights`, `type`, `dwc:taxonRemarks`, `dwc:datasetID` | preserved | no faithful field. `dcterms:type` uses a taxonomic/nomenclatural vocabulary, so it is neither `referenceType` nor an inferred `relationshipType` |

## Emitted records

- Identifier row: one `{subject}-identifier` row `{subject_fk, identifier}`. Missing/blank `dcterms:identifier` raises `ImportFailure`.
- Reference row: one `bibliographic-resource` row with `reference_pk = record_key` and mapped nonblank values, plus
  one `{subject}-reference` row `{reference_fk, subject_fk}`. Supplied identifiers stay in `referenceID`, never in keys.
  `relationshipType` is never set. A row with no mapped nonblank bibliographic value raises `ImportFailure` rather than emitting an empty record.
- `ImportFailure` is also raised for: subject other than occurrence/event; blank subject key or reference record key;
  any unaudited table/field (including `_pk`/`_fk`, `identifierType`, `referenceType`, `relationshipType`) regardless of value;
  event-* targets on an occurrence subject; differing values for one field supplied under both `occurrence-*` and `event-*`.
- Blank values are omitted; nonblank values are copied verbatim (not trimmed).

## Integration requirements for the coordinator

1. Add both row types to extension handling with table options `[identifier|reference, PRESERVE]` on Event and Occurrence cores only.
2. Column options: `reference_targets(row_type, term)` (rewrite `occurrence-` to `event-` on Event cores), always plus PRESERVE. Do not use `_candidates` for these families (it would match by IRI only and miss the reviewed decisions). Set `review=True` for `(row_type, term)` in `NON_EXACT_TARGETS`.
3. Subject: `occurrence_keys[source_id]` on Occurrence cores, `event_keys[source_id]` on Event cores. The archive attachment identifies the core record only; when an Occurrence-core archive uses `event-grain=by_id`, references still attach to the occurrence, not the grouped event.
4. `record_key`: `_key(archive, "reference", t, n)` (per table and row). `add()` each returned row; for the link/identifier tables (no primary key) the crosswalk key is empty, so trace them with the subject key explicitly if needed.
5. Disposition rewriting in `convert()` must map `occurrence-identifier.*` to `event-identifier.*` on Event cores, as done for assertions.
6. Validate that `bibliographic-resource`, `*-identifier` and `*-reference` are present in `TABLE_SPECS` exports and in the DwC-DP package profile when non-empty.

## Known limitations / review triggers

- One bibliographic record per source row (as specified). The existing catalogue proposal (`extension-catalogue.json` family "Literature References (2015)") says one record per *distinct* reference; that conflicts with this specification. The extension also allows repeating a work in multiple rows to list several identifiers (DOI + PDF URL), which therefore yields several records for one work. Deduplication needs a separate reviewed rule.
- The Identifier XML defines `dcterms:identifier` as "Other known identifier used for the same taxon"; use on occurrence/event cores relies on the GBIF extension's general use and the catalogue's conditional proposal — confirm per dataset.
- `dcterms:date` → `issued` is not normalised; non-ISO values are copied verbatim.

## Verification

Not run. Docker was available, but `back-end/.env.dev` (the `env_file` required by the `back-end` service in
`docker-compose.yml`) is absent from this snapshot, and `CLOUD_TASKS.md` forbids substituting host Python.
The coordinator should run:
`docker compose exec back-end python manage.py test api.test_dwca_references`.
